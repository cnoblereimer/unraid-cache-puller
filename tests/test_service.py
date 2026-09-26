import os
import time

import pytest

from cachepuller.db import Promoted
from cachepuller.service import PoolUsage, Service
from cachepuller.unraid import load_shares, managed_shares
from conftest import FakeOpenChecker, write

GiB = 1024**3


def make_service(cfg, db, usage=None, checker=None):
    usage = usage or (lambda path: PoolUsage(total=100 * GiB, used=10 * GiB, avail=90 * GiB))
    return Service(cfg, db, open_checker=checker or FakeOpenChecker(), require_mounts=False, usage_fn=usage)


def hot(db, share, rel, hits=3, now=None):
    now = now or time.time()
    db.record_hits({(share, rel): [now - i for i in range(hits)]})


def test_only_shares_with_array_as_secondary_are_managed(unraid):
    names = sorted(s.name for s in managed_shares(unraid, load_shares(unraid)))
    assert names == ["games", "media"]


def test_share_filters(unraid):
    unraid.share_modes = ["yes"]
    assert [s.name for s in managed_shares(unraid, load_shares(unraid))] == ["media"]
    unraid.share_modes = ["yes", "prefer"]
    unraid.exclude_shares = ["media"]
    assert [s.name for s in managed_shares(unraid, load_shares(unraid))] == ["games"]


def test_promotes_hot_file_and_writes_ignore_list(unraid, db):
    mnt = unraid.mnt_root
    write(f"{mnt}/disk2/media/tv/show/e1.mkv", mtime=time.time() - 7200)
    write(f"{mnt}/disk2/media/tv/show/e2.mkv", mtime=time.time() - 7200)
    hot(db, "media", "tv/show/e1.mkv", hits=3)
    hot(db, "media", "tv/show/e2.mkv", hits=1)  # below MIN_SCORE

    report = make_service(unraid, db).run_cycle()

    assert report.skipped_reason is None
    assert report.promoted == ["media/tv/show/e1.mkv"]
    assert os.path.exists(f"{mnt}/cache/media/tv/show/e1.mkv")
    assert not os.path.exists(f"{mnt}/disk2/media/tv/show/e1.mkv")
    assert os.path.exists(f"{mnt}/disk2/media/tv/show/e2.mkv")
    assert [p.relpath for p in db.promoted()] == ["tv/show/e1.mkv"]
    assert db.promoted()[0].origin_disk == "disk2"
    ignore = open(unraid.mover_ignore_file).read().splitlines()
    assert ignore == [f"{mnt}/cache/media/tv/show/e1.mkv"]


def test_dry_run_changes_nothing(unraid, db):
    unraid.dry_run = True
    mnt = unraid.mnt_root
    write(f"{mnt}/disk1/media/a.mkv")
    hot(db, "media", "a.mkv")
    report = make_service(unraid, db).run_cycle()
    assert report.promoted == ["media/a.mkv"]
    assert os.path.exists(f"{mnt}/disk1/media/a.mkv")
    assert not os.path.exists(f"{mnt}/cache/media/a.mkv")
    assert not os.path.exists(unraid.mover_ignore_file)
    assert os.path.exists(unraid.mover_ignore_file + ".dry-run")
    assert db.promoted() == []


@pytest.mark.parametrize(
    "setup, reason",
    [
        (lambda cfg, tmp: open(f"{cfg.emhttp_dir}/var.ini", "w").write('mdState="STOPPED"\n'), "array not started"),
        (lambda cfg, tmp: open(f"{cfg.emhttp_dir}/var.ini", "w").write('mdState="STARTED"\nmdResyncPos="1234"\n'), "parity"),
        (lambda cfg, tmp: open(cfg.mover_pid_files[0], "w").write(str(os.getpid())), "mover is running"),
        (lambda cfg, tmp: os.unlink(f"{cfg.emhttp_dir}/var.ini"), "array state unknown"),
    ],
)
def test_gates_block_moves(unraid, db, setup, reason, tmp_path):
    mnt = unraid.mnt_root
    write(f"{mnt}/disk1/media/a.mkv")
    hot(db, "media", "a.mkv")
    setup(unraid, tmp_path)
    report = make_service(unraid, db).run_cycle()
    assert reason in report.skipped_reason
    assert report.promoted == []
    assert os.path.exists(f"{mnt}/disk1/media/a.mkv")


def test_unusable_open_checker_blocks_moves(unraid, db):
    checker = FakeOpenChecker()
    checker.usable = False
    write(f"{unraid.mnt_root}/disk1/media/a.mkv")
    hot(db, "media", "a.mkv")
    report = make_service(unraid, db, checker=checker).run_cycle()
    assert "open-file check" in report.skipped_reason


def test_skips_open_files_duplicates_hardlinks_and_excludes(unraid, db):
    mnt = unraid.mnt_root
    unraid.exclude_patterns = ["*.part"]
    write(f"{mnt}/disk1/media/open.mkv")
    write(f"{mnt}/disk1/media/dup.mkv")
    write(f"{mnt}/disk2/media/dup.mkv")
    write(f"{mnt}/disk1/media/linked.mkv")
    os.link(f"{mnt}/disk1/media/linked.mkv", f"{mnt}/disk1/media/linked2.mkv")
    write(f"{mnt}/disk1/media/x.part")
    for rel in ("open.mkv", "dup.mkv", "linked.mkv", "x.part"):
        hot(db, "media", rel)
    checker = FakeOpenChecker()
    checker.open_paths.add(f"{mnt}/user/media/open.mkv")

    report = make_service(unraid, db, checker=checker).run_cycle()

    assert report.promoted == []
    for rel in ("open.mkv", "dup.mkv", "linked.mkv", "x.part"):
        assert os.path.exists(f"{mnt}/disk1/media/{rel}")
        assert not os.path.exists(f"{mnt}/cache/media/{rel}")


def test_respects_pool_budget(unraid, db):
    mnt = unraid.mnt_root
    write(f"{mnt}/disk1/media/big.mkv", b"x" * 5000)
    write(f"{mnt}/disk1/media/small.mkv", b"x" * 500)
    hot(db, "media", "big.mkv", hits=5)
    hot(db, "media", "small.mkv", hits=3)
    # 80% of 10000 = 8000 allowed; 7000 used => 1000 bytes of budget.
    usage = lambda path: PoolUsage(total=10000, used=7000, avail=3000)
    report = make_service(unraid, db, usage=usage).run_cycle()
    assert report.promoted == ["media/small.mkv"]


def test_limits_per_run(unraid, db):
    mnt = unraid.mnt_root
    unraid.max_files_per_run = 1
    for n in range(3):
        write(f"{mnt}/disk1/media/{n}.mkv")
        hot(db, "media", f"{n}.mkv", hits=3 + n)
    report = make_service(unraid, db).run_cycle()
    assert report.promoted == ["media/2.mkv"]


def test_creates_share_root_on_pool(unraid, db):
    mnt = unraid.mnt_root
    write(f"{mnt}/disk1/games/g/game.bin")
    hot(db, "games", "g/game.bin")
    report = make_service(unraid, db).run_cycle()
    assert report.promoted == ["games/g/game.bin"]
    assert os.path.exists(f"{mnt}/cache/games/g/game.bin")
    # "prefer" shares are moved to the pool by the mover anyway: not in the ignore list
    assert open(unraid.mover_ignore_file).read() == ""


def test_demotes_cold_promoted_files_under_pressure(unraid, db):
    mnt = unraid.mnt_root
    now = time.time()
    write(f"{mnt}/cache/media/cold.mkv", b"c" * 3000)
    write(f"{mnt}/cache/media/warm.mkv", b"w" * 3000)
    write(f"{mnt}/cache/media/user-file.mkv", b"u" * 3000)  # not promoted by us
    db.add_promoted(Promoted("media", "cold.mkv", "cache", "disk2", 3000, now))
    db.add_promoted(Promoted("media", "warm.mkv", "cache", "disk2", 3000, now))
    hot(db, "media", "warm.mkv", hits=5)

    def usage(path):
        if path.endswith("cache"):
            used = sum(os.path.getsize(f"{mnt}/cache/media/{n}") for n in os.listdir(f"{mnt}/cache/media"))
            return PoolUsage(total=10000, used=used, avail=10000 - used)
        return PoolUsage(total=10**12, used=0, avail=10**12)

    report = make_service(unraid, db, usage=usage).run_cycle()

    assert report.demoted == ["media/cold.mkv"]
    assert os.path.exists(f"{mnt}/disk2/media/cold.mkv")
    assert not os.path.exists(f"{mnt}/cache/media/cold.mkv")
    assert os.path.exists(f"{mnt}/cache/media/warm.mkv")
    assert os.path.exists(f"{mnt}/cache/media/user-file.mkv")
    assert [p.relpath for p in db.promoted()] == ["warm.mkv"]


def test_forgets_promoted_files_moved_away_by_mover(unraid, db):
    db.add_promoted(Promoted("media", "gone.mkv", "cache", "disk1", 1, time.time()))
    make_service(unraid, db).run_cycle()
    assert db.promoted() == []


def test_ignore_list_user_style(unraid, db):
    mnt = unraid.mnt_root
    unraid.mover_ignore_style = "both"
    write(f"{mnt}/cache/media/a.mkv")
    hot(db, "media", "a.mkv")
    make_service(unraid, db).run_cycle()
    assert open(unraid.mover_ignore_file).read().splitlines() == [
        f"{mnt}/cache/media/a.mkv",
        f"{mnt}/user/media/a.mkv",
    ]
