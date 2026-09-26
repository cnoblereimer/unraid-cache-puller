"""Host-level safety checks: open files and a running mover.

Both checks scan the host's /proc, so the container must run with
``--pid=host`` (and ``--cap-add=SYS_PTRACE`` to read other processes' fds).
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

_CONTAINER_INITS = {"tini", "docker-init", "python", "python3", "sh", "bash", "cache-puller"}


def host_pid_visible(proc: str = "/proc") -> bool:
    """Best-effort check that we share the host's PID namespace."""
    if os.getpid() == 1:
        return False
    try:
        with open(os.path.join(proc, "1", "comm")) as fh:
            comm = fh.read().strip()
    except OSError:
        return False
    return comm not in _CONTAINER_INITS


def _pids(proc: str) -> list[str]:
    try:
        return [p for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return []


class OpenFileChecker:
    """Answers "is this file open by any process on the host?"."""

    def __init__(self, mode: str, proc: str = "/proc"):
        self.proc = proc
        self.self_pid = str(os.getpid())
        visible = host_pid_visible(proc)
        if mode == "off":
            self.enabled = False
            self.usable = True
            log.warning("OPEN_FILE_CHECK=off: files in use may be moved")
        elif visible:
            self.enabled = True
            self.usable = True
        elif mode == "auto":
            self.enabled = False
            self.usable = True
            log.warning(
                "host processes are not visible (run the container with --pid=host); "
                "open-file checks are disabled"
            )
        else:  # "on" but we cannot check: refuse to move anything
            self.enabled = False
            self.usable = False
            log.error(
                "OPEN_FILE_CHECK=on but host processes are not visible; add "
                "'--pid=host --cap-add=SYS_PTRACE' to the container. No files will be moved."
            )

    def is_open(self, paths: list[str]) -> bool:
        """True if any of ``paths`` is open. Matches by (dev, inode) and by path.

        Unreadable fd tables count as "unknown" and are ignored, except that a
        complete failure to read any fd table makes the file count as open.
        """
        if not self.enabled:
            return False
        targets: set[tuple[int, int]] = set()
        names: set[str] = set()
        for p in paths:
            names.add(p)
            try:
                st = os.stat(p)
                targets.add((st.st_dev, st.st_ino))
            except OSError:
                pass
        readable = 0
        for pid in _pids(self.proc):
            if pid == self.self_pid:
                continue
            fd_dir = os.path.join(self.proc, pid, "fd")
            try:
                fds = os.listdir(fd_dir)
            except OSError:
                continue
            readable += 1
            for fd in fds:
                fp = os.path.join(fd_dir, fd)
                try:
                    link = os.readlink(fp)
                except OSError:
                    continue
                if not link.startswith("/"):
                    continue  # sockets, pipes, anon inodes
                if link in names:
                    return True
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                if (st.st_dev, st.st_ino) in targets:
                    return True
        if readable == 0:
            log.error("could not read any process fd table; treating file as open")
            return True
        return False


def mover_running(pid_files: list[str], process_names: list[str], proc: str = "/proc") -> str | None:
    """Return a description of the running mover, or None."""
    for pf in pid_files:
        try:
            with open(pf) as fh:
                pid = fh.read().strip()
        except OSError:
            continue
        if pid.isdigit() and os.path.exists(os.path.join(proc, pid)):
            return f"pid file {pf} (pid {pid})"
    names = set(process_names)
    if not names:
        return None
    for pid in _pids(proc):
        try:
            with open(os.path.join(proc, pid, "cmdline"), "rb") as fh:
                argv = [a.decode(errors="replace") for a in fh.read().split(b"\0") if a]
        except OSError:
            continue
        # Match the program itself or a script run through an interpreter,
        # e.g. "/bin/bash /usr/local/sbin/mover start".
        for arg in argv[:2]:
            if os.path.basename(arg) in names:
                return f"process {pid}: {' '.join(argv)}"
    return None
