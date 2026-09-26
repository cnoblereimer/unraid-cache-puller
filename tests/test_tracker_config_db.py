import os
import time

import pytest

from cachepuller.config import Config, ConfigError, parse_hours, parse_size
from cachepuller.db import Database, decayed
from cachepuller.safety import mover_running
from cachepuller.tracker import AccessTracker
from cachepuller.unraid import load_shares


def test_parse_size():
    assert parse_size("0") == 0
    assert parse_size("512") == 512
    assert parse_size("10G") == 10 * 1024**3
    assert parse_size("1.5TB") == int(1.5 * 1024**4)
    assert parse_size("500 MiB") == 500 * 1024**2
    with pytest.raises(ConfigError):
        parse_size("lots")


def test_parse_hours():
    assert parse_hours("") is None
    assert parse_hours("1-3,5") == {1, 2, 3, 5}
    assert parse_hours("22-1") == {22, 23, 0, 1}
    with pytest.raises(ConfigError):
        parse_hours("25")


def test_config_from_env():
    cfg = Config.from_env(
        {
            "DRY_RUN": "false",
            "INCLUDE_SHARES": "media, tv",
            "CACHE_MIN_FREE": "20G",
            "RUN_INTERVAL_MINUTES": "30",
            "MOVER_IGNORE_FILE": "",
            "HALF_LIFE_HOURS": "24",
        }
    )
    assert cfg.dry_run is False
    assert cfg.include_shares == ["media", "tv"]
    assert cfg.cache_min_free == 20 * 1024**3
    assert cfg.run_interval == 1800
    assert cfg.mover_ignore_file == ""
    assert cfg.half_life == 86400
    assert Config.from_env({}).dry_run is True
    with pytest.raises(ConfigError):
        Config.from_env({"SHARE_MODES": "only"})


def test_share_floor_and_disks(unraid):
    with open(os.path.join(unraid.shares_cfg_dir, "media.cfg"), "a") as fh:
        fh.write('shareFloor="1000"\nshareInclude="disk1,disk3"\n')
    s = load_shares(unraid)["media"]
    assert s.floor == 1000 * 1024
    assert s.allows_disk("disk1") and not s.allows_disk("disk2")


def test_decay():
    assert decayed(4.0, 0, 100, 100) == pytest.approx(2.0)
    assert decayed(4.0, 100, 50, 100) == 4.0


def test_db_scores_and_rename(tmp_path):
    db = Database(str(tmp_path / "db.sqlite"), half_life=3600)
    now = time.time()
    db.record_hits({("media", "a.mkv"): [now - 3600, now]})
    assert db.score_of("media", "a.mkv", now) == pytest.approx(1.5)
    db.rename("media", "a.mkv", "b.mkv")
    assert db.score_of("media", "a.mkv", now) == 0
    assert db.scores(now=now)[0].relpath == "b.mkv"
    assert db.prune(now=now + 3600 * 20) == 1
    db.close()


def test_mover_detection_by_process_name(tmp_path):
    proc = tmp_path / "proc"
    (proc / "42").mkdir(parents=True)
    (proc / "42" / "cmdline").write_bytes(b"/bin/bash\0/usr/local/sbin/mover\0start\0")
    assert "42" in mover_running([], ["mover"], proc=str(proc))
    assert mover_running([], ["age_mover"], proc=str(proc)) is None


def test_tracker_counts_opens_and_follows_new_dirs(tmp_path):
    root = tmp_path / "disk1" / "media"
    (root / "tv").mkdir(parents=True)
    f = root / "tv" / "e1.mkv"
    f.write_bytes(b"x")
    db = Database(str(tmp_path / "db.sqlite"), half_life=3600)
    tr = AccessTracker(db, debounce=0, mode="inotify")
    tr.sync_roots({str(root): "media"})
    tr.wait_for_scans()
    assert tr.watch_count == 2
    assert tr.state()["mode"] == "inotify"

    open(f, "rb").close()
    tr.poll(1)
    (root / "new").mkdir()
    tr.poll(1)
    g = root / "new" / "x.mkv"
    g.write_bytes(b"y")
    open(g, "rb").close()
    tr.poll(1)
    tr.suppress("media", "tv/e1.mkv")
    open(f, "rb").close()
    tr.poll(1)
    tr.flush()

    scores = {s.relpath: s.hits for s in db.scores()}
    assert scores["tv/e1.mkv"] == 1
    assert "new/x.mkv" in scores
    tr.stop()
    db.close()


def test_tracker_debounce_and_rename(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    f = root / "a.mkv"
    f.write_bytes(b"x")
    db = Database(str(tmp_path / "db.sqlite"), half_life=3600)
    tr = AccessTracker(db, debounce=3600, mode="inotify")
    tr.sync_roots({str(root): "media"})
    tr.wait_for_scans()
    for _ in range(3):
        open(f, "rb").close()
    tr.poll(1)
    tr.flush()
    assert db.scores()[0].hits == 1
    os.rename(f, root / "b.mkv")
    tr.poll(1)
    tr.flush()
    assert [s.relpath for s in db.scores()] == ["b.mkv"]
    tr.stop()
    db.close()


def test_inotify_scans_disks_in_background(tmp_path):
    roots = {}
    for d in ("disk1", "disk2"):
        root = tmp_path / d / "media"
        for i in range(30):
            (root / f"dir{i}" / "sub").mkdir(parents=True)
        roots[str(root)] = "media"
    db = Database(str(tmp_path / "db.sqlite"), half_life=3600)
    tr = AccessTracker(db, debounce=0, mode="inotify")
    tr.sync_roots(roots)
    tr.wait_for_scans()
    st = tr.state()
    assert not st["scanning"] and st["inotify_roots"] == 2
    assert tr.watch_count == 2 * (1 + 30 * 2)
    tr.stop()
    db.close()


def test_inotify_reports_deleted_files(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    (root / "a.mkv").write_bytes(b"x")
    db = Database(str(tmp_path / "db.sqlite"), half_life=3600)
    tr = AccessTracker(db, debounce=0, mode="inotify")
    tr.sync_roots({str(root): "media"})
    tr.wait_for_scans()
    os.unlink(root / "a.mkv")
    tr.poll(1)
    assert tr.take_deleted(60) == []  # not due yet
    assert tr.take_deleted(0) == [("media", "a.mkv")]
    assert tr.take_deleted(0) == []
    tr.stop()
    db.close()
