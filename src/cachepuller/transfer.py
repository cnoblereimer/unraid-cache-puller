"""Crash-safe, verify-before-delete file moves between filesystems.

Sequence for moving ``src`` (e.g. /mnt/disk3/media/a.mkv) to ``dst``
(e.g. /mnt/cache/media/a.mkv):

1. Snapshot the source (inode, size, mtime, ctime).
2. Create missing parent directories on the destination, copying owner and
   mode from the source side.
3. Copy into a hidden temp file next to ``dst`` (O_EXCL), hashing while
   reading, then fsync, drop it from the page cache and copy owner, mode,
   xattrs and timestamps.
4. Verify: re-read the temp file from disk and compare the hash (or only
   the size with VERIFY=size).
5. Re-check that the source is unchanged and not open by any process.
6. Publish with link(tmp, dst), which fails instead of overwriting if
   something appeared at ``dst`` in the meantime; then remove tmp.
7. Re-check the source one last time, then unlink it. If anything changed,
   the new copy is removed again and the source stays untouched.

At no point is there a moment where the file exists in neither location. A
crash leaves at worst a ``.cachepuller.*`` temp file (cleaned up on start)
or the file on both disks with identical content; Unraid's shfs serves the
pool copy first in that case.
"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import secrets
import stat
import time
from dataclasses import dataclass
from typing import Callable

from .tracker import TEMP_PREFIX

log = logging.getLogger(__name__)

CHUNK = 8 * 1024 * 1024


class TransferAborted(Exception):
    """The move was skipped for a safety reason; nothing was changed."""


@dataclass(frozen=True)
class Snapshot:
    dev: int
    ino: int
    size: int
    mtime_ns: int
    ctime_ns: int
    nlink: int
    mode: int

    @classmethod
    def of(cls, path: str) -> "Snapshot":
        st = os.lstat(path)
        return cls(st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_nlink, st.st_mode)

    def same_file_unchanged(self, other: "Snapshot") -> bool:
        return (self.dev, self.ino, self.size, self.mtime_ns, self.ctime_ns) == (
            other.dev,
            other.ino,
            other.size,
            other.mtime_ns,
            other.ctime_ns,
        )


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _copy_xattrs(src: str, dst: str) -> None:
    try:
        names = os.listxattr(src, follow_symlinks=False)
    except OSError:
        return
    for name in names:
        try:
            os.setxattr(dst, name, os.getxattr(src, name, follow_symlinks=False), follow_symlinks=False)
        except OSError as exc:
            if exc.errno not in (errno.ENOTSUP, errno.EPERM, errno.EOPNOTSUPP):
                log.debug("xattr %s not copied to %s: %s", name, dst, exc)


def _copy_owner_mode(src_st: os.stat_result, dst: str) -> None:
    try:
        os.chown(dst, src_st.st_uid, src_st.st_gid, follow_symlinks=False)
    except PermissionError:
        log.warning("cannot chown %s (not running as root?)", dst)
    os.chmod(dst, stat.S_IMODE(src_st.st_mode))


def ensure_parents(src_root: str, dst_root: str, reldir: str) -> None:
    """Create ``dst_root/reldir`` mirroring owner/mode of ``src_root/reldir``."""
    if not os.path.isdir(dst_root):
        raise TransferAborted(f"destination share root {dst_root} does not exist")
    if not reldir:
        return
    cur_src, cur_dst = src_root, dst_root
    for part in reldir.split("/"):
        cur_src = os.path.join(cur_src, part)
        cur_dst = os.path.join(cur_dst, part)
        if os.path.isdir(cur_dst) and not os.path.islink(cur_dst):
            continue
        if os.path.lexists(cur_dst):
            raise TransferAborted(f"{cur_dst} exists and is not a directory")
        src_st = os.stat(cur_src, follow_symlinks=False)
        try:
            os.mkdir(cur_dst, 0o700)
        except FileExistsError:
            if not os.path.isdir(cur_dst):
                raise
            continue
        _copy_owner_mode(src_st, cur_dst)
        _copy_xattrs(cur_src, cur_dst)
        os.utime(cur_dst, ns=(src_st.st_atime_ns, src_st.st_mtime_ns), follow_symlinks=False)


def _hash_file(path: str) -> bytes:
    h = hashlib.blake2b(digest_size=32)
    with open(path, "rb", buffering=0) as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.digest()


def _copy_data(src: str, tmp: str) -> tuple[bytes, int]:
    """Copy src to a new file tmp, returning (hash, bytes copied)."""
    h = hashlib.blake2b(digest_size=32)
    copied = 0
    sfd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        dfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            while True:
                chunk = os.read(sfd, CHUNK)
                if not chunk:
                    break
                h.update(chunk)
                view = memoryview(chunk)
                while view:
                    n = os.write(dfd, view)
                    view = view[n:]
                copied += len(chunk)
            os.fsync(dfd)
            try:  # make the verification read hit the disk, not the page cache
                os.posix_fadvise(dfd, 0, 0, os.POSIX_FADV_DONTNEED)
            except (AttributeError, OSError):
                pass
        finally:
            os.close(dfd)
    finally:
        os.close(sfd)
    return h.digest(), copied


def safe_move(
    src: str,
    dst: str,
    src_root: str,
    dst_root: str,
    *,
    verify: str = "hash",
    is_open: Callable[[str], bool] = lambda p: False,
    min_age: float = 0.0,
    now: float | None = None,
    journal: "Journal | None" = None,
) -> int:
    """Move one regular file between filesystems. Returns the size moved.

    Raises TransferAborted when a safety check fails (nothing changed), or
    OSError on I/O failure (partial state is rolled back).
    """
    now = time.time() if now is None else now
    rel = os.path.relpath(src, src_root)
    if rel.startswith("..") or os.path.relpath(dst, dst_root) != rel:
        raise ValueError(f"src/dst do not match their roots: {src} -> {dst}")

    before = Snapshot.of(src)
    if not stat.S_ISREG(before.mode):
        raise TransferAborted("not a regular file")
    if before.nlink > 1:
        raise TransferAborted(f"file has {before.nlink} hard links; moving would break them")
    if now - before.mtime_ns / 1e9 < min_age:
        raise TransferAborted("modified too recently")
    if os.path.lexists(dst):
        raise TransferAborted(f"destination already exists: {dst}")
    if is_open(src):
        raise TransferAborted("file is open")

    dst_dir = os.path.dirname(dst)
    ensure_parents(src_root, dst_root, os.path.dirname(rel))

    tmp = os.path.join(dst_dir, f"{TEMP_PREFIX}{secrets.token_hex(6)}.{os.path.basename(dst)}"[:255])
    linked = False
    if journal is not None:
        journal.record(tmp)
    try:
        src_st = os.stat(src, follow_symlinks=False)
        digest, copied = _copy_data(src, tmp)
        if copied != before.size:
            raise TransferAborted(f"size changed during copy ({before.size} -> {copied})")
        _copy_owner_mode(src_st, tmp)
        _copy_xattrs(src, tmp)
        os.utime(tmp, ns=(src_st.st_atime_ns, src_st.st_mtime_ns), follow_symlinks=False)

        if os.path.getsize(tmp) != copied:
            raise OSError(errno.EIO, f"verification failed: size mismatch on {tmp}")
        if verify == "hash" and _hash_file(tmp) != digest:
            raise OSError(errno.EIO, f"verification failed: checksum mismatch on {tmp}")

        if not Snapshot.of(src).same_file_unchanged(before):
            raise TransferAborted("source changed during copy")
        if is_open(src):
            raise TransferAborted("file was opened during copy")

        try:
            os.link(tmp, dst)
        except FileExistsError:
            raise TransferAborted(f"destination appeared during copy: {dst}") from None
        linked = True
        os.unlink(tmp)
        _fsync_dir(dst_dir)

        # Last look before the point of no return.
        if not Snapshot.of(src).same_file_unchanged(before) or is_open(src):
            raise TransferAborted("source changed or was opened while publishing")
        os.unlink(src)
        linked = False  # success: keep dst
        _fsync_dir(os.path.dirname(src))
        return copied
    finally:
        if os.path.lexists(tmp):
            try:
                os.unlink(tmp)
            except OSError as exc:
                log.error("could not remove temp file %s: %s", tmp, exc)
        if linked and os.path.lexists(src):
            # We published a copy but kept the source: take the copy back so
            # the file is not duplicated. Only remove it if it's still ours.
            try:
                st = os.stat(dst)
                if st.st_size == before.size and st.st_mtime_ns == before.mtime_ns:
                    os.unlink(dst)
                else:
                    log.error(
                        "%s was modified right after publishing; keeping both %s and %s, "
                        "please check them manually",
                        dst, src, dst,
                    )
            except OSError as exc:
                log.error("could not roll back %s: %s", dst, exc)
        if journal is not None:
            journal.clear()


class Journal:
    """Remembers the temp file of the transfer in progress, for crash recovery."""

    def __init__(self, path: str):
        self.path = path

    def record(self, tmp: str) -> None:
        with open(self.path, "w") as fh:
            fh.write(tmp + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def clear(self) -> None:
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass

    def recover(self) -> list[str]:
        """Remove temp files of an interrupted transfer. Returns removed paths."""
        try:
            with open(self.path) as fh:
                entries = [line.strip() for line in fh if line.strip()]
        except FileNotFoundError:
            return []
        removed = []
        for tmp in entries:
            if os.path.basename(tmp).startswith(TEMP_PREFIX) and os.path.lexists(tmp):
                try:
                    os.unlink(tmp)
                    removed.append(tmp)
                    log.info("removed temp file of interrupted transfer: %s", tmp)
                except OSError as exc:
                    log.warning("cannot remove %s: %s", tmp, exc)
        self.clear()
        return removed
