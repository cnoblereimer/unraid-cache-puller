"""Knowledge about the Unraid host: shares, array disks, pools and array state."""

from __future__ import annotations

import glob
import logging
import os
import re
from dataclasses import dataclass, field

from .config import Config, ConfigError, parse_size

log = logging.getLogger(__name__)

_DISK_RE = re.compile(r"^disk\d+$")
_KV_RE = re.compile(r'^\s*([A-Za-z0-9_]+)\s*=\s*"?(.*?)"?\s*$')


def parse_ini(path: str) -> dict[str, str]:
    """Parse Unraid's ``key="value"`` style files (share .cfg, var.ini)."""
    out: dict[str, str] = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.lstrip().startswith(("#", ";", "[")):
                continue
            m = _KV_RE.match(line)
            if m:
                out[m.group(1)] = m.group(2)
    return out


@dataclass
class Share:
    name: str
    use_cache: str  # "no", "yes", "only", "prefer"
    pool: str  # primary pool name
    secondary_pool: str  # non-empty when secondary storage is another pool
    include_disks: list[str] = field(default_factory=list)
    exclude_disks: list[str] = field(default_factory=list)
    floor: int = 0  # minimum free space in bytes

    @property
    def array_is_secondary(self) -> bool:
        """True when primary storage is a pool and the array is secondary."""
        return self.use_cache in ("yes", "prefer") and not self.secondary_pool and bool(self.pool)

    def allows_disk(self, disk: str) -> bool:
        if self.include_disks and disk not in self.include_disks:
            return False
        return disk not in self.exclude_disks


def _parse_floor(value: str) -> int:
    """shareFloor is in KB when it has no unit (Unraid convention)."""
    value = value.strip()
    if not value:
        return 0
    if value.isdigit():
        return int(value) * 1024
    try:
        return parse_size(value)
    except ConfigError:
        log.warning("could not parse shareFloor %r, treating as 0", value)
        return 0


def load_shares(cfg: Config) -> dict[str, Share]:
    shares: dict[str, Share] = {}
    for path in sorted(glob.glob(os.path.join(cfg.shares_cfg_dir, "*.cfg"))):
        name = os.path.basename(path)[: -len(".cfg")]
        try:
            kv = parse_ini(path)
        except OSError as exc:
            log.warning("cannot read share config %s: %s", path, exc)
            continue
        use_cache = kv.get("shareUseCache", "no").strip().lower()
        pool = kv.get("shareCachePool", "").strip()
        if use_cache != "no" and not pool:
            pool = "cache"  # pre-6.12 configs have no shareCachePool
        shares[name] = Share(
            name=name,
            use_cache=use_cache,
            pool=pool,
            secondary_pool=kv.get("shareCachePool2", "").strip(),
            include_disks=[d.strip() for d in kv.get("shareInclude", "").split(",") if d.strip()],
            exclude_disks=[d.strip() for d in kv.get("shareExclude", "").split(",") if d.strip()],
            floor=_parse_floor(kv.get("shareFloor", "")),
        )
    return shares


def managed_shares(cfg: Config, shares: dict[str, Share]) -> list[Share]:
    """Shares this service is allowed to touch."""
    out = []
    for s in shares.values():
        if not s.array_is_secondary:
            continue
        if s.use_cache not in cfg.share_modes:
            continue
        if cfg.include_shares and s.name not in cfg.include_shares:
            continue
        if s.name in cfg.exclude_shares:
            continue
        out.append(s)
    return out


def _is_mount(path: str, require_mounts: bool) -> bool:
    if not os.path.isdir(path):
        return False
    return os.path.ismount(path) if require_mounts else True


def array_disks(cfg: Config, require_mounts: bool = True) -> list[str]:
    """Names of mounted array data disks (``disk1`` ... ``diskN``)."""
    try:
        names = os.listdir(cfg.mnt_root)
    except OSError:
        return []
    disks = [n for n in names if _DISK_RE.match(n)]
    disks = [d for d in disks if _is_mount(os.path.join(cfg.mnt_root, d), require_mounts)]
    return sorted(disks, key=lambda d: int(d[4:]))


def pool_mounted(cfg: Config, pool: str, require_mounts: bool = True) -> bool:
    return _is_mount(os.path.join(cfg.mnt_root, pool), require_mounts)


def fs_type(path: str, mounts_file: str = "/proc/mounts") -> str | None:
    """Filesystem type of the mount containing ``path``."""
    path = os.path.realpath(path)
    best, best_type = "", None
    try:
        with open(mounts_file) as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mnt = parts[1].replace("\\040", " ")
                if (path == mnt or path.startswith(mnt.rstrip("/") + "/")) and len(mnt) > len(best):
                    best, best_type = mnt, parts[2]
    except OSError:
        return None
    return best_type


@dataclass
class ArrayState:
    known: bool
    started: bool
    parity_running: bool
    detail: str


def array_state(cfg: Config) -> ArrayState:
    path = os.path.join(cfg.emhttp_dir, "var.ini")
    try:
        kv = parse_ini(path)
    except OSError as exc:
        return ArrayState(False, False, False, f"cannot read {path}: {exc}")
    md_state = kv.get("mdState", "")
    started = md_state.upper() == "STARTED"

    def as_int(key: str) -> int:
        try:
            return int(kv.get(key, "0") or 0)
        except ValueError:
            return 0

    parity = as_int("mdResyncPos") > 0 or as_int("mdResync") > 0
    return ArrayState(True, started, parity, f"mdState={md_state} mdResyncPos={kv.get('mdResyncPos')}")
