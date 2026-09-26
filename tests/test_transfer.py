import os

import pytest

from cachepuller.transfer import Journal, TransferAborted, safe_move
from conftest import write


@pytest.fixture
def roots(tmp_path):
    src_root = tmp_path / "disk1" / "media"
    dst_root = tmp_path / "cache" / "media"
    src_root.mkdir(parents=True)
    dst_root.mkdir(parents=True)
    return str(src_root), str(dst_root)


def test_moves_file_and_preserves_metadata(roots):
    src_root, dst_root = roots
    src = write(os.path.join(src_root, "Movies", "A (2020)", "a.mkv"), os.urandom(3 * 1024 * 1024 + 7), mtime=1_600_000_000)
    os.chmod(os.path.dirname(src), 0o775)
    os.chmod(src, 0o640)
    dst = os.path.join(dst_root, "Movies", "A (2020)", "a.mkv")
    data = open(src, "rb").read()

    size = safe_move(src, dst, src_root, dst_root)

    assert size == len(data)
    assert not os.path.exists(src)
    assert open(dst, "rb").read() == data
    st = os.stat(dst)
    assert st.st_mtime == 1_600_000_000
    assert st.st_mode & 0o777 == 0o640
    assert os.stat(os.path.dirname(dst)).st_mode & 0o777 == 0o775
    assert not [n for n in os.listdir(os.path.dirname(dst)) if n.startswith(".cachepuller.")]


def test_refuses_hardlinked_file(roots):
    src_root, dst_root = roots
    src = write(os.path.join(src_root, "a.mkv"))
    os.link(src, os.path.join(src_root, "b.mkv"))
    with pytest.raises(TransferAborted, match="hard links"):
        safe_move(src, os.path.join(dst_root, "a.mkv"), src_root, dst_root)
    assert os.path.exists(src)
    assert not os.path.exists(os.path.join(dst_root, "a.mkv"))


def test_refuses_existing_destination(roots):
    src_root, dst_root = roots
    src = write(os.path.join(src_root, "a.mkv"), b"new")
    dst = write(os.path.join(dst_root, "a.mkv"), b"old")
    with pytest.raises(TransferAborted, match="already exists"):
        safe_move(src, dst, src_root, dst_root)
    assert open(dst, "rb").read() == b"old"
    assert os.path.exists(src)


def test_refuses_open_file(roots):
    src_root, dst_root = roots
    src = write(os.path.join(src_root, "a.mkv"))
    with pytest.raises(TransferAborted, match="open"):
        safe_move(src, os.path.join(dst_root, "a.mkv"), src_root, dst_root, is_open=lambda p: True)
    assert os.path.exists(src)


def test_refuses_recently_modified(roots):
    src_root, dst_root = roots
    src = write(os.path.join(src_root, "a.mkv"))
    with pytest.raises(TransferAborted, match="recently"):
        safe_move(src, os.path.join(dst_root, "a.mkv"), src_root, dst_root, min_age=3600)


def test_source_modified_during_copy_is_rolled_back(roots):
    src_root, dst_root = roots
    src = write(os.path.join(src_root, "a.mkv"), b"a" * 100, mtime=1_600_000_000)
    dst = os.path.join(dst_root, "a.mkv")
    calls = []

    def is_open(path):
        calls.append(path)
        if len(calls) == 2:  # the check after copying: simulate a writer
            with open(src, "ab") as fh:
                fh.write(b"more")
        return False

    with pytest.raises(TransferAborted, match="changed"):
        safe_move(src, dst, src_root, dst_root, is_open=is_open)
    assert open(src, "rb").read() == b"a" * 100 + b"more"
    assert not os.path.exists(dst)
    assert os.listdir(dst_root) == []


def test_opened_right_before_delete_keeps_source(roots):
    src_root, dst_root = roots
    src = write(os.path.join(src_root, "a.mkv"), b"abc", mtime=1_600_000_000)
    dst = os.path.join(dst_root, "a.mkv")
    calls = []

    def is_open(path):
        calls.append(path)
        return len(calls) == 3  # opened after publishing, before unlinking src

    with pytest.raises(TransferAborted):
        safe_move(src, dst, src_root, dst_root, is_open=is_open)
    assert os.path.exists(src)
    assert not os.path.exists(dst)


def test_mismatched_roots_rejected(roots):
    src_root, dst_root = roots
    src = write(os.path.join(src_root, "a.mkv"))
    with pytest.raises(ValueError):
        safe_move(src, os.path.join(dst_root, "b.mkv"), src_root, dst_root)


def test_journal_recovers_temp_file(tmp_path):
    tmp = tmp_path / ".cachepuller.abc.a.mkv"
    tmp.write_bytes(b"partial")
    j = Journal(str(tmp_path / "journal"))
    j.record(str(tmp))
    assert j.recover() == [str(tmp)]
    assert not tmp.exists()
    assert not (tmp_path / "journal").exists()


def test_journal_never_removes_non_temp_files(tmp_path):
    victim = tmp_path / "important.mkv"
    victim.write_bytes(b"data")
    j = Journal(str(tmp_path / "journal"))
    j.record(str(victim))
    assert j.recover() == []
    assert victim.exists()
