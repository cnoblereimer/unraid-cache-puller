"""fanotify tests. They need CAP_SYS_ADMIN and are skipped without it."""

import os
import subprocess
import sys
import time

import pytest

from cachepuller.db import Database
from cachepuller.fanotify import Fanotify
from cachepuller.tracker import AccessTracker


def _fanotify_ok(path):
    try:
        f = Fanotify()
        f.mark(str(path))
        f.close()
        return True
    except OSError:
        return False


@pytest.fixture
def root(tmp_path):
    if not _fanotify_ok(tmp_path):
        pytest.skip("fanotify not available (needs CAP_SYS_ADMIN)")
    r = tmp_path / "disk1" / "media"
    (r / "TV" / "Show").mkdir(parents=True)
    (r / "TV" / "Show" / "e1.mkv").write_bytes(b"x")
    return r


def other_process(code):
    """Accesses made by this process are ignored, so use a child process."""
    subprocess.run([sys.executable, "-c", code], check=True)


def drain(tr, seconds=1.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        tr.poll(0.1)
    tr.flush()


def test_counts_opens_without_scanning(root, tmp_path):
    db = Database(str(tmp_path / "db.sqlite"), half_life=3600)
    tr = AccessTracker(db, debounce=0, mode="fanotify")
    tr.sync_roots({str(root): "media"})
    st = tr.state()
    assert st["mode"] == "fanotify" and st["watches"] == 0 and not st["scanning"]

    # A folder created after startup needs no watch either.
    (root / "Movies" / "New").mkdir(parents=True)
    (root / "Movies" / "New" / "m.mkv").write_bytes(b"y")
    other_process(
        f"open({str(root / 'TV/Show/e1.mkv')!r}).close();"
        f"open({str(root / 'Movies/New/m.mkv')!r}).close();"
        f"open({str(root / 'TV/Show/e1.mkv')!r}).close()"
    )
    # Our own opens (copying, hashing) are ignored.
    open(root / "TV" / "Show" / "e1.mkv").close()
    drain(tr)
    # (fanotify may merge identical queued events, so only check which files.)
    assert {s.relpath for s in db.scores()} == {"TV/Show/e1.mkv", "Movies/New/m.mkv"}
    tr.stop()
    db.close()


def test_ignores_files_outside_roots(root, tmp_path):
    other = tmp_path / "disk1" / "appdata"
    other.mkdir()
    (other / "db.sqlite").write_bytes(b"z")
    db = Database(str(tmp_path / "db.sqlite"), half_life=3600)
    tr = AccessTracker(db, debounce=0, mode="fanotify")
    tr.sync_roots({str(root): "media"})
    other_process(f"open({str(other / 'db.sqlite')!r}).close()")
    drain(tr)
    assert db.scores() == []
    tr.stop()
    db.close()


def test_rename_keeps_score(root, tmp_path):
    db = Database(str(tmp_path / "db.sqlite"), half_life=3600)
    tr = AccessTracker(db, debounce=0, mode="fanotify")
    tr.sync_roots({str(root): "media"})
    old, new = root / "TV/Show/e1.mkv", root / "TV/Show/S01E01.mkv"
    other_process(f"import os; open({str(old)!r}).close(); os.rename({str(old)!r}, {str(new)!r})")
    drain(tr)
    assert [(s.relpath, s.hits) for s in db.scores()] == [("TV/Show/S01E01.mkv", 1)]
    tr.stop()
    db.close()


def test_does_not_keep_files_open_on_disk(root, tmp_path):
    """An open fd on a disk would stop Unraid from stopping the array."""
    db = Database(str(tmp_path / "db.sqlite"), half_life=3600)
    tr = AccessTracker(db, debounce=0, mode="fanotify")
    tr.sync_roots({str(root): "media"})
    other_process(f"open({str(root / 'TV/Show/e1.mkv')!r}).close()")
    drain(tr)
    disk = str(tmp_path / "disk1")
    open_paths = []
    for fd in os.listdir("/proc/self/fd"):
        try:
            open_paths.append(os.readlink(f"/proc/self/fd/{fd}"))
        except OSError:
            pass
    assert not [p for p in open_paths if p.startswith(disk)]
    tr.stop()
    db.close()
