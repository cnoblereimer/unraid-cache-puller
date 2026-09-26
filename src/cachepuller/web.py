"""Web UI: a small JSON API plus static files, on the standard library HTTP server."""

from __future__ import annotations

import base64
import glob
import hmac
import json
import logging
import os
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlparse

from . import __version__
from .config import ConfigError
from .daemon import Busy, Daemon
from .db import is_hot
from .unraid import load_shares, managed_shares, pool_mounted

log = logging.getLogger(__name__)

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
MAX_BODY = 256 * 1024


def _static(name: str) -> bytes:
    return resources.files("cachepuller").joinpath("web", name).read_bytes()


class Api:
    """Builds API responses from the daemon's state. Separate from HTTP for testing."""

    def __init__(self, daemon: Daemon):
        self.d = daemon

    # -- read ----------------------------------------------------------------

    def overview(self) -> dict:
        d = self.d
        cfg = d.cfg
        svc = d.service
        now = time.time()
        all_shares = load_shares(cfg)
        managed = managed_shares(cfg, all_shares)
        managed_names = {s.name for s in managed}
        scores = d.db.scores(now=now)
        hot_by_share: dict[str, int] = {}
        for f in scores:
            if is_hot(f.score, cfg.min_score):
                hot_by_share[f.share] = hot_by_share.get(f.share, 0) + 1
        promoted = d.db.promoted()

        pools = []
        for pool in sorted({s.pool for s in managed}):
            entry = {"name": pool, "mounted": pool_mounted(cfg, pool, cfg.require_mounts),
                     "limit_percent": cfg.cache_max_percent, "min_free": cfg.cache_min_free,
                     "promoted_bytes": sum(p.size for p in promoted if p.pool == pool),
                     "promoted_files": sum(1 for p in promoted if p.pool == pool)}
            if entry["mounted"]:
                try:
                    u = svc.usage_fn(os.path.join(cfg.mnt_root, pool))
                    entry.update(total=u.total, used=u.used, avail=u.avail)
                except OSError as exc:
                    entry["error"] = str(exc)
            pools.append(entry)

        shares = []
        for s in sorted(all_shares.values(), key=lambda s: s.name.lower()):
            if s.name in managed_names:
                status = "managed"
            elif not s.array_is_secondary:
                status = "not applicable"
            else:
                status = "excluded"
            shares.append({
                "name": s.name, "status": status, "use_cache": s.use_cache, "pool": s.pool,
                "secondary": s.secondary_pool or ("array" if s.use_cache in ("yes", "prefer") else ""),
                "hot_files": hot_by_share.get(s.name, 0),
                "eligible": s.array_is_secondary,
            })

        return {
            "version": __version__,
            "now": now,
            "dry_run": cfg.dry_run,
            "started_at": d.started_at,
            "gates": [g.__dict__ for g in svc.gates()],
            "disks": svc.disks(),
            "pools": pools,
            "shares": shares,
            "tracker": d.tracker_state(),
            "cycle": d.cycle_state(),
            "cleanup": d.cleanup_state(),
            "counts": {
                "tracked": len(scores),
                "hot": sum(hot_by_share.values()),
                "promoted": len(promoted),
                "promoted_bytes": sum(p.size for p in promoted),
            },
            "min_score": cfg.min_score,
            "mover_ignore_file": cfg.mover_ignore_file,
        }

    def files(self, q: str = "", hot_only: bool = False, limit: int = 100, offset: int = 0) -> dict:
        d = self.d
        cfg = d.cfg
        now = time.time()
        managed = {s.name: s for s in d.service.shares()}
        disks = d.service.disks()
        promoted = {(p.share, p.relpath): p for p in d.db.promoted()}
        rows = d.db.scores(now=now)
        if hot_only:
            rows = [r for r in rows if is_hot(r.score, cfg.min_score)]
        if q:
            needle = q.lower()
            rows = [r for r in rows if needle in f"{r.share}/{r.relpath}".lower()]
        total = len(rows)
        limit = max(1, min(limit, 500))
        out = []
        for r in rows[offset: offset + limit]:
            share = managed.get(r.share)
            if share is None:
                state, reason, location, size = "unmanaged", "share is not managed", "", 0
            else:
                chk = d.service.check_file(share, r.relpath, r.score, disks, now)
                state, reason, location, size = chk.state, chk.reason, chk.location, chk.size
                if state == "pool" and (r.share, r.relpath) in promoted:
                    reason = "moved to the pool by Cache Puller"
                    size = promoted[(r.share, r.relpath)].size
            out.append({
                "share": r.share, "relpath": r.relpath, "score": round(r.score, 2),
                "hits": r.hits, "last_access": r.last_access, "state": state,
                "reason": reason, "location": location, "size": size,
                "hot": is_hot(r.score, cfg.min_score),
            })
        return {"total": total, "offset": offset, "limit": limit, "rows": out, "min_score": cfg.min_score}

    def activity(self, limit: int = 100) -> dict:
        rows = self.d.db.history(max(1, min(limit, 1000)))
        return {"rows": [
            {"ts": ts, "action": action, "share": share, "relpath": rel, "size": size or 0,
             "result": result, "message": msg}
            for ts, action, share, rel, size, result, msg in rows
        ]}

    def settings(self) -> dict:
        data = self.d.store.describe(self.d.cfg)
        data["available_shares"] = sorted(
            (s.name for s in load_shares(self.d.cfg).values() if s.array_is_secondary), key=str.lower
        )
        return data

    # -- write ---------------------------------------------------------------

    def save_settings(self, body: dict) -> dict:
        changes = body.get("changes")
        if not isinstance(changes, dict):
            raise ConfigError("expected {\"changes\": {...}}")
        cfg = self.d.store.save(changes)
        self.d.apply_config(cfg)
        return self.settings()

    def reset_settings(self) -> dict:
        self.d.apply_config(self.d.store.reset())
        return self.settings()

    def cleanup(self) -> dict:
        self.d.request_cleanup()
        return {"ok": True, "message": "checking for deleted files…"}

    def forget(self, body: dict) -> dict:
        share, rel = _file_arg(body)
        ok, msg = self.d.forget(share, rel)
        return {"ok": ok, "message": msg}

    def run(self) -> dict:
        self.d.request_cycle()
        return {"ok": True, "message": "run started"}

    def promote(self, body: dict) -> dict:
        share, rel = _file_arg(body)
        ok, msg = self.d.promote_one(share, rel)
        return {"ok": ok, "message": msg}

    def exclude(self, body: dict) -> dict:
        share, rel = _file_arg(body)
        pattern = glob.escape(rel)
        patterns = list(self.d.cfg.exclude_patterns)
        if pattern not in patterns:
            patterns.append(pattern)
        self.save_settings({"changes": {"EXCLUDE_PATTERNS": patterns}})
        return {"ok": True, "message": f"{share}/{rel} will not be moved"}


def _file_arg(body: dict) -> tuple[str, str]:
    share, rel = body.get("share"), body.get("relpath")
    if not isinstance(share, str) or not isinstance(rel, str) or not share or not rel:
        raise ConfigError("share and relpath are required")
    if rel.startswith("/") or ".." in rel.split("/"):
        raise ConfigError("invalid path")
    return share, rel


def make_handler(api: Api, password: str | None):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"cache-puller/{__version__}"

        def log_message(self, fmt, *args):  # route through logging, quietly
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src 'self' data:; frame-ancestors 'self'")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, data) -> None:
            self._send(status, json.dumps(data, default=str).encode(), "application/json")

        def _authorized(self) -> bool:
            if not password:
                return True
            header = self.headers.get("Authorization", "")
            if header.startswith("Basic "):
                try:
                    _, _, given = base64.b64decode(header[6:]).decode().partition(":")
                except (ValueError, UnicodeDecodeError):
                    return False
                return hmac.compare_digest(given.encode(), password.encode())
            return False

        def _challenge(self) -> None:
            self._send(HTTPStatus.UNAUTHORIZED, b"authentication required", "text/plain",
                       {"WWW-Authenticate": 'Basic realm="cache-puller"'})

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if not self._authorized():
                return self._challenge()
            url = urlparse(self.path)
            qs = {k: v[-1] for k, v in parse_qs(url.query).items()}
            try:
                if url.path in STATIC:
                    name, ctype = STATIC[url.path]
                    return self._send(HTTPStatus.OK, _static(name), ctype)
                if url.path == "/api/overview":
                    return self._json(HTTPStatus.OK, api.overview())
                if url.path == "/api/files":
                    return self._json(HTTPStatus.OK, api.files(
                        q=qs.get("q", ""), hot_only=qs.get("hot") == "1",
                        limit=int(qs.get("limit", 100)), offset=int(qs.get("offset", 0))))
                if url.path == "/api/activity":
                    return self._json(HTTPStatus.OK, api.activity(int(qs.get("limit", 100))))
                if url.path == "/api/settings":
                    return self._json(HTTPStatus.OK, api.settings())
                if url.path == "/healthz":
                    return self._send(HTTPStatus.OK, b"ok", "text/plain")
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            except (ValueError, ConfigError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception as exc:
                log.exception("API error")
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

        def do_POST(self):
            if not self._authorized():
                return self._challenge()
            # Browsers can't send application/json cross-site without a CORS
            # preflight (which we never allow), so this blocks CSRF.
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                return self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "expected application/json"})
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc != self.headers.get("Host"):
                return self._json(HTTPStatus.FORBIDDEN, {"error": "cross-origin request refused"})
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length > MAX_BODY:
                    return self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "request too large"})
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("expected a JSON object")
                path = urlparse(self.path).path
                routes = {
                    "/api/settings": lambda: api.save_settings(body),
                    "/api/settings/reset": api.reset_settings,
                    "/api/run": api.run,
                    "/api/cleanup": api.cleanup,
                    "/api/forget": lambda: api.forget(body),
                    "/api/promote": lambda: api.promote(body),
                    "/api/exclude": lambda: api.exclude(body),
                }
                if path not in routes:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                self._json(HTTPStatus.OK, routes[path]())
            except Busy as exc:
                self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
            except (ValueError, ConfigError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception as exc:
                log.exception("API error")
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    return Handler


def start_web(daemon: Daemon, host: str, port: int, password: str | None) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(Api(daemon), password))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    log.info("web UI listening on http://%s:%d/%s", host, port,
             " (password protected)" if password else "")
    return server

