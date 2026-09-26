"""Settings editable from the web UI.

Environment variables provide the defaults; values saved in the UI are
stored in ``/config/settings.json`` (keyed by the environment variable name,
in the same string format) and take precedence.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Mapping

from .config import Config, ConfigError

GROUPS = [
    ("general", "General"),
    ("shares", "Shares"),
    ("hotness", "What counts as frequently used"),
    ("pool", "Cache pool limits"),
    ("files", "File filters"),
    ("safety", "Safety"),
]


@dataclass(frozen=True)
class Field:
    env: str
    attr: str
    label: str
    group: str
    kind: str  # bool, number, size, text, list, shares, choice, multichoice, hours
    help: str
    choices: tuple[str, ...] = ()
    unit: str = ""
    scale: float = 1.0  # config value = number * scale


FIELDS: list[Field] = [
    Field("DRY_RUN", "dry_run", "Dry run", "general", "bool",
          "Only show what would be moved. Turn off to start moving files."),
    Field("RUN_INTERVAL_MINUTES", "run_interval", "Run every", "general", "number",
          "How often to look for files to move.", unit="minutes", scale=60),
    Field("CLEANUP_INTERVAL_HOURS", "cleanup_interval", "Remove deleted files from the list every", "general",
          "number", "Checks every tracked file and forgets the ones that no longer exist on any disk or pool. "
          "Files deleted while the app is running are also removed about a minute after deletion. "
          "0 = never.", unit="hours", scale=3600),
    Field("ALLOWED_HOURS", "allowed_hours", "Allowed hours", "general", "hours",
          "Only move files during these hours, e.g. 1-6 or 22-5. Leave empty for any time."),
    Field("INCLUDE_SHARES", "include_shares", "Only these shares", "shares", "shares",
          "Leave all unticked to manage every share whose secondary storage is the array."),
    Field("EXCLUDE_SHARES", "exclude_shares", "Never these shares", "shares", "shares",
          "These shares are never touched."),
    Field("SHARE_MODES", "share_modes", "Mover directions", "shares", "multichoice",
          "yes = mover moves pool → array (hot files are kept on the pool); "
          "prefer = mover moves array → pool (hot files get there first).",
          choices=("yes", "prefer")),
    Field("MIN_SCORE", "min_score", "Minimum score", "hotness", "number",
          "Roughly how many recent separate accesses a file needs before it is moved."),
    Field("HALF_LIFE_HOURS", "half_life", "Half-life", "hotness", "number",
          "Accesses count half as much after this long.", unit="hours", scale=3600),
    Field("ACCESS_DEBOUNCE_MINUTES", "access_debounce", "Count repeated opens once within", "hotness", "number",
          "A player opening a file several times while streaming counts as one access.",
          unit="minutes", scale=60),
    Field("CACHE_MAX_PERCENT", "cache_max_percent", "Maximum pool usage", "pool", "number",
          "Never fill a pool above this. Above it, files this app moved that have gone cold are moved back.",
          unit="%"),
    Field("CACHE_MIN_FREE", "cache_min_free", "Always keep free", "pool", "size",
          "Minimum free space to leave on the pool, e.g. 50G."),
    Field("DEMOTE_ON_PRESSURE", "demote_on_pressure", "Move cold files back when the pool is full", "pool", "bool",
          "Only files this app moved to the pool are ever moved back."),
    Field("MAX_BYTES_PER_RUN", "max_bytes_per_run", "Max data per run", "pool", "size",
          "Upper limit on data moved per run, e.g. 200G."),
    Field("MAX_FILES_PER_RUN", "max_files_per_run", "Max files per run", "pool", "number",
          "Upper limit on files moved per run."),
    Field("MIN_FILE_SIZE", "min_file_size", "Minimum file size", "files", "size",
          "Skip smaller files. 0 = no minimum."),
    Field("MAX_FILE_SIZE", "max_file_size", "Maximum file size", "files", "size",
          "Skip larger files. 0 = no limit."),
    Field("MIN_FILE_AGE_MINUTES", "min_file_age", "Skip files modified in the last", "files", "number",
          "Files still being written are never moved.", unit="minutes", scale=60),
    Field("EXCLUDE_PATTERNS", "exclude_patterns", "Exclude patterns", "files", "list",
          "One per line. Matched against the path inside the share and the file name, "
          "e.g. *.part or downloads/*"),
    Field("VERIFY", "verify", "Verify copies", "safety", "choice",
          "hash = re-read every copy from disk and compare checksums before deleting the original. "
          "size = compare sizes only (faster).", choices=("hash", "size")),
    Field("MOVER_IGNORE_STYLE", "mover_ignore_style", "Mover ignore list paths", "safety", "choice",
          "Path style written to the mover ignore list: /mnt/<pool>/..., /mnt/user/... or both.",
          choices=("pool", "user", "both")),
]
BY_ENV = {f.env: f for f in FIELDS}


def fmt_size(n: int) -> str:
    for unit, mult in (("T", 1024**4), ("G", 1024**3), ("M", 1024**2), ("K", 1024)):
        if n and n % mult == 0:
            return f"{n // mult}{unit}"
    return str(n)


def _fmt_number(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


def value_of(cfg: Config, f: Field):
    """Current value of a field, in the shape the UI edits it."""
    v = getattr(cfg, f.attr)
    if f.kind == "bool":
        return bool(v)
    if f.kind == "number":
        return float(v) / f.scale
    if f.kind == "size":
        return fmt_size(int(v))
    if f.kind == "hours":
        return _fmt_hours(v)
    if f.kind in ("list", "shares", "multichoice"):
        return list(v)
    return v


def _fmt_hours(hours) -> str:
    if not hours:
        return ""
    hs = sorted(hours)
    parts, start, prev = [], hs[0], hs[0]
    for h in hs[1:] + [None]:
        if h is not None and h == prev + 1:
            prev = h
            continue
        parts.append(str(start) if start == prev else f"{start}-{prev}")
        if h is not None:
            start = prev = h
    return ",".join(parts)


def to_env_string(f: Field, value) -> str:
    """Convert a value from the UI to the env-var string format."""
    if f.kind == "bool":
        if isinstance(value, str):
            return value
        return "true" if value else "false"
    if f.kind == "number":
        return _fmt_number(float(value))
    if f.kind in ("list", "shares", "multichoice"):
        if isinstance(value, str):
            value = [x for x in value.replace(",", "\n").splitlines()]
        items = [str(x).strip() for x in value if str(x).strip()]
        if any("," in x for x in items):
            raise ConfigError(f"{f.label}: entries may not contain commas")
        return ",".join(items)
    return str(value).strip()


class SettingsStore:
    def __init__(self, path: str, base_env: Mapping[str, str] | None = None):
        self.path = path
        self.base_env = dict(os.environ if base_env is None else base_env)

    def overrides(self) -> dict[str, str]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return {}
        return {k: str(v) for k, v in data.items() if k in BY_ENV}

    def load(self) -> Config:
        return Config.from_env({**self.base_env, **self.overrides()})

    def save(self, changes: Mapping[str, object]) -> Config:
        """Validate and persist changed values. Raises ConfigError if invalid."""
        unknown = set(changes) - set(BY_ENV)
        if unknown:
            raise ConfigError(f"unknown setting(s): {', '.join(sorted(unknown))}")
        merged = self.overrides()
        for key, value in changes.items():
            try:
                merged[key] = to_env_string(BY_ENV[key], value)
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"{BY_ENV[key].label}: {exc}") from None
        try:
            cfg = Config.from_env({**self.base_env, **merged})
        except ValueError as exc:  # ConfigError is a ValueError
            raise ConfigError(str(exc)) from None
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(merged, fh, indent=2, sort_keys=True)
        os.replace(tmp, self.path)
        return cfg

    def reset(self) -> Config:
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        return self.load()

    def describe(self, cfg: Config) -> dict:
        overridden = set(self.overrides())
        return {
            "groups": [{"key": k, "label": l} for k, l in GROUPS],
            "fields": [
                {
                    "key": f.env,
                    "label": f.label,
                    "group": f.group,
                    "kind": f.kind,
                    "help": f.help,
                    "choices": list(f.choices),
                    "unit": f.unit,
                    "value": value_of(cfg, f),
                    "customized": f.env in overridden,
                }
                for f in FIELDS
            ],
        }
