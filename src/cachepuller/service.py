"""Decide what to move and do it, one cycle at a time."""

from __future__ import annotations

import fnmatch
import logging
import os
import stat
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .config import Config
from .db import Database, Promoted
from .safety import OpenFileChecker, mover_running
from .transfer import Journal, TransferAborted, ensure_parents, safe_move
from .tracker import TEMP_PREFIX
from .unraid import Share, array_disks, array_state, fs_type, load_shares, managed_shares, pool_mounted

log = logging.getLogger(__name__)


@dataclass
class PoolUsage:
    total: int
    used: int
    avail: int

    @classmethod
    def of(cls, path: str) -> "PoolUsage":
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        return cls(total, total - st.f_bfree * st.f_frsize, st.f_bavail * st.f_frsize)


@dataclass
class CycleReport:
    skipped_reason: str | None = None
    promoted: list[str] = field(default_factory=list)
    demoted: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    bytes_promoted: int = 0
    bytes_demoted: int = 0
    ignore_list_entries: int = 0


class Service:
    def __init__(
        self,
        cfg: Config,
        db: Database,
        *,
        open_checker: OpenFileChecker | None = None,
        suppress: Callable[[str, str], None] | None = None,
        require_mounts: bool | None = None,
        usage_fn: Callable[[str], PoolUsage] = PoolUsage.of,
        stop: threading.Event | None = None,
    ):
        self.cfg = cfg
        self.db = db
        self.open_checker = open_checker or OpenFileChecker(cfg.open_file_check)
        self.suppress = suppress or (lambda share, rel: None)
        self.require_mounts = cfg.require_mounts if require_mounts is None else require_mounts
        self.usage_fn = usage_fn
        self.stop = stop or threading.Event()
        self.journal = Journal(os.path.join(cfg.config_dir, "transfer.journal"))

    # -- discovery ---------------------------------------------------------

    def shares(self) -> list[Share]:
        return managed_shares(self.cfg, load_shares(self.cfg))

    def disks(self) -> list[str]:
        return array_disks(self.cfg, self.require_mounts)

    def watch_roots(self) -> dict[str, str]:
        roots: dict[str, str] = {}
        disks = self.disks()
        for s in self.shares():
            for d in disks:
                roots[os.path.join(self.cfg.mnt_root, d, s.name)] = s.name
            if pool_mounted(self.cfg, s.pool, self.require_mounts):
                roots[os.path.join(self.cfg.mnt_root, s.pool, s.name)] = s.name
        return roots

    def _p(self, *parts: str) -> str:
        return os.path.join(self.cfg.mnt_root, *parts)

    # -- gates -----------------------------------------------------------

    def blocked_reason(self) -> str | None:
        """Why moving files is not safe right now (None if it is)."""
        cfg = self.cfg
        if cfg.allowed_hours is not None and time.localtime().tm_hour not in cfg.allowed_hours:
            return "outside ALLOWED_HOURS"
        st = array_state(cfg)
        if not st.known:
            if cfg.require_array_state:
                return f"array state unknown ({st.detail})"
        else:
            if not st.started:
                return f"array not started ({st.detail})"
            if st.parity_running and cfg.skip_during_parity:
                return "parity check/rebuild in progress"
        mover = mover_running(cfg.mover_pid_files, cfg.mover_process_names)
        if mover:
            return f"mover is running ({mover})"
        if not self.open_checker.usable:
            return "open-file check unavailable (need --pid=host and SYS_PTRACE)"
        return None

    def _excluded(self, rel: str) -> bool:
        name = os.path.basename(rel)
        if name.startswith(TEMP_PREFIX):
            return True
        return any(
            fnmatch.fnmatchcase(rel, pat) or fnmatch.fnmatchcase(name, pat)
            for pat in self.cfg.exclude_patterns
        )

    def _is_open(self, share: str, rel: str) -> Callable[[str], bool]:
        user_paths = [self._p("user", share, rel), self._p("user0", share, rel)]
        return lambda p: self.open_checker.is_open([p, *user_paths])

    # -- cycle -----------------------------------------------------------

    def run_cycle(self, now: float | None = None) -> CycleReport:
        now = time.time() if now is None else now
        report = CycleReport()
        shares = self.shares()
        disks = self.disks()
        self.db.prune(now=now)
        self._reconcile_promoted(shares, disks)

        reason = self.blocked_reason()
        if reason:
            report.skipped_reason = reason
            log.info("not moving files: %s", reason)
        else:
            pools = sorted({s.pool for s in shares if pool_mounted(self.cfg, s.pool, self.require_mounts)})
            if self.cfg.demote_on_pressure:
                for pool in pools:
                    self._demote_pool(pool, shares, disks, report, now)
            self._promote(shares, disks, report, now)

        report.ignore_list_entries = self.write_mover_ignore(shares, now)
        verb = "would move" if self.cfg.dry_run else "moved"
        log.info(
            "cycle done: %s %d file(s) to pool (%s), %d back to array (%s), %d failed; "
            "%d file(s) in mover ignore list",
            verb,
            len(report.promoted),
            human(report.bytes_promoted),
            len(report.demoted),
            human(report.bytes_demoted),
            len(report.failed),
            report.ignore_list_entries,
        )
        return report

    def _reconcile_promoted(self, shares: list[Share], disks: list[str]) -> None:
        """Forget promoted entries that are no longer (only) on their pool."""
        names = {s.name for s in shares}
        for p in self.db.promoted():
            pool_path = self._p(p.pool, p.share, p.relpath)
            if p.share not in names or not os.path.lexists(pool_path):
                self.db.remove_promoted(p.share, p.relpath)

    def _pool_budget(self, share: Share, usage: PoolUsage) -> int:
        limit_used = int(usage.total * self.cfg.cache_max_percent / 100)
        reserve = max(self.cfg.cache_min_free, share.floor)
        return max(0, min(limit_used - usage.used, usage.avail - reserve))

    def _promote(self, shares: list[Share], disks: list[str], report: CycleReport, now: float) -> None:
        cfg = self.cfg
        by_name = {s.name: s for s in shares}
        # Bytes committed this cycle per pool (so dry runs budget correctly too).
        committed: dict[str, int] = {}
        files_left = cfg.max_files_per_run
        bytes_left = cfg.max_bytes_per_run

        for fs in self.db.scores(now=now):
            if self.stop.is_set() or files_left <= 0:
                break
            if fs.score < cfg.min_score:
                break  # sorted by score, nothing hotter follows
            share = by_name.get(fs.share)
            if share is None or self._excluded(fs.relpath):
                continue
            if not pool_mounted(cfg, share.pool, self.require_mounts):
                continue
            pool_share_root = self._p(share.pool, share.name)
            dst = os.path.join(pool_share_root, fs.relpath)
            if os.path.lexists(dst):
                continue  # already on the pool
            located = [d for d in disks if os.path.lexists(self._p(d, share.name, fs.relpath))]
            if len(located) != 1:
                if len(located) > 1:
                    log.warning("%s/%s exists on several disks %s; not touching it", share.name, fs.relpath, located)
                continue
            disk = located[0]
            src_root = self._p(disk, share.name)
            src = os.path.join(src_root, fs.relpath)
            try:
                st = os.lstat(src)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
                continue
            if st.st_size < cfg.min_file_size or (cfg.max_file_size and st.st_size > cfg.max_file_size):
                continue
            if now - st.st_mtime < cfg.min_file_age:
                continue
            if st.st_size > bytes_left:
                continue
            usage = self.usage_fn(self._p(share.pool))
            budget = self._pool_budget(share, usage) - (committed.get(share.pool, 0) if cfg.dry_run else 0)
            if st.st_size > budget:
                log.debug("no room on %s for %s/%s", share.pool, share.name, fs.relpath)
                continue
            # Re-check the gates before every file: the mover or a parity
            # check may have started since the cycle began.
            reason = self.blocked_reason()
            if reason:
                log.info("stopping promotions: %s", reason)
                report.skipped_reason = reason
                break
            if not self._ensure_share_root(share, src_root, pool_share_root):
                continue

            label = f"{share.name}/{fs.relpath}"
            if cfg.dry_run:
                log.info("[dry-run] would promote %s (%s, score %.1f) %s -> %s", label, human(st.st_size), fs.score, disk, share.pool)
                size = st.st_size
            else:
                self.suppress(share.name, fs.relpath)
                try:
                    size = safe_move(
                        src, dst, src_root, pool_share_root,
                        verify=cfg.verify,
                        is_open=self._is_open(share.name, fs.relpath),
                        min_age=cfg.min_file_age,
                        now=now,
                        journal=self.journal,
                    )
                except TransferAborted as exc:
                    log.info("skipped %s: %s", label, exc)
                    self.db.log_action("promote", share.name, fs.relpath, src, dst, st.st_size, "skipped", str(exc))
                    continue
                except OSError as exc:
                    log.error("failed to promote %s: %s", label, exc)
                    self.db.log_action("promote", share.name, fs.relpath, src, dst, st.st_size, "error", str(exc))
                    report.failed.append(label)
                    continue
                finally:
                    self.suppress(share.name, fs.relpath)
                log.info("promoted %s (%s, score %.1f) %s -> %s", label, human(size), fs.score, disk, share.pool)
                self.db.add_promoted(Promoted(share.name, fs.relpath, share.pool, disk, size, time.time()))
                self.db.log_action("promote", share.name, fs.relpath, src, dst, size, "ok")
            committed[share.pool] = committed.get(share.pool, 0) + size
            report.promoted.append(label)
            report.bytes_promoted += size
            files_left -= 1
            bytes_left -= size

    def _ensure_share_root(self, share: Share, src_root: str, pool_share_root: str) -> bool:
        if os.path.isdir(pool_share_root):
            return True
        if fs_type(self._p(share.pool)) == "zfs":
            # Unraid creates a dataset per share on ZFS pools; don't create a
            # plain directory in its place.
            log.warning("share %s has no directory on ZFS pool %s yet; skipping", share.name, share.pool)
            return False
        if self.cfg.dry_run:
            return True
        try:
            ensure_parents(os.path.dirname(src_root), self._p(share.pool), share.name)
            return True
        except (OSError, TransferAborted) as exc:
            log.error("cannot create %s: %s", pool_share_root, exc)
            return False

    def _demote_pool(
        self, pool: str, shares: list[Share], disks: list[str], report: CycleReport, now: float
    ) -> None:
        cfg = self.cfg
        usage = self.usage_fn(self._p(pool))
        limit_used = int(usage.total * cfg.cache_max_percent / 100)
        if usage.used <= limit_used:
            return
        to_free = usage.used - limit_used
        log.info("pool %s is above %.0f%% (%s over); demoting cold files it received", pool, cfg.cache_max_percent, human(to_free))
        by_name = {s.name: s for s in shares}
        candidates = []
        for p in self.db.promoted():
            share = by_name.get(p.share)
            if p.pool != pool or share is None:
                continue
            score = self.db.score_of(p.share, p.relpath, now)
            if score >= cfg.min_score:
                continue  # still hot, keep it
            candidates.append((score, p, share))
        candidates.sort(key=lambda c: c[0])

        for score, p, share in candidates:
            if to_free <= 0 or self.stop.is_set():
                break
            src_root = self._p(pool, share.name)
            src = os.path.join(src_root, p.relpath)
            try:
                st = os.lstat(src)
            except OSError:
                continue
            if any(os.path.lexists(self._p(d, share.name, p.relpath)) for d in disks):
                continue
            disk = self._pick_disk(share, disks, p.origin_disk, st.st_size)
            if disk is None:
                log.warning("no array disk with room for %s/%s", share.name, p.relpath)
                continue
            reason = self.blocked_reason()
            if reason:
                report.skipped_reason = reason
                break
            dst_root = self._p(disk, share.name)
            dst = os.path.join(dst_root, p.relpath)
            label = f"{share.name}/{p.relpath}"
            if cfg.dry_run:
                log.info("[dry-run] would demote %s (%s, score %.1f) %s -> %s", label, human(st.st_size), score, pool, disk)
                size = st.st_size
            else:
                self.suppress(share.name, p.relpath)
                try:
                    size = safe_move(
                        src, dst, src_root, dst_root,
                        verify=cfg.verify,
                        is_open=self._is_open(share.name, p.relpath),
                        min_age=cfg.min_file_age,
                        now=now,
                        journal=self.journal,
                    )
                except TransferAborted as exc:
                    log.info("skipped demoting %s: %s", label, exc)
                    self.db.log_action("demote", share.name, p.relpath, src, dst, st.st_size, "skipped", str(exc))
                    continue
                except OSError as exc:
                    log.error("failed to demote %s: %s", label, exc)
                    self.db.log_action("demote", share.name, p.relpath, src, dst, st.st_size, "error", str(exc))
                    report.failed.append(label)
                    continue
                finally:
                    self.suppress(share.name, p.relpath)
                log.info("demoted %s (%s) %s -> %s", label, human(size), pool, disk)
                self.db.remove_promoted(share.name, p.relpath)
                self.db.log_action("demote", share.name, p.relpath, src, dst, size, "ok")
            report.demoted.append(label)
            report.bytes_demoted += size
            to_free -= size

    def _pick_disk(self, share: Share, disks: list[str], origin: str, size: int) -> str | None:
        """Prefer the disk the file came from; else the allowed disk with most room."""

        def room(d: str) -> int:
            try:
                return self.usage_fn(self._p(d)).avail - share.floor - size
            except OSError:
                return -1

        allowed = [d for d in disks if share.allows_disk(d) and os.path.isdir(self._p(d, share.name))]
        if origin in allowed and room(origin) > 0:
            return origin
        best = max(allowed, key=room, default=None)
        return best if best is not None and room(best) > 0 else None

    # -- mover ignore list -------------------------------------------------

    def write_mover_ignore(self, shares: list[Share], now: float) -> int:
        """Write hot files that are on a pool, so the mover leaves them there.

        Only "yes" shares need this (the mover moves them pool -> array).
        """
        path = self.cfg.mover_ignore_file
        if not path:
            return 0
        if self.cfg.dry_run:
            path += ".dry-run"
        yes_shares = {s.name: s for s in shares if s.use_cache == "yes"}
        lines: list[str] = []
        for fs in self.db.scores(now=now):
            if fs.score < self.cfg.min_score:
                break
            share = yes_shares.get(fs.share)
            if share is None or self._excluded(fs.relpath):
                continue
            pool_path = self._p(share.pool, share.name, fs.relpath)
            if not os.path.isfile(pool_path):
                continue
            if self.cfg.mover_ignore_style in ("pool", "both"):
                lines.append(pool_path)
            if self.cfg.mover_ignore_style in ("user", "both"):
                lines.append(self._p("user", share.name, fs.relpath))
        lines.sort()
        tmp = f"{path}.tmp"
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("".join(f"{line}\n" for line in lines))
        os.replace(tmp, path)
        return len(lines)


def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"
