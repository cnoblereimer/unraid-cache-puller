"""Watch share directories on array disks and pools and count file opens.

inotify watches are per-inode, so opens that go through Unraid's /mnt/user
FUSE layer are seen too: shfs opens the underlying file on the disk.
"""

from __future__ import annotations

import errno
import logging
import os
import select
import threading
import time
from dataclasses import dataclass

from . import inotify as ino
from .db import Database

log = logging.getLogger(__name__)

TEMP_PREFIX = ".cachepuller."

DIR_MASK = (
    ino.IN_OPEN
    | ino.IN_CREATE
    | ino.IN_MOVED_FROM
    | ino.IN_MOVED_TO
    | ino.IN_ONLYDIR
    | ino.IN_DONT_FOLLOW
    | ino.IN_EXCL_UNLINK
)


@dataclass
class _Watch:
    share: str
    reldir: str  # directory relative to the share root ("" for the root)
    path: str  # absolute directory path


def _join(reldir: str, name: str) -> str:
    return f"{reldir}/{name}" if reldir else name


class AccessTracker:
    def __init__(self, db: Database, debounce: float, flush_interval: float = 30.0):
        self.db = db
        self.debounce = debounce
        self.flush_interval = flush_interval
        self._ino = ino.Inotify()
        self._lock = threading.Lock()
        self._watches: dict[int, _Watch] = {}
        self._roots: set[str] = set()
        self._pending: dict[tuple[str, str], list[float]] = {}
        self._last_hit: dict[tuple[str, str], float] = {}
        self._suppress: dict[tuple[str, str], float] = {}
        self._moves: dict[int, tuple[str, str, float]] = {}
        self._limit_warned = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.events_seen = 0
        self.overflows = 0

    # -- watch management -------------------------------------------------

    @property
    def watch_count(self) -> int:
        return len(self._watches)

    @property
    def limit_reached(self) -> bool:
        return self._limit_warned

    def sync_roots(self, roots: dict[str, str]) -> None:
        """Ensure every ``{abs_share_dir: share_name}`` root is watched."""
        for path, share in roots.items():
            if path in self._roots or not os.path.isdir(path):
                continue
            started = time.monotonic()
            n = self._add_tree(path, share, "")
            self._roots.add(path)
            log.info("watching %s (%d dirs, %.1fs)", path, n, time.monotonic() - started)

    def _add_one(self, path: str, share: str, reldir: str) -> bool:
        try:
            wd = self._ino.add_watch(path, DIR_MASK)
        except OSError as exc:
            if exc.errno == errno.ENOSPC and not self._limit_warned:
                self._limit_warned = True
                log.error(
                    "inotify watch limit reached after %d watches; raise "
                    "fs.inotify.max_user_watches on the host. Accesses in unwatched "
                    "directories will not be counted.",
                    len(self._watches),
                )
            elif exc.errno not in (errno.ENOENT, errno.ENOTDIR, errno.ENOSPC):
                log.debug("cannot watch %s: %s", path, exc)
            return False
        with self._lock:
            self._watches[wd] = _Watch(share, reldir, path)
        return True

    def _add_tree(self, path: str, share: str, reldir: str) -> int:
        count = 0
        stack = [(path, reldir)]
        while stack:
            p, r = stack.pop()
            if not self._add_one(p, share, r):
                if self._limit_warned:
                    break
                continue
            count += 1
            try:
                with os.scandir(p) as it:
                    for entry in it:
                        if entry.name.startswith(TEMP_PREFIX):
                            continue
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append((entry.path, _join(r, entry.name)))
                        except OSError:
                            continue
            except OSError as exc:
                log.debug("cannot list %s: %s", p, exc)
        return count

    # -- suppression of our own accesses ------------------------------------

    def suppress(self, share: str, relpath: str, seconds: float = 300.0) -> None:
        with self._lock:
            self._suppress[(share, relpath)] = time.time() + seconds

    # -- event loop ------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="tracker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        self.flush()
        self._ino.close()

    def _run(self) -> None:
        last_flush = time.monotonic()
        while not self._stop.is_set():
            try:
                r, _, _ = select.select([self._ino.fd], [], [], 1.0)
                if r:
                    self.handle_events(self._ino.read())
                if time.monotonic() - last_flush >= self.flush_interval:
                    self.flush()
                    last_flush = time.monotonic()
            except Exception:  # keep the tracker alive no matter what
                log.exception("tracker error")
                time.sleep(1)

    def poll(self, timeout: float = 0.0) -> None:
        """Process pending events synchronously (used by tests)."""
        r, _, _ = select.select([self._ino.fd], [], [], timeout)
        if r:
            self.handle_events(self._ino.read())

    def handle_events(self, events: list[ino.Event], now: float | None = None) -> None:
        now = time.time() if now is None else now
        for ev in events:
            self.events_seen += 1
            if ev.mask & ino.IN_Q_OVERFLOW:
                self.overflows += 1
                log.warning("inotify queue overflow; some accesses were not counted")
                continue
            if ev.mask & ino.IN_IGNORED:
                with self._lock:
                    w = self._watches.pop(ev.wd, None)
                if w is not None:
                    self._roots.discard(w.path)
                continue
            w = self._watches.get(ev.wd)
            if w is None or not ev.name or ev.name.startswith(TEMP_PREFIX):
                continue
            is_dir = bool(ev.mask & ino.IN_ISDIR)
            rel = _join(w.reldir, ev.name)
            path = os.path.join(w.path, ev.name)
            if is_dir and ev.mask & (ino.IN_CREATE | ino.IN_MOVED_TO):
                self._add_tree(path, w.share, rel)
                continue
            if is_dir:
                continue
            if ev.mask & ino.IN_MOVED_FROM:
                self._moves[ev.cookie] = (w.share, rel, now)
            elif ev.mask & ino.IN_MOVED_TO:
                src = self._moves.pop(ev.cookie, None)
                if src and src[0] == w.share and src[1] != rel:
                    self.flush()
                    self.db.rename(w.share, src[1], rel)
            elif ev.mask & ino.IN_OPEN:
                self._hit(w.share, rel, now)
        # Forget unmatched move halves after a while.
        if self._moves:
            self._moves = {k: v for k, v in self._moves.items() if now - v[2] < 60}

    def _hit(self, share: str, rel: str, now: float) -> None:
        key = (share, rel)
        with self._lock:
            until = self._suppress.get(key)
            if until is not None:
                if now < until:
                    return
                del self._suppress[key]
            last = self._last_hit.get(key)
            if last is not None and now - last < self.debounce:
                return
            self._last_hit[key] = now
            self._pending.setdefault(key, []).append(now)

    def flush(self) -> None:
        with self._lock:
            pending, self._pending = self._pending, {}
            cutoff = time.time() - self.debounce
            if len(self._last_hit) > 10000:
                self._last_hit = {k: v for k, v in self._last_hit.items() if v >= cutoff}
        if pending:
            self.db.record_hits(pending)
