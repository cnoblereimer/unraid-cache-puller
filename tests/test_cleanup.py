import os
import time

import pytest

from cachepuller.daemon import Daemon
from cachepuller.db import Promoted
from cachepuller.service import MASS_MISSING_MIN, PoolUsage, Service
from cachepuller.settings import SettingsStore
from conftest import FakeOpenChecker, write


def make_service(cfg, db):
    return Service(cfg, db, open_checker=FakeOpenChecker(), require_mounts=False,
                   usage_fn=lambda p: PoolUsage(total=10**12, used=0, avail=10**12))


def track(db, share, rels):
    db.record_hits({(share, rel): [time.time()] for rel in rels})


def tracked(db):
    return sorted(f"{f.share}/{f.relpath}" for f in db.scores())


def test_removes_only_files_that_are_gone_everywhere(unraid, db):
    mnt = unraid.mnt_root
    write(f"{mnt}/disk1/media/on-disk1.mkv")
    write(f"{mnt}/disk2/media/on-disk2.mkv")
    write(f"{mnt}/cache/media/on-pool.mkv")
    write(f"{mnt}/disk1/backup/array-only-share.bin")  # share with no pool
    track(db, "media", ["on-disk1.mkv", "on-disk2.mkv", "on-pool.mkv", "deleted.mkv"])
    track(db, "backup", ["array-only-share.bin", "gone.bin"])
    db.add_promoted(Promoted("media", "deleted.mkv", "cache", "disk1", 1, time.time()))

    report = make_service(unraid, db).cleanup_missing()

    assert report.skipped_reason is None
    assert report.removed == 2 and report.checked == 6
    assert tracked(db) == ["backup/array-only-share.bin", "media/on-disk1.mkv",
                           "media/on-disk2.mkv", "media/on-pool.mkv"]
    assert db.promoted() == []


def test_does_nothing_when_array_is_stopped(unraid, db):
    track(db, "media", ["deleted.mkv"])
    with open(f"{unraid.emhttp_dir}/var.ini", "w") as fh:
        fh.write('mdState="STOPPED"\n')
    report = make_service(unraid, db).cleanup_missing()
    assert "array not started" in report.skipped_reason
    assert tracked(db) == ["media/deleted.mkv"]


def test_does_nothing_when_array_state_unknown(unraid, db):
    track(db, "media", ["deleted.mkv"])
    os.unlink(f"{unraid.emhttp_dir}/var.ini")
    report = make_service(unraid, db).cleanup_missing()
    assert "unknown" in report.skipped_reason
    assert tracked(db) == ["media/deleted.mkv"]


def test_skips_share_when_most_files_look_missing(unraid, db):
    """E.g. a disk that isn't mounted: don't wipe the share's history."""
    mnt = unraid.mnt_root
    write(f"{mnt}/disk1/media/still-here.mkv")
    rels = [f"f{i}.mkv" for i in range(MASS_MISSING_MIN + 5)]
    track(db, "media", rels + ["still-here.mkv"])
    report = make_service(unraid, db).cleanup_missing()
    assert report.removed == 0
    assert any("suspicious" in s for s in report.skipped_shares)
    assert len(tracked(db)) == len(rels) + 1


def test_skips_share_when_pool_not_mounted(unraid, db):
    import shutil
    mnt = unraid.mnt_root
    write(f"{mnt}/disk1/media/a.mkv")
    track(db, "media", ["a.mkv", "on-pool.mkv"])
    shutil.rmtree(f"{mnt}/cache")
    report = make_service(unraid, db).cleanup_missing()
    assert report.removed == 0
    assert any("pool cache is not mounted" in s for s in report.skipped_shares)


def test_forgets_files_of_deleted_share(unraid, db):
    track(db, "oldshare", ["x.mkv"])
    write(f"{unraid.mnt_root}/disk1/media/a.mkv")
    track(db, "media", ["a.mkv"])
    report = make_service(unraid, db).cleanup_missing()
    assert report.removed == 1
    assert tracked(db) == ["media/a.mkv"]


def test_no_share_configs_means_no_cleanup(unraid, db):
    for f in os.listdir(unraid.shares_cfg_dir):
        os.unlink(os.path.join(unraid.shares_cfg_dir, f))
    track(db, "media", ["a.mkv"])
    report = make_service(unraid, db).cleanup_missing()
    assert "no share configs" in report.skipped_reason
    assert tracked(db) == ["media/a.mkv"]


def test_forget_if_missing(unraid, db):
    write(f"{unraid.mnt_root}/disk2/media/here.mkv")
    track(db, "media", ["here.mkv", "gone.mkv"])
    svc = make_service(unraid, db)
    assert svc.forget_if_missing("media", "here.mkv") == (False, "the file still exists")
    ok, _ = svc.forget_if_missing("media", "gone.mkv")
    assert ok
    assert tracked(db) == ["media/here.mkv"]


class FakeTracker:
    def __init__(self, deleted):
        self.deleted = deleted

    def take_deleted(self, older_than, now=None):
        out, self.deleted = self.deleted, []
        return out

    def flush(self):
        pass

    def suppress(self, share, rel):
        pass


def test_daemon_checks_deletions_reported_by_tracker(unraid, db):
    mnt = unraid.mnt_root
    # The mover deleted the pool copy after copying it to the array: keep it.
    write(f"{mnt}/disk1/media/moved-by-mover.mkv")
    track(db, "media", ["moved-by-mover.mkv", "really-deleted.mkv"])
    tracker = FakeTracker([("media", "moved-by-mover.mkv"), ("media", "really-deleted.mkv"),
                           ("media", "never-tracked.mkv")])
    store = SettingsStore(os.path.join(unraid.config_dir, "settings.json"), base_env={})
    d = Daemon(unraid, db, store, tracker=tracker, service=make_service(unraid, db))
    assert d.check_deleted() == 1
    assert tracked(db) == ["media/moved-by-mover.mkv"]
    assert d.cleanup_state()["removed_on_delete"] == 1


def test_daemon_cleanup_runs_in_background(unraid, db):
    track(db, "media", ["gone.mkv"])
    write(f"{unraid.mnt_root}/disk1/media/a.mkv")
    track(db, "media", ["a.mkv"])
    store = SettingsStore(os.path.join(unraid.config_dir, "settings.json"), base_env={})
    d = Daemon(unraid, db, store, service=make_service(unraid, db))
    d.request_cleanup()
    deadline = time.time() + 5
    while d.cleanup_running and time.time() < deadline:
        time.sleep(0.01)
    state = d.cleanup_state()
    assert state["last"]["removed"] == 1
    assert state["next_at"] > time.time()
    assert tracked(db) == ["media/a.mkv"]


def test_cleanup_interval_setting():
    from cachepuller.config import Config, ConfigError
    assert Config.from_env({}).cleanup_interval == 86400
    assert Config.from_env({"CLEANUP_INTERVAL_HOURS": "0"}).cleanup_interval == 0
    with pytest.raises(ConfigError):
        Config.from_env({"CLEANUP_INTERVAL_HOURS": "0.5"})
