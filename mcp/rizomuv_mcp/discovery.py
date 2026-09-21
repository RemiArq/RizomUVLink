"""The discovery files listening RizomUV instances publish, and the client lock on each.

Every RizomUV that listens for RizomUVLink (the artist's interactive session on an
ephemeral port, or an instance started with -id) writes <instances_dir>/<pid>.json once
its port is bound, atomically, and deletes it when it quits. A crash leaves the file
behind, and a later process may even get the same pid, so a record is only believed once
the pid is alive AND is still the process that wrote it (creation time and image name on
Windows; the token over the link settles it everywhere).

The link protocol serves one client per port, so a client that attaches holds an
exclusive advisory lock on <instances_dir>/<pid>.lock for its whole session. The OS
releases it when the holder dies: nothing to clean up after a crash.
"""
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)

SCHEMA = 1
_WINDOWS = sys.platform == "win32"


@dataclass(frozen=True)
class InstanceRecord:
    pid: int
    port: int
    notify_port: int | None
    token: str
    headless: bool
    port_source: str        # "default": the artist's own session; "command_line": started with -id
    owner_pid: int | None   # RIZOMUV_LINK_OWNER_PID of whoever launched it, if they said
    version: str
    exe: str
    user_dir: str
    started_unix: int
    process_start: int      # Windows FILETIME creation ticks, 0 elsewhere
    path: Path

    @property
    def artist_session(self):
        """Opened by the artist (listening without -id, with a window): their live scene."""
        return self.port_source == "default" and not self.headless


# ------------------------------------------------------------------ process identity

if _WINDOWS:
    import ctypes
    import ctypes.wintypes as wt

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenProcess.restype = wt.HANDLE
    _k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    _k32.GetExitCodeProcess.restype = wt.BOOL
    _k32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
    _k32.GetProcessTimes.restype = wt.BOOL
    _k32.GetProcessTimes.argtypes = [wt.HANDLE] + [ctypes.POINTER(wt.FILETIME)] * 4
    _k32.QueryFullProcessImageNameW.restype = wt.BOOL
    _k32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
    _k32.CloseHandle.argtypes = [wt.HANDLE]

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259
    _ERROR_ACCESS_DENIED = 5

    def _identity(pid):
        """(alive, creation ticks or None, image path or None)."""
        if pid <= 0:
            return False, None, None
        h = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            # a process of another user or an elevated one exists, it is just not ours
            # to open; anything else (invalid parameter) means there is no such process
            return ctypes.get_last_error() == _ERROR_ACCESS_DENIED, None, None
        try:
            code = wt.DWORD()
            alive = bool(_k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == _STILL_ACTIVE
            c, e, k, u = wt.FILETIME(), wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
            ticks = None
            if _k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
                ticks = (c.dwHighDateTime << 32) | c.dwLowDateTime
            buf = ctypes.create_unicode_buffer(1024)
            n = wt.DWORD(len(buf))
            image = buf.value if _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) else None
            return alive, ticks, image
        finally:
            _k32.CloseHandle(h)


def pid_alive(pid):
    """True while a process with this pid exists (whoever it is)."""
    pid = int(pid)
    if pid <= 0:
        return False
    if _WINDOWS:
        # never os.kill(pid, 0) here: on Windows it is TerminateProcess
        return _identity(pid)[0]
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_start_ticks(pid):
    """Creation time of a live process as the app publishes it (Windows FILETIME ticks),
    None when unknown (dead, not ours to query, or not Windows)."""
    if not _WINDOWS:
        return None
    alive, ticks, _ = _identity(int(pid))
    return ticks if alive else None


def process_image(pid):
    """Full path of the executable of a live process, None when unknown."""
    if not _WINDOWS:
        return None
    alive, _, image = _identity(int(pid))
    return image if alive else None


def record_alive(record):
    """The process that wrote this record is still running. The pid alone is not enough:
    a stale file of a crashed instance may name a pid that now belongs to anything."""
    if not _WINDOWS:
        return pid_alive(record.pid)
    alive, ticks, image = _identity(record.pid)
    if not alive:
        return False
    if record.process_start and ticks is not None and ticks != record.process_start:
        return False
    if record.exe and image and \
            os.path.basename(record.exe).casefold() != os.path.basename(image).casefold():
        return False
    return True


# ------------------------------------------------------------------ records

def _load_text(path):
    return path.read_text(encoding="utf-8")


def _load_json(path):
    # The app renames a complete file into place, so a torn read should not happen; a
    # writer that is not the app, or a sharing violation during the rename, can still
    # produce one: one retry, then the file is garbage.
    for attempt in (0, 1):
        try:
            return json.loads(_load_text(path))
        except FileNotFoundError:
            return None                 # the instance quit between the listing and the read
        except (OSError, ValueError):   # ValueError covers JSONDecodeError and bad UTF-8
            if attempt == 0:
                time.sleep(0.05)
    return None


def _int(value, *, optional=False):
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(value)
    return value


def _str(value):
    if not isinstance(value, str):
        raise ValueError(value)
    return value


def read_record(path):
    """The record of one discovery file, or None when it is not one we understand: a
    .tmp being written, another schema version, unparsable or incomplete content, or a
    pid that does not match the file name."""
    path = Path(path)
    if path.suffix != ".json":
        return None
    data = _load_json(path)
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return None
    try:
        record = InstanceRecord(
            pid=_int(data["pid"]),
            port=_int(data["port"]),
            notify_port=_int(data.get("notify_port"), optional=True),
            token=_str(data["token"]),
            headless=bool(data.get("headless", False)),
            port_source=_str(data.get("port_source", "")),
            owner_pid=_int(data.get("owner_pid"), optional=True),
            version=_str(data.get("version", "")),
            exe=_str(data.get("exe", "")),
            user_dir=_str(data.get("user_dir", "")),
            started_unix=_int(data.get("started_unix", 0)),
            process_start=_int(data.get("process_start", 0)),
            path=path,
        )
    except (KeyError, ValueError):
        log.debug("ignoring malformed discovery file %s", path)
        return None
    if str(record.pid) != path.stem or not 0 < record.port < 65536:
        log.debug("ignoring inconsistent discovery file %s", path)
        return None
    return record


def list_instances(directory=None, alive_only=True):
    """Records of the instances in the discovery directory, newest first."""
    directory = Path(directory) if directory is not None else paths.instances_dir()
    try:
        files = sorted(directory.glob("*.json"))
    except OSError:
        return []
    records = [r for r in map(read_record, files) if r is not None]
    if alive_only:
        records = [r for r in records if record_alive(r)]
    records.sort(key=lambda r: (r.started_unix, r.pid), reverse=True)
    return records


def lock_path(record_or_pid):
    """The client lock of an instance: next to its record, named after its pid."""
    if isinstance(record_or_pid, InstanceRecord):
        return record_or_pid.path.with_name("%d.lock" % record_or_pid.pid)
    return paths.instances_dir() / ("%d.lock" % int(record_or_pid))


def port_lock_path(port):
    """The client lock of an instance known only by its port (a build that publishes no
    discovery file, attached with --port)."""
    return paths.instances_dir() / ("port-%d.lock" % int(port))


# ------------------------------------------------------------------ locks

def try_lock(handle):
    """Exclusive, non-blocking lock on byte 0 of an open file. The same technique as
    RizomUVLink.py's CLaunchLock, so that the two refuse each other."""
    handle.seek(0)
    try:
        if _WINDOWS:
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def unlock(handle):
    try:
        handle.seek(0)
        if _WINDOWS:
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def remove_lock_file(path):
    """Best effort, and Windows only. There, a lock file cannot be deleted while anyone
    has it open (Python opens without FILE_SHARE_DELETE), so the delete either happens
    when nobody uses it or fails harmlessly. On POSIX an unlink could let two processes
    lock two different inodes under one name: the files stay."""
    if _WINDOWS:
        try:
            os.remove(path)
        except OSError:
            pass


class LockRefused(RuntimeError):
    """Another process holds the lock."""


class InstanceLock:
    """Exclusive right to drive one RizomUV instance, held for a whole session.

    acquire() answers False when another process holds it: another assistant drives
    that instance. A lock file that cannot even be created raises OSError: that is a
    different problem and must not be reported as the first one.
    As a context manager it raises LockRefused instead of answering False.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._handle = None

    @property
    def held(self):
        return self._handle is not None

    def acquire(self):
        if self._handle is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        if not try_lock(handle):
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self):
        if self._handle is None:
            return
        unlock(self._handle)
        self._handle.close()
        self._handle = None
        remove_lock_file(self.path)

    def __enter__(self):
        if not self.acquire():
            raise LockRefused("%s is held by another process" % self.path)
        return self

    def __exit__(self, *exc):
        self.release()

    def __repr__(self):
        return "InstanceLock(%r, held=%s)" % (str(self.path), self.held)
