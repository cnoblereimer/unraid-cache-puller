"""Whole-filesystem access tracking with fanotify (no per-directory watches).

One ``FAN_MARK_FILESYSTEM`` mark per disk reports every file open on that
disk, including opens made by Unraid's shfs for /mnt/user, so nothing has to
be scanned at startup and there is no watch limit.

Events carry the parent directory as a file handle plus the file name
(``FAN_REPORT_DFID_NAME``). Handles are turned into paths with
``open_by_handle_at`` relative to a mount in *our* namespace, so the paths
match what the rest of the program sees.

Requires CAP_SYS_ADMIN (fanotify_init) and CAP_DAC_READ_SEARCH
(open_by_handle_at), i.e. ``--cap-add=SYS_ADMIN --cap-add=DAC_READ_SEARCH``.

Nothing here keeps a file descriptor open on a disk: that would keep the
filesystem busy and stop Unraid from stopping the array. Mount points are
opened only for the duration of a single handle lookup.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import logging
import os
import struct
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

FAN_CLASS_NOTIF = 0x0
FAN_CLOEXEC = 0x1
FAN_NONBLOCK = 0x2
FAN_UNLIMITED_QUEUE = 0x10
FAN_REPORT_DIR_FID = 0x400
FAN_REPORT_NAME = 0x800
FAN_REPORT_DFID_NAME = FAN_REPORT_DIR_FID | FAN_REPORT_NAME

FAN_MARK_ADD = 0x1
FAN_MARK_FILESYSTEM = 0x100

FAN_OPEN = 0x20
FAN_DELETE = 0x200
FAN_Q_OVERFLOW = 0x4000
FAN_RENAME = 0x10000000
FAN_ONDIR = 0x40000000

INFO_DFID_NAME = 2
INFO_OLD_DFID_NAME = 10
INFO_NEW_DFID_NAME = 12

AT_FDCWD = -100
_METADATA = struct.Struct("IBBHQii")
_INFO_HEADER = struct.Struct("BBH")
_FSID = struct.Struct("ii")
_HANDLE_HEADER = struct.Struct("Ii")


def _load_libc() -> ctypes.CDLL:
    for name in (ctypes.util.find_library("c"), "libc.so.6", None):
        try:
            libc = ctypes.CDLL(name, use_errno=True)
            libc.fanotify_init  # noqa: B018 - probe symbol
            return libc
        except (OSError, AttributeError):
            continue
    raise OSError(errno.ENOSYS, "fanotify is not available in this libc")


_libc = None


def _lib() -> ctypes.CDLL:
    global _libc
    if _libc is None:
        _libc = _load_libc()
        _libc.fanotify_init.argtypes = [ctypes.c_uint, ctypes.c_uint]
        _libc.fanotify_mark.argtypes = [ctypes.c_int, ctypes.c_uint, ctypes.c_uint64, ctypes.c_int, ctypes.c_char_p]
        _libc.open_by_handle_at.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    return _libc


def fsid_of(path: str) -> int:
    return os.statvfs(path).f_fsid & 0xFFFFFFFFFFFFFFFF


def mount_point(path: str) -> str:
    """The mount point of the filesystem containing ``path``."""
    path = os.path.realpath(path)
    dev = os.stat(path).st_dev
    while path != "/":
        parent = os.path.dirname(path)
        if os.stat(parent).st_dev != dev:
            break
        path = parent
    return path


@dataclass
class FanEvent:
    kind: str  # "open", "delete", "rename" or "overflow"
    pid: int
    path: str | None = None  # absolute path (new path for renames)
    old_path: str | None = None  # renames only


class Fanotify:
    def __init__(self) -> None:
        lib = _lib()
        flags = FAN_CLASS_NOTIF | FAN_CLOEXEC | FAN_NONBLOCK | FAN_UNLIMITED_QUEUE | FAN_REPORT_DFID_NAME
        fd = lib.fanotify_init(flags, os.O_RDONLY)
        if fd < 0:
            e = ctypes.get_errno()
            raise OSError(e, f"fanotify_init: {os.strerror(e)}")
        self.fd = fd
        self.mask = FAN_OPEN | FAN_DELETE | FAN_RENAME
        self._mounts: dict[int, str] = {}  # fsid -> mount point in our namespace
        self._dir_cache: dict[tuple[int, int, bytes], tuple[float, str | None]] = {}
        self.cache_ttl = 30.0
        self.self_pid = os.getpid()

    def mark(self, path: str) -> None:
        """Watch the whole filesystem containing ``path``. Idempotent.

        Raises OSError if the filesystem can't be watched this way.
        """
        lib = _lib()
        r = lib.fanotify_mark(self.fd, FAN_MARK_ADD | FAN_MARK_FILESYSTEM, self.mask, AT_FDCWD, os.fsencode(path))
        if r < 0 and self.mask & FAN_RENAME and ctypes.get_errno() == errno.EINVAL:
            # Kernels before 5.17 don't know FAN_RENAME; opens are what matter.
            self.mask &= ~FAN_RENAME
            r = lib.fanotify_mark(self.fd, FAN_MARK_ADD | FAN_MARK_FILESYSTEM, self.mask, AT_FDCWD, os.fsencode(path))
        if r < 0:
            e = ctypes.get_errno()
            raise OSError(e, f"fanotify_mark({path}): {os.strerror(e)}")
        self._mounts[fsid_of(path)] = mount_point(path)

    def read(self) -> list[FanEvent]:
        try:
            data = os.read(self.fd, 256 * 1024)
        except BlockingIOError:
            return []
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EINTR):
                return []
            raise
        return self.parse(data)

    def parse(self, data: bytes, now: float | None = None) -> list[FanEvent]:
        now = time.monotonic() if now is None else now
        events: list[FanEvent] = []
        off = 0
        while off + _METADATA.size <= len(data):
            event_len, _vers, _res, meta_len, mask, fd, pid = _METADATA.unpack_from(data, off)
            if event_len < _METADATA.size:
                break
            if fd >= 0:  # never expected in FID mode, but don't leak it
                os.close(fd)
            if mask & FAN_Q_OVERFLOW:
                events.append(FanEvent("overflow", pid))
            elif pid != self.self_pid and not mask & FAN_ONDIR:
                infos = self._infos(data, off + meta_len, off + event_len)
                if mask & FAN_RENAME:
                    old = self._resolve(infos.get(INFO_OLD_DFID_NAME), now)
                    new = self._resolve(infos.get(INFO_NEW_DFID_NAME), now)
                    if old or new:
                        events.append(FanEvent("rename", pid, new, old))
                elif mask & (FAN_OPEN | FAN_DELETE):
                    path = self._resolve(infos.get(INFO_DFID_NAME), now)
                    if path:
                        # A queued event can carry both bits when merged.
                        if mask & FAN_OPEN:
                            events.append(FanEvent("open", pid, path))
                        if mask & FAN_DELETE:
                            events.append(FanEvent("delete", pid, path))
            off += event_len
        return events

    @staticmethod
    def _infos(data: bytes, start: int, end: int) -> dict[int, tuple[int, int, bytes, str]]:
        """Info records as {type: (fsid, handle_type, handle, name)}."""
        out = {}
        p = start
        while p + _INFO_HEADER.size <= end:
            itype, _pad, ilen = _INFO_HEADER.unpack_from(data, p)
            if ilen == 0:
                break
            if itype in (INFO_DFID_NAME, INFO_OLD_DFID_NAME, INFO_NEW_DFID_NAME):
                f0, f1 = _FSID.unpack_from(data, p + 4)
                fsid = (f0 & 0xFFFFFFFF) | ((f1 & 0xFFFFFFFF) << 32)
                hbytes, htype = _HANDLE_HEADER.unpack_from(data, p + 12)
                hstart = p + 12 + _HANDLE_HEADER.size
                handle = data[hstart: hstart + hbytes]
                name = data[hstart + hbytes: p + ilen].split(b"\0", 1)[0]
                out[itype] = (fsid, htype, handle, os.fsdecode(name))
            p += ilen
        return out

    def _resolve(self, info, now: float) -> str | None:
        if info is None:
            return None
        fsid, htype, handle, name = info
        if not name or name == ".":
            return None
        key = (fsid, htype, handle)
        hit = self._dir_cache.get(key)
        if hit is not None and now - hit[0] < self.cache_ttl:
            directory = hit[1]
        else:
            directory = self._dir_path(fsid, htype, handle)
            if len(self._dir_cache) > 50000:
                self._dir_cache.clear()
            self._dir_cache[key] = (now, directory)
        return os.path.join(directory, name) if directory else None

    def _dir_path(self, fsid: int, htype: int, handle: bytes) -> str | None:
        mnt = self._mounts.get(fsid)
        if mnt is None:
            return None
        buf = _HANDLE_HEADER.pack(len(handle), htype) + handle
        try:
            mfd = os.open(mnt, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except OSError:
            return None
        try:
            fd = _lib().open_by_handle_at(mfd, buf, os.O_PATH | os.O_CLOEXEC)
            if fd < 0:
                return None  # e.g. ESTALE: the directory is gone
            try:
                path = os.readlink(f"/proc/self/fd/{fd}")
            finally:
                os.close(fd)
        finally:
            os.close(mfd)
        if path.endswith(" (deleted)") or not path.startswith("/"):
            return None
        return path

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
