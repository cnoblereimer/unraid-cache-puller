"""Watch share directories on array disks and pools and count file opens.

Two backends, chosen per share root:

* fanotify (preferred): one mark per filesystem, no startup scan, no watch
  limit. Needs CAP_SYS_ADMIN and CAP_DAC_READ_SEARCH.
* inotify (fallback): one watch per directory, so every directory has to be
  found first. Scans run in the background, one thread per disk.

Both see opens made through Unraid's /mnt/user FUSE layer, because shfs opens
the underlying file on the disk.
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
from .fanotify import Fanotify, FanEvent

log = logging.getLogger(__name__)

TEMP_PREFIX = ".cachepuller."

DIR_MASK = (
    ino.IN_OPEN
    | ino.IN_CREATE
    | ino.IN_DELETE
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
    def __init__(self, db: Database, debounce: float, flush_interval: float = 30.0, mode: str = "auto"):
        self.db = db
        self.debounce = debounce
        self.flush_interval = flush_interval
        self._ino = ino.Inotify()
        self._fan: Fanotify | None = None
        if mode in ("auto", "fanotify"):
            try:
                self._fan = Fanotify()
            except OSError as exc:
                level = logging.WARNING if mode == "fanotify" else logging.INFO
                log.log(level, "fanotify unavailable (%s); using inotify, which has to scan every "
                        "folder first. Add --cap-add=SYS_ADMIN --cap-add=DAC_READ_SEARCH to avoid that.", exc)
        self._lock = threading.Lock()
        self._watches: dict[int, _Watch] = {}
        self._roots: set[str] = set()  # inotify roots fully scanned
        self._fan_roots: dict[str, str] = {}  # fanotify roots -> share
        self._fan_lookup: list[tuple[str, str]] = []  # (root, share), longest first
        self._scan_queues: dict[int, list[tuple[str, str]]] = {}  # st_dev -> pending roots
        self._scanning: dict[str, int] = {}  # root being scanned / queued -> dirs so far
        self._pending: dict[tuple[str, str], list[float]] = {}
        self._last_hit: dict[tuple[str, str], float] = {}
        self._suppress: dict[tuple[str, str], float] = {}
        self._moves: dict[int, tuple[str, str, float]] = {}
        self._deleted: dict[tuple[str, str], float] = {}  # files seen being deleted
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

    @property
    def fanotify_available(self) -> bool:
        return self._fan is not None

    def state(self) -> dict:
        with self._lock:
            scanning = dict(self._scanning)
            fan_roots = sorted(self._fan_roots)
        filesystems = set()
        for r in fan_roots:
            try:
                filesystems.add(os.stat(r).st_dev)
            except OSError:
                pass
        if fan_roots and (self._roots or scanning):
            mode = "mixed"
        elif fan_roots:
            mode = "fanotify"
        elif self._roots or scanning:
            mode = "inotify"
        else:
            mode = "none"
        return {
            "mode": mode,
            "fanotify_available": self._fan is not None,
            "filesystems": len(filesystems),
            "fanotify_roots": len(fan_roots),
            "inotify_roots": len(self._roots),
            "watches": len(self._watches),
            "scanning": bool(scanning),
            "scan_progress": [{"root": r, "dirs": n} for r, n in sorted(scanning.items())],
            "events": self.events_seen,
            "overflows": self.overflows,
            "limit_reached": self._limit_warned,
        }

    def sync_roots(self, roots: dict[str, str]) -> None:
        """Ensure every ``{abs_share_dir: share_name}`` root is watched.

        Returns quickly: fanotify marks are instant and inotify scans run in
        background threads.
        """
        for path, share in roots.items():
            if not os.path.isdir(path):
                continue
            with self._lock:
                on_inotify = path in self._roots or path in self._scanning
            if self._fan is not None and not on_inotify:
                try:
                    # Re-marking every time is cheap and idempotent, and
                    # re-arms the mark after the array is stopped and started.
                    self._fan.mark(path)
                except OSError as exc:
                    log.warning("fanotify can't watch %s (%s); scanning its folders for inotify instead", path, exc)
                else:
                    if path not in self._fan_roots:
                        log.info("watching %s (whole disk via fanotify, no scan needed)", path)
                        with self._lock:
                            self._fan_roots[path] = share
                            self._fan_lookup = sorted(self._fan_roots.items(), key=lambda x: len(x[0]), reverse=True)
                    continue
            if not on_inotify:
                self._queue_scan(path, share)

    def _queue_scan(self, path: str, share: str) -> None:
        try:
            dev = os.stat(path).st_dev
        except OSError:
            return
        with self._lock:
            self._scanning[path] = 0
            queue = self._scan_queues.get(dev)
            if queue is not None:
                queue.append((path, share))  # that disk's scanner will pick it up
                return
            self._scan_queues[dev] = [(path, share)]
        # One scanner per disk: disks are scanned in parallel, but a single
        # disk isn't made to seek between several trees at once.
        threading.Thread(target=self._scan_disk, args=(dev,), name=f"scan-{dev}", daemon=True).start()

    def _scan_disk(self, dev: int) -> None:
        while not self._stop.is_set():
            with self._lock:
                queue = self._scan_queues.get(dev)
                if not queue:
                    self._scan_queues.pop(dev, None)
                    return
                path, share = queue.pop(0)
            started = time.monotonic()
            try:
                n = self._add_tree(path, share, "", progress=path)
            except Exception:
                log.exception("scanning %s failed", path)
                n = 0
            with self._lock:
                self._scanning.pop(path, None)
                self._roots.add(path)
            log.info("watching %s (%d folders scanned in %.0fs)", path, n, time.monotonic() - started)

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

    def _add_tree(self, path: str, share: str, reldir: str, progress: str | None = None) -> int:
        count = 0
        stack = [(path, reldir)]
        while stack:
            if self._stop.is_set():
                break
            p, r = stack.pop()
            if not self._add_one(p, share, r):
                if self._limit_warned:
                    break
                continue
            count += 1
            if progress is not None and count % 500 == 0:
                with self._lock:
                    if progress in self._scanning:
                        self._scanning[progress] = count
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
        if self._fan is not None:
            self._fan.close()

    def _fds(self) -> list[int]:
        return [self._ino.fd] + ([self._fan.fd] if self._fan is not None else [])

    def _read_ready(self, ready: list[int]) -> None:
        if self._ino.fd in ready:
            self.handle_events(self._ino.read())
        if self._fan is not None and self._fan.fd in ready:
            self.handle_fan_events(self._fan.read())

    def _run(self) -> None:
        last_flush = time.monotonic()
        while not self._stop.is_set():
            try:
                r, _, _ = select.select(self._fds(), [], [], 1.0)
                self._read_ready(r)
                if time.monotonic() - last_flush >= self.flush_interval:
                    self.flush()
                    last_flush = time.monotonic()
            except Exception:  # keep the tracker alive no matter what
                log.exception("tracker error")
                time.sleep(1)

    def poll(self, timeout: float = 0.0) -> None:
        """Process pending events synchronously (used by tests)."""
        r, _, _ = select.select(self._fds(), [], [], timeout)
        self._read_ready(r)

    def wait_for_scans(self, timeout: float = 30.0) -> None:
        """Block until background inotify scans are done (used by tests)."""
        deadline = time.monotonic() + timeout
        while self._scanning and time.monotonic() < deadline:
            time.sleep(0.01)

    def _locate(self, path: str) -> tuple[str, str] | None:
        for root, share in self._fan_lookup:
            if path.startswith(root + "/"):
                return share, path[len(root) + 1:]
        return None

    def handle_fan_events(self, events: list[FanEvent], now: float | None = None) -> None:
        now = time.time() if now is None else now
        for ev in events:
            self.events_seen += 1
            if ev.kind == "overflow":
                self.overflows += 1
                log.warning("fanotify queue overflow; some accesses were not counted")
                continue
            new = self._locate(ev.path) if ev.path else None
            if ev.kind == "open":
                if new and not os.path.basename(new[1]).startswith(TEMP_PREFIX):
                    self._hit(new[0], new[1], now)
            elif ev.kind == "delete":
                if new:
                    self._note_deleted(new[0], new[1], now)
            elif ev.kind == "rename":
                old = self._locate(ev.old_path) if ev.old_path else None
                if old and new and old[0] == new[0] and old[1] != new[1]:
                    self.flush()
                    self.db.rename(new[0], old[1], new[1])

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
            if ev.mask & ino.IN_DELETE:
                self._note_deleted(w.share, rel, now)
            elif ev.mask & ino.IN_MOVED_FROM:
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

    def _note_deleted(self, share: str, rel: str, now: float) -> None:
        if os.path.basename(rel).startswith(TEMP_PREFIX):
            return
        with self._lock:
            if len(self._deleted) < 200000:
                self._deleted[(share, rel)] = now

    def take_deleted(self, older_than: float, now: float | None = None) -> list[tuple[str, str]]:
        """Files seen being deleted at least ``older_than`` seconds ago.

        A deletion on one disk doesn't mean the file is gone: the mover and
        this app delete the old copy after copying a file elsewhere, so the
        caller must still check every location.
        """
        now = time.time() if now is None else now
        with self._lock:
            due = [k for k, t in self._deleted.items() if now - t >= older_than]
            for k in due:
                del self._deleted[k]
        return due

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
