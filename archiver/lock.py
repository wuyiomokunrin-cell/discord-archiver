"""Cross-platform exclusive run lock.

POSIX has fcntl.flock; Windows has no fcntl at all, so the previous
implementation crashed the moment it ran on the user's dual-booted Windows.
This uses msvcrt.locking on Windows instead. Both release the lock
automatically when the process dies, so a crash cannot strand it.
"""

from __future__ import annotations

import os

_BUSY = (
    "Another archiver process is already using this database.\n"
    "  -> Find it with:  pgrep -af \"main.py\"            (Linux/macOS)\n"
    "                    tasklist | findstr python        (Windows)\n"
    "  -> Only one may run at a time; SQLite allows a single writer."
)


def acquire(db_path) -> object:
    """Take an exclusive lock on <db_path>.lock. Raises SystemExit if held."""
    lock_path = str(db_path) + ".lock"
    if os.name == "nt":
        return _acquire_windows(lock_path)
    return _acquire_posix(lock_path)


def _acquire_posix(lock_path: str):
    import fcntl
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise SystemExit(_BUSY)
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def _acquire_windows(lock_path: str):
    import msvcrt
    fh = open(lock_path, "w")
    fh.write("0")          # msvcrt.locking needs at least one byte present
    fh.flush()
    fh.seek(0)
    try:
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        fh.close()
        raise SystemExit(_BUSY)
    fh.write(str(os.getpid()))
    fh.flush()
    return fh
