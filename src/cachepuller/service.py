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
from .db import Database, Promoted, is_hot
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
class Gate:
    key: str
    label: str
    ok: bool
    detail: str


@dataclass
class FileCheck:
    """Where a tracked file is and whether it can be promoted."""

    state: str  # "pool", "candidate", "cold", "blocked", "missing"
    reason: str
    location: str  # pool name, disk name, "several disks" or ""
    size: int = 0
    src: str = ""
    src_root: str = ""
    dst: str = ""
    dst_root: str = ""


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

    def gates(self) -> list[Gate]:
        """Every safety condition, in order, with whether it currently passes."""
        cfg = self.cfg
        out: list[Gate] = []
        hour_ok = cfg.allowed_hours is None or time.localtime().tm_hour in cfg.allowed_hours
        out.append(Gate("hours", "Allowed hours", hour_ok,
                        "any time" if cfg.allowed_hours is None else
                        ("inside the allowed hours" if hour_ok else "outside ALLOWED_HOURS")))
        st = array_state(cfg)
        if not st.known:
            out.append(Gate("array", "Array started", not cfg.require_array_state,
                            f"array state unknown ({st.detail})"))
        else:
            out.append(Gate("array", "Array started", st.started,
                            "started" if st.started else f"array not started ({st.detail})"))
            parity_ok = not (st.parity_running and cfg.skip_during_parity)
            out.append(Gate("parity", "No parity operation", parity_ok,
                            "parity check/rebuild in progress" if st.parity_running else "idle"))
        mover = mover_running(cfg.mover_pid_files, cfg.mover_process_names)
        out.append(Gate("mover", "Mover idle", mover is None,
                        f"mover is running ({mover})" if mover else "idle"))
        out.append(Gate("openfiles", "Open-file check", self.open_checker.usable,
                        ("active" if getattr(self.open_checker, "enabled", True) else "disabled")
                        if self.open_checker.usable else
                        "open-file check unavailable (need --pid=host and SYS_PTRACE)"))
        return out

    def blocked_reason(self) -> str | None:
        """Why moving files is not safe right now (None if it is)."""
        for g in self.gates():
            if not g.ok:
                return g.detail
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

    def check_file(
        self, share: Share, rel: str, score: float, disks: list[str], now: float | None = None
    ) -> FileCheck:
        cfg = self.cfg
        now = time.time() if now is None else now
        pool_share_root = self._p(share.pool, share.name)
        dst = os.path.join(pool_share_root, rel)
        if os.path.lexists(dst):
            return FileCheck("pool", "on the pool", share.pool)
        located = [d for d in disks if os.path.lexists(self._p(d, share.name, rel))]
        if not located:
            return FileCheck("missing", "not found (deleted or renamed)", "")
        if len(located) > 1:
            return FileCheck("blocked", f"exists on several disks ({', '.join(located)})", "several disks")
        disk = located[0]
        src_root = self._p(disk, share.name)
        src = os.path.join(src_root, rel)
        try:
            st = os.lstat(src)
        except OSError:
            return FileCheck("missing", "not found", disk)

        def blocked(reason: str) -> FileCheck:
            return FileCheck("blocked", reason, disk, st.st_size)

        if self._excluded(rel):
            return blocked("matches an exclude pattern")
        if not stat.S_ISREG(st.st_mode):
            return blocked("not a regular file")
        if st.st_nlink > 1:
            return blocked("hard-linked")
        if st.st_size < cfg.min_file_size:
            return blocked("smaller than the minimum file size")
        if cfg.max_file_size and st.st_size > cfg.max_file_size:
            return blocked("larger than the maximum file size")
        if now - st.st_mtime < cfg.min_file_age:
            return blocked("modified recently")
        if not pool_mounted(cfg, share.pool, self.require_mounts):
            return blocked(f"pool {share.pool} is not mounted")
        if not is_hot(score, cfg.min_score):
            return FileCheck("cold", "not used often enough yet", disk, st.st_size)
        return FileCheck("candidate", "will be moved to the pool", disk, st.st_size,
                         src, src_root, dst, pool_share_root)

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
            if not is_hot(fs.score, cfg.min_score):
                break  # sorted by score, nothing hotter follows
            share = by_name.get(fs.share)
            if share is None:
                continue
            chk = self.check_file(share, fs.relpath, fs.score, disks, now)
            if chk.state != "candidate":
                if chk.state == "blocked" and "several disks" in chk.reason:
                    log.warning("%s/%s %s; not touching it", share.name, fs.relpath, chk.reason)
                continue
            file_size = chk.size
            if file_size > bytes_left:
                continue
            usage = self.usage_fn(self._p(share.pool))
            budget = self._pool_budget(share, usage) - (committed.get(share.pool, 0) if cfg.dry_run else 0)
            if file_size > budget:
                log.debug("no room on %s for %s/%s", share.pool, share.name, fs.relpath)
                continue
            # Re-check the gates before every file: the mover or a parity
            # check may have started since the cycle began.
            reason = self.blocked_reason()
            if reason:
                log.info("stopping promotions: %s", reason)
                report.skipped_reason = reason
                break
            size = self._move_to_pool(share, fs.relpath, chk, fs.score, report, now)
            if size is None:
                continue
            committed[share.pool] = committed.get(share.pool, 0) + size
            report.promoted.append(f"{share.name}/{fs.relpath}")
            report.bytes_promoted += size
            files_left -= 1
            bytes_left -= size

    def _move_to_pool(
        self, share: Share, rel: str, chk: FileCheck, score: float, report: CycleReport, now: float
    ) -> int | None:
        """Move one checked candidate to the pool. Returns the size, or None if skipped."""
        cfg = self.cfg
        disk, src_root, src, dst_root, dst = chk.location, chk.src_root, chk.src, chk.dst_root, chk.dst
        if not self._ensure_share_root(share, src_root, dst_root):
            return None
        label = f"{share.name}/{rel}"
        if cfg.dry_run:
            log.info("[dry-run] would promote %s (%s, score %.1f) %s -> %s", label, human(chk.size), score, disk, share.pool)
            return chk.size
        self.suppress(share.name, rel)
        try:
            size = safe_move(
                src, dst, src_root, dst_root,
                verify=cfg.verify,
                is_open=self._is_open(share.name, rel),
                min_age=cfg.min_file_age,
                now=now,
                journal=self.journal,
            )
        except TransferAborted as exc:
            log.info("skipped %s: %s", label, exc)
            self.db.log_action("promote", share.name, rel, src, dst, chk.size, "skipped", str(exc))
            return None
        except OSError as exc:
            log.error("failed to promote %s: %s", label, exc)
            self.db.log_action("promote", share.name, rel, src, dst, chk.size, "error", str(exc))
            report.failed.append(label)
            return None
        finally:
            self.suppress(share.name, rel)
        log.info("promoted %s (%s, score %.1f) %s -> %s", label, human(size), score, disk, share.pool)
        self.db.add_promoted(Promoted(share.name, rel, share.pool, disk, size, time.time()))
        self.db.log_action("promote", share.name, rel, src, dst, size, "ok")
        return size

    def promote_one(self, share_name: str, rel: str, now: float | None = None) -> tuple[bool, str]:
        """Move a single file to the pool on request, whatever its score.

        All safety gates and file checks still apply.
        """
        now = time.time() if now is None else now
        share = next((s for s in self.shares() if s.name == share_name), None)
        if share is None:
            return False, f"share {share_name} is not managed"
        reason = self.blocked_reason()
        if reason:
            return False, f"not safe right now: {reason}"
        chk = self.check_file(share, rel, float("inf"), self.disks(), now)
        if chk.state != "candidate":
            return False, chk.reason
        if chk.size > self._pool_budget(share, self.usage_fn(self._p(share.pool))):
            return False, f"not enough room on pool {share.pool} within the configured limits"
        report = CycleReport()
        size = self._move_to_pool(share, rel, chk, self.db.score_of(share.name, rel, now), report, now)
        if size is None:
            return False, "skipped, see the activity log" if not report.failed else "failed, see the activity log"
        if self.cfg.dry_run:
            return True, f"dry run: would move {human(size)} from {chk.location} to {share.pool}"
        # Keep the ignore list current so the mover doesn't move it straight back.
        self.write_mover_ignore(self.shares(), now)
        return True, f"moved {human(size)} from {chk.location} to {share.pool}"

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
            if is_hot(score, cfg.min_score):
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
            if not is_hot(fs.score, self.cfg.min_score):
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
