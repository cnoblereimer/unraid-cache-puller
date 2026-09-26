"""Minimal inotify binding via ctypes (no third-party dependencies)."""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import struct
from dataclasses import dataclass

IN_ACCESS = 0x00000001
IN_MODIFY = 0x00000002
IN_ATTRIB = 0x00000004
IN_CLOSE_WRITE = 0x00000008
IN_CLOSE_NOWRITE = 0x00000010
IN_OPEN = 0x00000020
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_UNMOUNT = 0x00002000
IN_Q_OVERFLOW = 0x00004000
IN_IGNORED = 0x00008000
IN_ONLYDIR = 0x01000000
IN_DONT_FOLLOW = 0x02000000
IN_EXCL_UNLINK = 0x04000000
IN_ISDIR = 0x40000000

_IN_CLOEXEC = os.O_CLOEXEC
_IN_NONBLOCK = os.O_NONBLOCK
_HEADER = struct.Struct("iIII")


def _load_libc() -> ctypes.CDLL:
    for name in (ctypes.util.find_library("c"), "libc.so.6", None):
        try:
            libc = ctypes.CDLL(name, use_errno=True)
            libc.inotify_init1  # noqa: B018 - probe symbol
            return libc
        except (OSError, AttributeError):
            continue
    raise OSError("inotify is not available in this libc")


_libc = _load_libc()
_libc.inotify_init1.argtypes = [ctypes.c_int]
_libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
_libc.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]


@dataclass
class Event:
    wd: int
    mask: int
    cookie: int
    name: str


class Inotify:
    def __init__(self) -> None:
        fd = _libc.inotify_init1(_IN_CLOEXEC | _IN_NONBLOCK)
        if fd < 0:
            e = ctypes.get_errno()
            raise OSError(e, f"inotify_init1: {os.strerror(e)}")
        self.fd = fd

    def add_watch(self, path: str, mask: int) -> int:
        wd = _libc.inotify_add_watch(self.fd, os.fsencode(path), mask)
        if wd < 0:
            e = ctypes.get_errno()
            raise OSError(e, f"inotify_add_watch({path}): {os.strerror(e)}")
        return wd

    def rm_watch(self, wd: int) -> None:
        _libc.inotify_rm_watch(self.fd, wd)

    def read(self) -> list[Event]:
        try:
            data = os.read(self.fd, 256 * 1024)
        except BlockingIOError:
            return []
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EINTR):
                return []
            raise
        return parse_events(data)

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def parse_events(data: bytes) -> list[Event]:
    events = []
    off = 0
    while off + _HEADER.size <= len(data):
        wd, mask, cookie, length = _HEADER.unpack_from(data, off)
        off += _HEADER.size
        raw = data[off : off + length]
        off += length
        name = os.fsdecode(raw.split(b"\0", 1)[0])
        events.append(Event(wd, mask, cookie, name))
    return events
