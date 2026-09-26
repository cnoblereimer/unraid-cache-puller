import os

import pytest

from cachepuller.config import Config
from cachepuller.db import Database


class FakeOpenChecker:
    usable = True
    enabled = True

    def __init__(self):
        self.open_paths: set[str] = set()

    def is_open(self, paths):
        return any(p in self.open_paths for p in paths)


@pytest.fixture
def unraid(tmp_path):
    """A fake Unraid layout: two array disks, a cache pool and share configs."""
    mnt = tmp_path / "mnt"
    for d in ("disk1", "disk2", "cache", "user"):
        (mnt / d).mkdir(parents=True)
    shares = tmp_path / "shares"
    shares.mkdir()
    (shares / "media.cfg").write_text('shareUseCache="yes"\nshareCachePool="cache"\nshareFloor="0"\n')
    (shares / "games.cfg").write_text('shareUseCache="prefer"\nshareCachePool="cache"\n')
    (shares / "backup.cfg").write_text('shareUseCache="no"\n')
    (shares / "appdata.cfg").write_text('shareUseCache="only"\nshareCachePool="cache"\n')
    (shares / "p2p.cfg").write_text('shareUseCache="yes"\nshareCachePool="cache"\nshareCachePool2="fast"\n')
    for s in ("media", "games", "backup"):
        (mnt / "disk1" / s).mkdir()
    (mnt / "disk2" / "media").mkdir()
    (mnt / "cache" / "media").mkdir()
    emhttp = tmp_path / "emhttp"
    emhttp.mkdir()
    (emhttp / "var.ini").write_text('mdState="STARTED"\nmdResyncPos="0"\nmdResync="0"\n')
    config = tmp_path / "config"
    config.mkdir()

    cfg = Config(
        mnt_root=str(mnt),
        shares_cfg_dir=str(shares),
        emhttp_dir=str(emhttp),
        config_dir=str(config),
        dry_run=False,
        min_file_age=0,
        cache_min_free=0,
        cache_max_percent=80,
        min_score=2.0,
        mover_pid_files=[str(tmp_path / "mover.pid")],
        mover_process_names=[],
        mover_ignore_file=str(config / "mover-ignore.txt"),
    )
    return cfg


@pytest.fixture
def db(unraid):
    d = Database(unraid.db_path, unraid.half_life)
    yield d
    d.close()


def write(path, data=b"x" * 1000, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path
