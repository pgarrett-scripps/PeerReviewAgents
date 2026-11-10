"""Exclusive advisory file locking that stays importable without ``fcntl``.

The engine locks small sentinel files to serialise concurrent reviewer processes.
``fcntl`` is absent on Windows, so importing it at module scope would break the
package there. This module picks the available primitive once, at import time,
and exposes one blocking call.
"""
import time

try:  # POSIX
    import fcntl
except ModuleNotFoundError:  # pragma: no cover - exercised only on Windows
    fcntl = None

try:  # Windows
    import msvcrt
except ModuleNotFoundError:  # pragma: no cover - exercised only on POSIX
    msvcrt = None

BACKEND = 'flock' if fcntl is not None else 'msvcrt' if msvcrt is not None else 'none'
RETRY_SECONDS = 0.05
TIMEOUT_SECONDS = 30.0


def exclusive_lock(handle, timeout=TIMEOUT_SECONDS):
    """Take an exclusive lock on an open file, blocking until it is granted.

    The lock is released when the file object is closed, which is how every
    caller uses it (``with path.open('a') as lock``). Returns the backend name
    so a caller can record that no lock was available.
    """
    if fcntl is not None:
        fcntl.flock(handle, fcntl.LOCK_EX)
        return 'flock'
    if msvcrt is not None:  # pragma: no cover - exercised only on Windows
        # Lock one byte at the start of the file. The sentinel files are opened
        # for append and never written, so the descriptor sits at offset zero.
        handle.seek(0)
        deadline = time.monotonic() + timeout
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return 'msvcrt'
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(RETRY_SECONDS)
    return 'none'  # pragma: no cover - no platform without one of the two
