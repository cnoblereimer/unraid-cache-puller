"""The long-running service: access tracker, periodic cycles and web UI state."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict

from .config import Config
from .db import Database
from .service import CleanupReport, CycleReport, Service
from .settings import SettingsStore

log = logging.getLogger(__name__)


class Busy(Exception):
    """A cycle or another move is already running."""


class Daemon:
    def __init__(self, cfg: Config, db: Database, store: SettingsStore, tracker=None,
                 service: Service | None = None):
        self.cfg = cfg
        self.db = db
        self.store = store
        self.tracker = tracker
        self.stop = threading.Event()
        self._wake = threading.Event()
        self._work_lock = threading.Lock()
        suppress = tracker.suppress if tracker is not None else None
        self.service = service or Service(cfg, db, suppress=suppress, stop=self.stop)
        self.started_at = time.time()
        self.next_cycle = time.time() + min(cfg.run_interval, 300)
        self.cycle_running = False
        self.last_cycle_start: float | None = None
        self.last_cycle_end: float | None = None
        self.last_report: CycleReport | None = None
        self.last_error: str | None = None
        # Deleted-file cleanup: first full check a few minutes after start.
        self.next_cleanup: float | None = (
            time.time() + min(cfg.cleanup_interval, 600) if cfg.cleanup_interval else None
        )
        self.cleanup_running = False
        self.last_cleanup: CleanupReport | None = None
        self.deleted_removed = 0  # removed right after being deleted, since start
        # Bumped whenever file states may have changed (moves, cleanups,
        # settings), so cached per-file checks in the web UI get redone.
        self.generation = 0

    # -- configuration -----------------------------------------------------

    def apply_config(self, cfg: Config) -> None:
        """Switch to new settings without restarting."""
        old_interval = self.cfg.run_interval
        old_cleanup = self.cfg.cleanup_interval
        self.cfg = cfg
        self.service.cfg = cfg
        self.db.set_half_life(cfg.half_life)
        if self.tracker is not None:
            self.tracker.debounce = cfg.access_debounce
        if cfg.run_interval != old_interval and self.last_cycle_end is not None:
            self.next_cycle = self.last_cycle_end + cfg.run_interval
        if not cfg.cleanup_interval:
            self.next_cleanup = None
        elif cfg.cleanup_interval != old_cleanup or self.next_cleanup is None:
            base = self.last_cleanup.finished if self.last_cleanup and self.last_cleanup.finished else time.time()
            self.next_cleanup = max(time.time() + 60, base + cfg.cleanup_interval)
        self.generation += 1
        self._wake.set()
        log.info("settings updated%s", " (DRY RUN)" if cfg.dry_run else "")

    # -- work --------------------------------------------------------------

    def request_cycle(self) -> None:
        if self.cycle_running:
            raise Busy("a run is already in progress")
        self.next_cycle = time.time()
        self._wake.set()

    def promote_one(self, share: str, rel: str) -> tuple[bool, str]:
        if not self._work_lock.acquire(blocking=False):
            raise Busy("a run is in progress; try again when it has finished")
        try:
            if self.tracker is not None:
                self.tracker.flush()
            return self.service.promote_one(share, rel)
        finally:
            self.generation += 1
            self._work_lock.release()

    def run_cycle(self) -> None:
        with self._work_lock:
            self.cycle_running = True
            self.last_cycle_start = time.time()
            try:
                if self.tracker is not None:
                    self.tracker.flush()
                self.last_report = self.service.run_cycle()
                self.last_error = None
            except Exception as exc:
                log.exception("cycle failed")
                self.last_error = str(exc)
            finally:
                self.cycle_running = False
                self.generation += 1
                self.last_cycle_end = time.time()
                self.next_cycle = self.last_cycle_end + self.cfg.run_interval

    # -- deleted files -------------------------------------------------------

    DELETE_CHECK_DELAY = 60.0  # seconds between seeing a delete and checking

    def request_cleanup(self) -> None:
        if self.cleanup_running:
            raise Busy("a check for deleted files is already running")
        self._start_cleanup()

    def _start_cleanup(self) -> None:
        self.cleanup_running = True
        threading.Thread(target=self._cleanup, name="cleanup", daemon=True).start()

    def _cleanup(self) -> None:
        try:
            if self.tracker is not None:
                self.tracker.flush()
            self.last_cleanup = self.service.cleanup_missing()
        except Exception as exc:
            log.exception("deleted-file check failed")
            self.last_cleanup = CleanupReport(started=time.time(), finished=time.time(),
                                              skipped_reason=f"error: {exc}")
        finally:
            self.cleanup_running = False
            self.generation += 1
            if self.cfg.cleanup_interval:
                self.next_cleanup = time.time() + self.cfg.cleanup_interval

    def check_deleted(self, now: float | None = None) -> int:
        """Forget files that were deleted a minute ago and are really gone."""
        if self.tracker is None:
            return 0
        removed = 0
        for share, rel in self.tracker.take_deleted(self.DELETE_CHECK_DELAY, now):
            if not self.db.is_tracked(share, rel):
                continue
            ok, _msg = self.service.forget_if_missing(share, rel)
            if ok:
                removed += 1
                log.debug("forgot deleted file %s/%s", share, rel)
        self.deleted_removed += removed
        if removed:
            self.generation += 1
        return removed

    def forget(self, share: str, rel: str) -> tuple[bool, str]:
        result = self.service.forget_if_missing(share, rel)
        self.generation += 1
        return result

    # -- loop ----------------------------------------------------------------

    def run_forever(self) -> None:
        while not self.stop.is_set():
            try:
                if self.tracker is not None:
                    self.tracker.sync_roots(self.service.watch_roots())
            except Exception:
                log.exception("could not update watches")
            try:
                self.check_deleted()
            except Exception:
                log.exception("could not check deleted files")
            if self.next_cleanup is not None and time.time() >= self.next_cleanup and not self.cleanup_running:
                self._start_cleanup()
            if time.time() >= self.next_cycle:
                self.run_cycle()
            self._wake.clear()
            self._wake.wait(min(60, max(0.5, self.next_cycle - time.time())))
            if self.stop.is_set():
                break

    def shutdown(self) -> None:
        self.stop.set()
        self._wake.set()

    # -- state for the UI --------------------------------------------------

    def cycle_state(self) -> dict:
        return {
            "running": self.cycle_running,
            "last_start": self.last_cycle_start,
            "last_end": self.last_cycle_end,
            "next_at": self.next_cycle,
            "interval": self.cfg.run_interval,
            "last_error": self.last_error,
            "last_report": asdict(self.last_report) if self.last_report else None,
        }

    def cleanup_state(self) -> dict:
        return {
            "running": self.cleanup_running,
            "interval": self.cfg.cleanup_interval,
            "next_at": self.next_cleanup,
            "removed_on_delete": self.deleted_removed,
            "last": asdict(self.last_cleanup) if self.last_cleanup else None,
        }

    def tracker_state(self) -> dict:
        t = self.tracker
        if t is None:
            return {"active": False}
        return {"active": True, **t.state()}
