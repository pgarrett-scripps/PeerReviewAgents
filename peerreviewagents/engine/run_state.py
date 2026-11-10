"""Durable stage state with process identity and interruption detection."""
import ctypes
import ctypes.util
import errno
import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .storage import read, write

# macOS has no PID namespaces, so every local process shares one fixed value.
DARWIN_NAMESPACE = 'darwin'


class _BSDInfo(ctypes.Structure):
    # struct proc_bsdinfo from <sys/proc_info.h>, returned by PROC_PIDTBSDINFO.
    _fields_ = [(name, ctypes.c_uint32) for name in (
        'flags', 'status', 'xstatus', 'pid', 'ppid', 'uid', 'gid', 'ruid', 'rgid', 'svuid', 'svgid', 'rfu_1')] + [
        ('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32)] + [
        (name, ctypes.c_uint32) for name in ('nfiles', 'pgid', 'pjobc', 'tdev', 'tpgid')] + [
        ('nice', ctypes.c_int32), ('start_tvsec', ctypes.c_uint64), ('start_tvusec', ctypes.c_uint64)]


def _darwin_boot_session():
    libc = ctypes.CDLL(ctypes.util.find_library('c'))
    # Explicit signatures keep pointer and size_t arguments correct on Apple Silicon.
    libc.sysctlbyname.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                                  ctypes.c_void_p, ctypes.c_size_t]
    libc.sysctlbyname.restype = ctypes.c_int
    size = ctypes.c_size_t(64)
    value = ctypes.create_string_buffer(size.value)
    if libc.sysctlbyname(b'kern.bootsessionuuid', value, ctypes.byref(size), None, 0) != 0:
        return None
    return value.value.decode() or None


def _darwin_identity(pid):
    """Return boot session and exact start time, None if absent, or raise if not inspectable."""
    if type(pid) is not int or pid <= 0:
        return None
    # Since macOS 11 this resolves from the dyld shared cache, not a file on disk.
    libproc = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    libproc.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    libproc.proc_pidinfo.restype = ctypes.c_int
    info = _BSDInfo()
    # PROC_PIDTBSDINFO is 3. Zombies and exited processes return no record.
    if libproc.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info):
        if ctypes.get_errno() == errno.EPERM:
            raise PermissionError('Process belongs to another user')
        return None
    boot = _darwin_boot_session()
    return f'{boot}:{info.start_tvsec}.{info.start_tvusec:06d}' if boot else None


def process_namespace():
    if sys.platform == 'darwin':
        # Without a working identity probe, report no namespace so inspection stays unverified.
        try:
            available = _darwin_identity(os.getpid()) is not None
        except (OSError, AttributeError):
            available = False
        return DARWIN_NAMESPACE if available else None
    try:
        return os.readlink('/proc/self/ns/pid')
    except OSError:
        return None


def process_identity(pid):
    if sys.platform == 'darwin':
        try:
            return _darwin_identity(pid)
        except PermissionError:
            raise
        except (OSError, AttributeError):
            return None
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] == 'Z':
            return None
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()+':'+fields[19]
    except (OSError, IndexError):
        return None


def inspect_state(path):
    """Derive stale status without rewriting the historical stage record."""
    value = read(path)
    if value.get('status') == 'running':
        namespace = process_namespace()
        if not namespace or value.get('process_namespace') != namespace:
            return {**value, 'status': 'unverified', 'recorded_status': 'running',
                    'reason': 'Inspect from the owning process namespace before declaring interruption or retrying.'}
        try:
            identity = process_identity(value.get('pid'))
        except PermissionError:
            return {**value, 'status': 'unverified', 'recorded_status': 'running',
                    'reason': 'The owning process exists but cannot be inspected by this user.'}
        if not identity or identity != value.get('process_identity'):
            return {**value, 'status': 'interrupted', 'reason': 'Owning process is absent or changed.',
                    'recorded_status': 'running', 'usage_requires_reconciliation': True}
        if time.time() > value.get('deadline_at', float('inf')):
            return {**value, 'status': 'overdue', 'recorded_status': 'running',
                    'reason': 'Stage exceeded its expected completion deadline. Do not retry while its process is alive.'}
    return value


@contextmanager
def tracked_stage(path, label, timeout=1200):
    path = Path(path)
    pid = os.getpid()
    now = time.time()
    value = {'status': 'running', 'stage': label, 'pid': pid,
             'process_identity': process_identity(pid), 'process_namespace': process_namespace(), 'started_at': now,
             'heartbeat_at': now, 'deadline_at': now+timeout}
    write(path, value)
    stopped = threading.Event()
    def heartbeat():
        while not stopped.wait(15):
            value['heartbeat_at'] = time.time()
            write(path, value)
    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        yield
    except BaseException as error:
        stopped.set()
        thread.join()
        write(path, {**value, 'status': 'interrupted' if isinstance(error, (KeyboardInterrupt, SystemExit)) else 'failed',
                     'finished_at': time.time(), 'error': str(error), 'usage_requires_reconciliation': True})
        raise
    else:
        stopped.set()
        thread.join()
        write(path, {**value, 'status': 'complete', 'finished_at': time.time()})
