"""The long-running service: access tracker, periodic cycles and web UI state."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict

from .config import Config
from .db import Database
from .service import CycleReport, Service
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

    # -- configuration -----------------------------------------------------

    def apply_config(self, cfg: Config) -> None:
        """Switch to new settings without restarting."""
        old_interval = self.cfg.run_interval
        self.cfg = cfg
        self.service.cfg = cfg
        self.db.half_life = cfg.half_life
        if self.tracker is not None:
            self.tracker.debounce = cfg.access_debounce
        if cfg.run_interval != old_interval and self.last_cycle_end is not None:
            self.next_cycle = self.last_cycle_end + cfg.run_interval
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
                self.last_cycle_end = time.time()
                self.next_cycle = self.last_cycle_end + self.cfg.run_interval

    def run_forever(self) -> None:
        while not self.stop.is_set():
            try:
                if self.tracker is not None:
                    self.tracker.sync_roots(self.service.watch_roots())
            except Exception:
                log.exception("could not update watches")
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

    def tracker_state(self) -> dict:
        t = self.tracker
        if t is None:
            return {"active": False}
        return {"active": True, **t.state()}
