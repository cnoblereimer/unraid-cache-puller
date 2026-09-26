"""Configuration, read from environment variables (the Unraid template way)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Mapping

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGTP]?)(?:I?B)?\s*$", re.IGNORECASE)
_UNITS = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}


class ConfigError(ValueError):
    pass


def parse_bool(value: str) -> bool:
    v = value.strip().lower()
    if v in ("1", "true", "yes", "on", "y"):
        return True
    if v in ("0", "false", "no", "off", "n", ""):
        return False
    raise ConfigError(f"not a boolean: {value!r}")


def parse_list(value: str) -> list[str]:
    return [x.strip() for x in re.split(r"[,\n]", value) if x.strip()]


def parse_size(value: str) -> int:
    """Parse sizes like ``500M``, ``20G``, ``1.5TB`` or a plain byte count."""
    m = _SIZE_RE.match(value)
    if not m:
        raise ConfigError(f"not a size: {value!r}")
    return int(float(m.group(1)) * _UNITS[m.group(2).upper()])


def parse_hours(value: str) -> frozenset[int] | None:
    """Parse ``"1-6,22-23"`` into a set of hours. Empty means always allowed."""
    if not value.strip():
        return None
    hours: set[int] = set()
    for part in parse_list(value):
        if "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
            if not (0 <= a <= 23 and 0 <= b <= 23):
                raise ConfigError(f"hour out of range: {part!r}")
            # Wrap-around ranges like 22-4 are allowed.
            h = a
            while True:
                hours.add(h)
                if h == b:
                    break
                h = (h + 1) % 24
        else:
            h = int(part)
            if not 0 <= h <= 23:
                raise ConfigError(f"hour out of range: {part!r}")
            hours.add(h)
    return frozenset(hours)


@dataclass
class Config:
    # Paths (inside the container). /mnt must be mounted at /mnt so that the
    # paths written to the mover ignore list are valid on the host.
    mnt_root: str = "/mnt"
    shares_cfg_dir: str = "/boot/config/shares"
    emhttp_dir: str = "/var/local/emhttp"
    config_dir: str = "/config"

    # Behaviour
    dry_run: bool = True
    run_interval: int = 3600
    cleanup_interval: float = 24 * 3600.0  # remove deleted files from the list; 0 = never
    allowed_hours: frozenset[int] | None = None
    include_shares: list[str] = field(default_factory=list)
    exclude_shares: list[str] = field(default_factory=list)
    share_modes: list[str] = field(default_factory=lambda: ["yes", "prefer"])
    exclude_patterns: list[str] = field(default_factory=list)

    # Hotness scoring
    min_score: float = 3.0
    half_life: float = 72 * 3600.0
    access_debounce: float = 900.0

    # File filters
    min_file_size: int = 0
    max_file_size: int = 0  # 0 = unlimited
    min_file_age: float = 3600.0

    # Pool limits
    cache_max_percent: float = 80.0
    cache_min_free: int = 50 * 1024**3
    max_bytes_per_run: int = 200 * 1024**3
    max_files_per_run: int = 1000
    demote_on_pressure: bool = True

    # Safety
    verify: str = "hash"  # "hash" or "size"
    open_file_check: str = "on"  # "on", "auto" or "off"
    mover_pid_files: list[str] = field(default_factory=lambda: ["/proc/1/root/var/run/mover.pid"])
    mover_process_names: list[str] = field(default_factory=lambda: ["mover", "age_mover"])
    skip_during_parity: bool = True
    require_array_state: bool = True
    require_mounts: bool = True  # only disable for testing outside Unraid

    # Mover integration
    mover_ignore_file: str = "/config/mover-ignore.txt"
    mover_ignore_style: str = "pool"  # "pool", "user" or "both"

    # Access tracking backend: "auto" (fanotify, else inotify), "fanotify" or "inotify"
    tracker: str = "auto"

    log_level: str = "INFO"

    @property
    def db_path(self) -> str:
        return os.path.join(self.config_dir, "cache-puller.db")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env
        c = cls()

        def get(name: str) -> str | None:
            v = env.get(name)
            return v if v is not None and v.strip() != "" else None

        str_fields = {
            "MNT_ROOT": "mnt_root",
            "SHARES_CFG_DIR": "shares_cfg_dir",
            "EMHTTP_DIR": "emhttp_dir",
            "CONFIG_DIR": "config_dir",
            "VERIFY": "verify",
            "OPEN_FILE_CHECK": "open_file_check",
            "MOVER_IGNORE_STYLE": "mover_ignore_style",
            "LOG_LEVEL": "log_level",
            "TRACKER": "tracker",
        }
        for key, attr in str_fields.items():
            if (v := get(key)) is not None:
                setattr(c, attr, v.strip())
        # MOVER_IGNORE_FILE may be explicitly empty to disable it.
        if "MOVER_IGNORE_FILE" in env:
            c.mover_ignore_file = env["MOVER_IGNORE_FILE"].strip()

        bool_fields = {
            "DRY_RUN": "dry_run",
            "DEMOTE_ON_PRESSURE": "demote_on_pressure",
            "SKIP_DURING_PARITY": "skip_during_parity",
            "REQUIRE_ARRAY_STATE": "require_array_state",
            "REQUIRE_MOUNTS": "require_mounts",
        }
        for key, attr in bool_fields.items():
            if (v := get(key)) is not None:
                setattr(c, attr, parse_bool(v))

        list_fields = {
            "INCLUDE_SHARES": "include_shares",
            "EXCLUDE_SHARES": "exclude_shares",
            "SHARE_MODES": "share_modes",
            "EXCLUDE_PATTERNS": "exclude_patterns",
            "MOVER_PID_FILES": "mover_pid_files",
            "MOVER_PROCESS_NAMES": "mover_process_names",
        }
        for key, attr in list_fields.items():
            if (v := get(key)) is not None:
                setattr(c, attr, parse_list(v))

        size_fields = {
            "MIN_FILE_SIZE": "min_file_size",
            "MAX_FILE_SIZE": "max_file_size",
            "CACHE_MIN_FREE": "cache_min_free",
            "MAX_BYTES_PER_RUN": "max_bytes_per_run",
        }
        for key, attr in size_fields.items():
            if (v := get(key)) is not None:
                setattr(c, attr, parse_size(v))

        if (v := get("RUN_INTERVAL_MINUTES")) is not None:
            c.run_interval = int(float(v) * 60)
        if (v := get("CLEANUP_INTERVAL_HOURS")) is not None:
            c.cleanup_interval = float(v) * 3600
        if (v := get("ALLOWED_HOURS")) is not None:
            c.allowed_hours = parse_hours(v)
        if (v := get("MIN_SCORE")) is not None:
            c.min_score = float(v)
        if (v := get("HALF_LIFE_HOURS")) is not None:
            c.half_life = float(v) * 3600
        if (v := get("ACCESS_DEBOUNCE_MINUTES")) is not None:
            c.access_debounce = float(v) * 60
        if (v := get("MIN_FILE_AGE_MINUTES")) is not None:
            c.min_file_age = float(v) * 60
        if (v := get("CACHE_MAX_PERCENT")) is not None:
            c.cache_max_percent = float(v)
        if (v := get("MAX_FILES_PER_RUN")) is not None:
            c.max_files_per_run = int(v)

        c.validate()
        return c

    def validate(self) -> None:
        if self.verify not in ("hash", "size"):
            raise ConfigError("VERIFY must be 'hash' or 'size'")
        if self.open_file_check not in ("on", "auto", "off"):
            raise ConfigError("OPEN_FILE_CHECK must be 'on', 'auto' or 'off'")
        if self.tracker not in ("auto", "fanotify", "inotify"):
            raise ConfigError("TRACKER must be 'auto', 'fanotify' or 'inotify'")
        if self.mover_ignore_style not in ("pool", "user", "both"):
            raise ConfigError("MOVER_IGNORE_STYLE must be 'pool', 'user' or 'both'")
        bad = set(self.share_modes) - {"yes", "prefer"}
        if bad:
            raise ConfigError(
                f"SHARE_MODES may only contain 'yes' and 'prefer' (got {sorted(bad)})"
            )
        if not 1 <= self.cache_max_percent <= 99:
            raise ConfigError("CACHE_MAX_PERCENT must be between 1 and 99")
        if self.run_interval < 60:
            raise ConfigError("RUN_INTERVAL_MINUTES must be at least 1")
        if self.cleanup_interval < 0:
            raise ConfigError("CLEANUP_INTERVAL_HOURS can't be negative")
        if 0 < self.cleanup_interval < 3600:
            raise ConfigError("CLEANUP_INTERVAL_HOURS must be 0 (never) or at least 1")
        if self.half_life <= 0:
            raise ConfigError("HALF_LIFE_HOURS must be positive")
        if self.min_score <= 0:
            raise ConfigError("MIN_SCORE must be positive")
