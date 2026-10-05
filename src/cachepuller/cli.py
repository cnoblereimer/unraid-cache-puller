"""Command line entry point.

    cache-puller run      # daemon: track accesses and move files periodically
    cache-puller once     # a single move cycle using the scores collected so far
    cache-puller check    # show what the container can see and whether it's safe to move
    cache-puller status   # hottest files, promoted files and recent actions
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time

from . import __version__
from .config import Config, ConfigError
from .db import Database, FileFilter
from .safety import host_pid_visible, mover_running
from .service import PoolUsage, Service, human
from .settings import SettingsStore
from .unraid import array_state, load_shares, pool_mounted

log = logging.getLogger("cachepuller")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def _open_db(cfg: Config) -> Database:
    os.makedirs(cfg.config_dir, exist_ok=True)
    return Database(cfg.db_path, cfg.half_life)


def cmd_run(cfg: Config, store: SettingsStore) -> int:
    from .daemon import Daemon
    from .tracker import AccessTracker  # needs inotify, only import when running
    from .web import start_web

    log.info("unraid-cache-puller %s starting%s", __version__, " (DRY RUN)" if cfg.dry_run else "")
    db = _open_db(cfg)
    tracker = AccessTracker(db, cfg.access_debounce, mode=cfg.tracker)
    daemon = Daemon(cfg, db, store, tracker)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: daemon.shutdown())

    daemon.service.journal.recover()
    _warn_about_setup(cfg, daemon.service)
    tracker.start()
    server = None
    port = int(os.environ.get("UI_PORT", "8080") or 0)
    if port:
        server = start_web(daemon, os.environ.get("UI_BIND", "0.0.0.0"), port,
                           os.environ.get("UI_PASSWORD") or None)
    try:
        daemon.run_forever()
    finally:
        log.info("shutting down")
        if server is not None:
            server.shutdown()
        tracker.stop()
        db.close()
    return 0


def _warn_about_setup(cfg: Config, service: Service) -> None:
    shares = service.shares()
    if not shares:
        log.warning(
            "no managed shares found in %s (need primary storage = pool, secondary = array)",
            cfg.shares_cfg_dir,
        )
    for s in shares:
        log.info("managing share %s (pool %s, mover %s)", s.name, s.pool,
                 "pool->array" if s.use_cache == "yes" else "array->pool")
    if any(s.use_cache == "yes" for s in shares) and not cfg.mover_ignore_file:
        log.warning(
            "MOVER_IGNORE_FILE is empty: the Unraid mover will move promoted files on "
            "'yes' shares back to the array on its next run"
        )


def cmd_once(cfg: Config) -> int:
    db = _open_db(cfg)
    service = Service(cfg, db)
    service.journal.recover()
    report = service.run_cycle()
    db.close()
    return 1 if report.failed else 0


class _NoCheck:
    usable = True

    def is_open(self, paths: list[str]) -> bool:
        return False


def cmd_check(cfg: Config) -> int:
    ok = True

    def line(status: str, msg: str) -> None:
        print(f"[{status:^4}] {msg}")

    line("info", f"dry run: {cfg.dry_run}")
    st = array_state(cfg)
    if not st.known:
        ok = ok and not cfg.require_array_state
        line("FAIL" if cfg.require_array_state else "warn", f"array state: {st.detail}")
    else:
        line("ok" if st.started else "FAIL", f"array: {st.detail}")
        if st.parity_running:
            line("warn", "parity operation running")
    pid = host_pid_visible()
    line("ok" if pid else ("FAIL" if cfg.open_file_check == "on" else "warn"),
         f"host processes visible (--pid=host): {pid}")
    if cfg.open_file_check == "on" and not pid:
        ok = False
    mover = mover_running(cfg.mover_pid_files, cfg.mover_process_names)
    line("warn" if mover else "ok", f"mover running: {mover or 'no'}")

    db = _open_db(cfg)
    service = Service(cfg, db, open_checker=_NoCheck())
    disks = service.disks()
    line("ok" if disks else "FAIL", f"array disks mounted: {', '.join(disks) or 'none'}")
    ok = ok and bool(disks)
    all_shares = load_shares(cfg)
    managed = {s.name for s in service.shares()}
    for s in sorted(all_shares.values(), key=lambda s: s.name):
        if s.name in managed:
            mounted = pool_mounted(cfg, s.pool, cfg.require_mounts)
            usage = ""
            if mounted:
                u = PoolUsage.of(os.path.join(cfg.mnt_root, s.pool))
                usage = f", pool {human(u.used)} / {human(u.total)} used"
            line("ok" if mounted else "FAIL", f"share {s.name}: managed (useCache={s.use_cache}, pool={s.pool}{usage})")
        else:
            why = "array is not secondary" if not s.array_is_secondary else "excluded by configuration"
            line("info", f"share {s.name}: ignored ({why})")
    if cfg.tracker != "inotify":
        try:
            from .fanotify import Fanotify
            Fanotify().close()
            line("ok", "access tracking: fanotify (whole disks, no folder scan)")
        except OSError as exc:
            line("warn", f"access tracking: inotify fallback, folders must be scanned first "
                 f"(fanotify: {exc}; add --cap-add=SYS_ADMIN --cap-add=DAC_READ_SEARCH)")
    try:
        with open("/proc/sys/fs/inotify/max_user_watches") as fh:
            line("info", f"fs.inotify.max_user_watches = {fh.read().strip()}")
    except OSError:
        pass
    db.close()
    print("\nready" if ok else "\nNOT ready: fix the FAIL lines above")
    return 0 if ok else 1


def cmd_status(cfg: Config, limit: int) -> int:
    db = _open_db(cfg)
    now = time.time()
    tracked = db.count()
    hot = db.count(FileFilter(min_score=cfg.min_score), now)
    print(f"tracked files: {tracked}, hot (score >= {cfg.min_score:g}): {hot}\n")
    print(f"{'score':>7} {'hits':>5}  {'last access':<19}  file")
    for f in db.query(limit=limit, now=now):
        last = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(f.last_access))
        print(f"{f.score:7.2f} {f.hits:5d}  {last:<19}  {f.share}/{f.relpath}")
    promoted = db.promoted()
    total = sum(p.size for p in promoted)
    print(f"\nfiles promoted to a pool and still there: {len(promoted)} ({human(total)})")
    print("\nrecent actions:")
    for ts, action, share, rel, size, result, msg in db.history(limit):
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        extra = f" ({msg})" if msg else ""
        print(f"  {when} {action:<7} {result:<7} {share}/{rel} {human(size or 0)}{extra}")
    db.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cache-puller", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("run", help="run the daemon (default)")
    sub.add_parser("once", help="run a single move cycle and exit")
    sub.add_parser("check", help="check the environment")
    p_status = sub.add_parser("status", help="show hot files and recent actions")
    p_status.add_argument("-n", "--limit", type=int, default=25)
    args = parser.parse_args(argv)

    try:
        base = Config.from_env()
    except (ConfigError, ValueError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    _setup_logging(base.log_level)
    os.makedirs(base.config_dir, exist_ok=True)
    store = SettingsStore(os.path.join(base.config_dir, "settings.json"))
    try:
        cfg = store.load()
    except (ConfigError, ValueError) as exc:
        log.error("ignoring saved settings in %s: %s", store.path, exc)
        cfg = base

    if args.cmd == "once":
        return cmd_once(cfg)
    if args.cmd == "check":
        return cmd_check(cfg)
    if args.cmd == "status":
        return cmd_status(cfg, args.limit)
    return cmd_run(cfg, store)


if __name__ == "__main__":
    sys.exit(main())
