#!/usr/bin/env python3
"""Cross-platform advisory lock for Codex Custom Subagents task pools.

POSIX uses ``fcntl.flock``; Windows uses ``msvcrt.locking`` byte-range locks on
an open file. Both backends serialize the same mailbox paths, so a mixed
POSIX/Windows team never double-claims a task. The module imports without any
platform-specific dependency on common paths: the backend is selected lazily at
lock time.

Windows note: ``msvcrt.locking`` requires the file to be opened in binary mode
and locks a fixed byte range. We lock the first byte of the file, which is
enough to serialize cooperating processes on the same path. Locking is advisory
and not inherited; the process must hold the file handle for the whole critical
section.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


LOCKED_BYTE_RANGE = 1


class PlatformLockError(OSError):
    """Raised when a platform lock cannot be acquired or released."""


def _posix_lock(handle: int, exclusive: bool) -> None:
    import fcntl

    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_UN
    fcntl.flock(handle, operation)


def _windows_lock(handle: int, exclusive: bool) -> None:
    import msvcrt

    if exclusive:
        msvcrt.locking(handle, msvcrt.LK_LOCK, LOCKED_BYTE_RANGE)
    else:
        # LK_UNLOCK on the same range the caller locked.
        msvcrt.locking(handle, msvcrt.LK_UNLCK, LOCKED_BYTE_RANGE)


def lock_backend() -> str:
    """Return the platform lock backend name: ``posix`` or ``windows``."""
    return "posix" if os.name == "posix" else "windows"


def _lock_handle(handle: int, exclusive: bool) -> None:
    backend = lock_backend()
    try:
        if backend == "windows":
            _windows_lock(handle, exclusive)
        else:
            _posix_lock(handle, exclusive)
    except OSError as error:
        raise PlatformLockError(f"{backend} lock failed: {error}") from error


@contextmanager
def platform_file_lock(path: Path, *, exclusive: bool = True) -> Iterator[None]:
    """Serialize access to ``path`` across processes on the current platform.

    The lock file is created (and the parent directory) when missing. The lock
    is released when the context exits; on Windows the file stays open until
    then because ``msvcrt.locking`` needs the handle.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if exclusive:
            _lock_handle(handle.fileno(), exclusive=True)
        try:
            yield
        finally:
            if exclusive:
                _lock_handle(handle.fileno(), exclusive=False)


def create_lock_file(path: Path) -> None:
    """Ensure the lock file exists so future ``open("a+b")`` never truncates it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.open("a+b").close()


def current_process() -> str:
    return sys.platform
