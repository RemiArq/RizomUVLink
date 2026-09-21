"""Start a private headless RizomUV the server owns: find the executable, pick a port
pair, spawn it with explicit stdio inside a kill-on-close job, and explain why it stopped
when it does not come up.

Why not RizomUVLink.RunRizomUV(): it Popens with inherited handles, and a headless
RizomUV writes its log to whatever stdout it inherits -- here, the MCP JSON-RPC wire.
"""
import logging
import os
import random
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import discovery, paths, tcptable

log = logging.getLogger(__name__)

_WINDOWS = sys.platform == "win32"
EXE_NAME = "rizomuv.exe" if _WINDOWS else "rizomuv"

# The app also binds port + 1 for its notification channel, so ports come in pairs.
PORT_MIN, PORT_MAX = 49152, 65533


class LaunchError(Exception):
    """RizomUV could not be found or started; the message is meant for the user."""


# ------------------------------------------------------------------ finding the executable

def registry_installs():
    """[(version (major, minor), source, install dir)] of the RizomUV installs registered
    under HKLM\\SOFTWARE\\Rizom Lab, newest first, 64-bit view before 32-bit. The key and
    value shape are the installer's: "RizomUV VS RS <maj>.<min>\\rizomuv.exe" whose
    default value is "{app}\\rizomuv"."""
    if not _WINDOWS:
        return []
    import winreg
    found = []
    for view, label in ((winreg.KEY_WOW64_64KEY, ""), (winreg.KEY_WOW64_32KEY, " (32-bit view)")):
        try:
            root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Rizom Lab", 0, winreg.KEY_READ | view)
        except OSError:
            continue
        with root:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                m = re.fullmatch(r"RizomUV VS RS (\d+)\.(\d+)", name)
                if not m:
                    continue
                try:
                    with winreg.OpenKey(root, name + r"\rizomuv.exe", 0, winreg.KEY_QUERY_VALUE | view) as key:
                        value = winreg.QueryValueEx(key, "")[0]
                except OSError:
                    continue
                if value:
                    found.append(((int(m.group(1)), int(m.group(2))),
                                  "HKLM\\SOFTWARE\\Rizom Lab\\" + name + label, Path(value).parent))
    # compared as integers, so 2027.10 ranks above 2027.2; the sort is stable, so the
    # 64-bit view keeps priority for a version registered in both
    found.sort(key=lambda t: t[0], reverse=True)
    return found


def mac_bundles():
    """RizomUV bundles in /Applications, newest version first."""
    if sys.platform != "darwin":
        return []
    bundles = list(Path("/Applications").glob("RizomUV.*.app"))
    return sorted(bundles, key=lambda p: [int(n) for n in re.findall(r"\d+", p.name)], reverse=True)


def _candidates():
    """(path, source) in the order of the spec; each is checked by the caller."""
    app_dir = os.environ.get("RIZOMUV_MCP_APP_DIR")
    if app_dir:
        yield Path(app_dir) / EXE_NAME, "RIZOMUV_MCP_APP_DIR"
    pkg = paths.package_dir()
    if _WINDOWS:
        # an install ships RizomUVLink next to rizomuv.exe
        yield pkg.parent / EXE_NAME, "the install this server ships with"
        for _, source, install in registry_installs():
            yield install / EXE_NAME, source
    elif sys.platform == "darwin":
        contents = pkg.parent.parent                 # <bundle>/Contents/Resources/RizomUVLink
        if contents.name == "Contents":
            yield contents / "MacOS" / contents.parent.stem, "the bundle this server ships with"
        for bundle in mac_bundles():
            yield bundle / "Contents" / "MacOS" / bundle.stem, "/Applications"
    else:
        # the package ships in the AppImage at usr/bin/RizomUVLink; the launchable entry
        # is the AppDir's AppRun (the bare usr/bin/rizomuv carries no rpath)
        if len(pkg.parents) > 2:
            appdir = pkg.parents[2]
            if (appdir / "usr" / "bin" / "rizomuv").exists():
                yield appdir / "AppRun", "the AppImage this server ships with"
        for name in ("rizomuv", "RizomUV"):
            found = shutil.which(name)
            if found:
                yield Path(found), "PATH"
    if _WINDOWS:
        # a dev checkout: <repo>/RizomUVLink/RizomUVLink is this package
        yield pkg.parent.parent / "RizomUVApp" / "bin" / EXE_NAME, "the development tree"


def find_rizomuv_exe(explicit=None):
    """(path, source) of the RizomUV executable to launch. An explicit choice (--exe, then
    RIZOMUV_EXE) is never second-guessed: if it names nothing, that is the error."""
    for value, source in ((explicit, "--exe"), (os.environ.get("RIZOMUV_EXE"), "RIZOMUV_EXE")):
        if value:
            path = Path(value)
            if not path.is_file():
                raise LaunchError("%s names %s, which is not a file." % (source, value))
            return path, source
    tried = []
    for path, source in _candidates():
        if path.is_file():
            return path, source
        tried.append("%s: %s" % (source, path))
    if not tried:
        tried.append("no RizomUV install is registered on this machine")
    raise LaunchError("RizomUV was not found. Tried:\n  " + "\n  ".join(tried)
                      + "\nPass --exe <path to the RizomUV executable> or set RIZOMUV_EXE.")


# ------------------------------------------------------------------ ports

def port_listening(port, timeout=0.3):
    """Something accepts TCP connections on 127.0.0.1:<port>. (A zmq REP socket accepts
    a bare TCP connection and drops it: the same probe TCPPortIsOpen makes.)"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout):
            return True
    except OSError:
        return False


def _bindable(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if _WINDOWS:
            # exclusive: the bind must fail wherever another socket already holds 127.0.0.1:port
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _pair_free(port, listening=None):
    """p and p + 1 bindable on 127.0.0.1, and nobody listening on either. The second test
    is not redundant: on Windows a listener on 0.0.0.0:p does not stop a bind to
    127.0.0.1:p (measured, even with SO_EXCLUSIVEADDRUSE), yet until RizomUV has bound,
    the readiness probe of 127.0.0.1:p would reach that listener and take it for RizomUV."""
    if listening is None:
        listening = tcptable.listening_ports() or set()
    return port not in listening and port + 1 not in listening and _bindable(port) and _bindable(port + 1)


def pick_port_pair(start=None, attempts=500):
    """A port p such that p and p + 1 are both free on 127.0.0.1 and nobody is launching
    on p. The start is random: RunRizomUV scans upward from 49152, and so do people."""
    span = PORT_MAX - PORT_MIN + 1
    first = random.randint(PORT_MIN, PORT_MAX) if start is None else start
    listening = tcptable.listening_ports() or set()
    for i in range(min(attempts, span)):
        port = PORT_MIN + (first - PORT_MIN + i) % span
        if _pair_free(port, listening) and LaunchLock.free(port):
            return port
    raise LaunchError("No free pair of loopback ports was found in %d-%d." % (PORT_MIN, PORT_MAX + 1))


class LaunchLock:
    """Exclusive right to start a RizomUV on one port, shared with RizomUVLink.py.

    Same file (%TEMP%/rizomuvlink_launch_<port>.lock) and same byte-0 lock as its
    CLaunchLock, so a DCC bridge calling RunRizomUV() and this server never launch on the
    same port at once. Re-entrant within this process: two byte-range locks on two
    handles of one process refuse each other on Windows, and the server must not refuse
    itself when a caller holds the lock around launch_headless(), which takes it too.
    """

    _mutex = threading.Lock()
    _held = {}   # port -> [file handle, count]

    def __init__(self, port):
        self.port = int(port)
        self.path = Path(tempfile.gettempdir()) / ("rizomuvlink_launch_%d.lock" % self.port)
        self._mine = False

    @property
    def held(self):
        return self._mine

    @classmethod
    def free(cls, port):
        """Nobody holds it, this process included."""
        with cls._mutex:
            if port in cls._held:
                return False
        probe = cls(port)
        try:
            if not probe.acquire():
                return False
        except OSError:
            return True   # no lock possible here: the bind test alone decides
        probe.release()
        return True

    def acquire(self):
        """False when another process holds it. OSError when the lock file cannot even be
        created, which is a different problem."""
        if self._mine:
            return True
        with self._mutex:
            entry = self._held.get(self.port)
            if entry is None:
                handle = open(self.path, "a+b")
                if not discovery.try_lock(handle):
                    handle.close()
                    return False
                self._held[self.port] = [handle, 1]
            else:
                entry[1] += 1
            self._mine = True
            return True

    def release(self):
        if not self._mine:
            return
        with self._mutex:
            self._mine = False
            entry = self._held.get(self.port)
            if entry is None:
                return
            entry[1] -= 1
            if entry[1] > 0:
                return
            del self._held[self.port]
            discovery.unlock(entry[0])
            entry[0].close()
        discovery.remove_lock_file(self.path)

    def __enter__(self):
        if not self.acquire():
            raise LaunchError("Another process is starting RizomUV on port %d right now." % self.port)
        return self

    def __exit__(self, *exc):
        self.release()


# ------------------------------------------------------------------ job object (Windows)

class _Job:
    """A job object with KILL_ON_JOB_CLOSE holding one launched instance. When this
    process dies -- however it dies -- the kernel closes the handle and kills the
    instance and anything it started: no orphan keeps a licence seat."""

    def __init__(self, handle, backend):
        self.handle = handle
        self.backend = backend

    def terminate(self, code=1):
        try:
            if self.backend == "pywin32":
                import win32job
                win32job.TerminateJobObject(self.handle, code)
            else:
                _k32().TerminateJobObject(self.handle, code)
        except Exception as e:   # noqa: BLE001
            log.debug("TerminateJobObject failed: %r", e)

    def close(self):
        if self.handle is None:
            return
        try:
            if self.backend == "pywin32":
                self.handle.Close()
            else:
                _k32().CloseHandle(self.handle)
        except Exception as e:   # noqa: BLE001
            log.debug("closing the job handle failed: %r", e)
        self.handle = None


# Job handles live until terminate() or the end of this process: a dropped
# LaunchedInstance must not kill its instance behind the caller's back (garbage collection
# closes a pywin32 handle), only the death of the server does.
_JOBS = set()


def _k32():
    import ctypes
    import ctypes.wintypes as wt
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wt.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
    k32.SetInformationJobObject.restype = wt.BOOL
    k32.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
    k32.AssignProcessToJobObject.restype = wt.BOOL
    k32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
    k32.TerminateJobObject.restype = wt.BOOL
    k32.TerminateJobObject.argtypes = [wt.HANDLE, wt.UINT]
    k32.CloseHandle.argtypes = [wt.HANDLE]
    return k32


_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JobObjectExtendedLimitInformation = 9


def _job_pywin32(process_handle):
    import win32job
    job = win32job.CreateJobObject(None, "")
    info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
    win32job.AssignProcessToJobObject(job, process_handle)
    return _Job(job, "pywin32")


def _job_ctypes(process_handle):
    import ctypes
    import ctypes.wintypes as wt

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount",
                    "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BASIC(ctypes.Structure):   # JOBOBJECT_BASIC_LIMIT_INFORMATION
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD)]

    class EXTENDED(ctypes.Structure):   # JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = _k32()
    job = k32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(job, _JobObjectExtendedLimitInformation,
                                       ctypes.byref(info), ctypes.sizeof(info)) or \
            not k32.AssignProcessToJobObject(job, int(process_handle)):
        err = ctypes.get_last_error()
        k32.CloseHandle(job)
        raise ctypes.WinError(err)
    return _Job(job, "ctypes")


def _make_job(proc, backend="auto"):
    """The job holding proc, or None when none could be made. pywin32 first (the SDK
    brings it along on Windows), ctypes when it is not importable. Failing is not fatal:
    the instance still works, it merely could outlive a killed server."""
    for name, make in (("pywin32", _job_pywin32), ("ctypes", _job_ctypes)):
        if backend not in ("auto", name):
            continue
        try:
            job = make(int(proc._handle))
        except ImportError:
            continue
        except Exception as e:   # noqa: BLE001
            log.warning("could not put pid %d in a job object (%s): %r", proc.pid, name, e)
            return None
        _JOBS.add(job)
        return job
    return None


def spawn_in_job(args, *, cwd=None, env=None, stdout=None, stderr=None, job_backend="auto"):
    """Popen with stdin closed, explicit stdout/stderr (never this process' own: stdout is
    the MCP wire), no console window, and -- on Windows -- inside a kill-on-close job.
    POSIX gets a session of its own instead, so the whole group can be signalled."""
    kwargs = dict(cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                  stdout=subprocess.DEVNULL if stdout is None else stdout,
                  stderr=subprocess.DEVNULL if stderr is None else stderr)
    if _WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(args, **kwargs)
    job = _make_job(proc, job_backend) if _WINDOWS else None
    return proc, job


# ------------------------------------------------------------------ exit codes

_NTSTATUS = {
    0xC0000005: "it crashed (access violation, 0xC0000005)",
    0xC0000374: "it crashed (heap corruption, 0xC0000374)",
    0xC0000409: "it crashed (fail-fast / stack buffer overrun, 0xC0000409)",
    0xC00000FD: "it crashed (stack overflow, 0xC00000FD)",
    0xC0000135: "a DLL it needs was not found (0xC0000135): the installation is incomplete",
    0xC0000142: "a DLL failed to initialise (0xC0000142)",
    0xC000013A: "it was interrupted (Ctrl+C, 0xC000013A)",
}


def explain_exit_code(code):
    """One sentence on why a headless RizomUV stopped with this exit code
    (docs/headless-mode.md, "Lifetime and exit codes")."""
    if code is None:
        return "it is still running"
    if code == 0:
        return "it quit normally (exit code 0)"
    if code == 1:
        return ("no usable RizomUV licence was found (exit code 1). A headless RizomUV cannot show "
                "the licence dialog: open RizomUV normally once to check or activate its licence")
    if code in (2, 3):
        return "a fatal error stopped it during startup (exit code %d)" % code
    if code == 4:
        return "it printed its command line help instead of starting (exit code 4)"
    if code == 6:
        return "LM-X, the licensing library, failed to initialise (exit code 6)"
    if code == 8:
        return ("it did not understand its command line (exit code 8): this RizomUV is most likely "
                "too old for headless mode, which needs 2027.0.417 or later")
    if code < 0:
        try:
            name = signal.Signals(-code).name
        except ValueError:
            name = "signal %d" % -code
        return "it was killed by %s" % name
    if code in _NTSTATUS:
        return _NTSTATUS[code]
    if code >= 0xC0000000:
        return "it crashed (NTSTATUS 0x%08X)" % code
    return "it stopped with exit code %d" % code


def _log_tail(path, lines=6, width=200):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 8192))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    tail = [ln.strip() for ln in text.splitlines() if ln.strip()][-lines:]
    return [ln if len(ln) <= width else ln[:width] + "..." for ln in tail]


# ------------------------------------------------------------------ the launched instance

@dataclass
class LaunchedInstance:
    """A headless RizomUV this server started. Keep it as long as the instance lives:
    terminate() and the readiness checks go through it."""
    proc: subprocess.Popen
    port: int
    exe: Path
    log_path: Path
    job: object = None
    user_dir: Path | None = None
    launch_lock: LaunchLock | None = field(default=None, repr=False)

    @property
    def pid(self):
        return self.proc.pid

    def alive(self):
        return self.proc.poll() is None

    def exit_code(self):
        return self.proc.poll()

    def describe_exit(self, code=None):
        """The error text for an instance that stopped before it was ready."""
        code = self.proc.poll() if code is None else code
        msg = "RizomUV stopped before opening its link port %d: %s. Its output is in %s." % (
            self.port, explain_exit_code(code), self.log_path)
        if code == 8 and self.user_dir is not None:
            msg += " The command line error is in %s." % (Path(self.user_dir) / "logs" / "StartupError.log")
        tail = _log_tail(self.log_path)
        if tail:
            msg += "\nLast lines of its output:\n  " + "\n  ".join(tail)
        return msg

    def _release_launch_lock(self):
        if self.launch_lock is not None:
            self.launch_lock.release()
            self.launch_lock = None

    def check_ready(self):
        """One readiness poll: True once the instance listens on its port. Raises
        LaunchError if it stopped first, or if another process holds the port."""
        if port_listening(self.port):
            listeners = tcptable.listening_pids(self.port)
            if listeners and self.pid not in listeners:
                # someone bound the port between our bind test and RizomUV's own bind:
                # connecting would reach a stranger
                who = ", ".join("%d (%s)" % (p, tcptable._image_name(p)) for p in sorted(listeners))
                self.terminate()
                raise LaunchError("Port %d was taken by another process (pid %s) before RizomUV "
                                  "could open it; RizomUV was stopped. Try again." % (self.port, who))
            self._release_launch_lock()
            return True
        code = self.proc.poll()
        if code is not None:
            self._release_launch_lock()
            raise LaunchError(self.describe_exit(code))
        return False

    def wait_ready(self, timeout=180.0, period=0.25):
        """Block until check_ready(). On timeout the instance is stopped and LaunchError
        names its log: a RizomUV that does not answer is of no use and holds a seat."""
        deadline = time.monotonic() + timeout
        while not self.check_ready():
            if time.monotonic() > deadline:
                self.terminate()
                raise LaunchError("RizomUV did not open its link port %d within %.0f s and was stopped. "
                                  "Its output is in %s." % (self.port, timeout, self.log_path))
            time.sleep(period)

    def terminate(self, timeout=2.0):
        """Kill the instance (and on Windows everything in its job) without asking: for an
        instance that does not answer. An answering one is quit over the link instead.
        Returns the exit code, or None if it is somehow still running."""
        self._release_launch_lock()
        if self.proc.poll() is None:
            if self.job is not None:
                self.job.terminate()
            elif _WINDOWS:
                self.proc.kill()
            else:
                # RizomUV ignores SIGTERM on Linux (docs/build-linux.md): SIGKILL after a grace
                self._signal_group(signal.SIGTERM)
                try:
                    self.proc.wait(timeout / 2)
                except subprocess.TimeoutExpired:
                    self._signal_group(signal.SIGKILL)
        try:
            self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                pass
        if self.job is not None and self.proc.poll() is not None:
            self.job.close()
            _JOBS.discard(self.job)
            self.job = None
        return self.proc.poll()

    def _signal_group(self, sig):
        try:
            os.killpg(self.proc.pid, sig)
        except OSError:
            pass


def launch_headless(exe, *, port=None, owner_pid=None, user_dir=None, log_dir=None):
    """Start `exe -id <port> -hl` and return at once; the caller waits for readiness
    (check_ready / wait_ready) so that it can report progress meanwhile.

    port: a pair picked by pick_port_pair() when None. The launch lock on it is held until
    the instance listens (or stops), so a RunRizomUV() elsewhere never starts on it too.
    owner_pid: published by the app as "owner_pid" in its discovery file (default: us).
    user_dir: RIZOMUV_USER_DIR of the instance (default: state_dir/userdir). A private one,
    because a second instance on the artist's user dir rotates the artist's command log.
    RIZOMUV_LOCAL_DIR is left alone: the licence pointer lives there.
    """
    exe = Path(exe)
    owner_pid = os.getpid() if owner_pid is None else int(owner_pid)
    user_dir = Path(user_dir) if user_dir is not None else paths.state_dir("userdir")
    log_dir = Path(log_dir) if log_dir is not None else paths.state_dir("logs")
    user_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    if port is None:
        port = pick_port_pair()
    lock = LaunchLock(port)
    try:
        if not lock.acquire():
            raise LaunchError("Another process is starting RizomUV on port %d right now." % port)
    except OSError as e:
        raise LaunchError("Cannot create the launch lock %s: %s" % (lock.path, e)) from e
    try:
        # again under the lock: the pair may have been taken since it was picked
        if not _pair_free(port):
            raise LaunchError("Port %d or %d is already in use." % (port, port + 1))
        log_path = log_dir / ("rizomuv-%d.log" % port)
        env = dict(os.environ)
        env["RIZOMUV_LINK_OWNER_PID"] = str(owner_pid)
        env["RIZOMUV_USER_DIR"] = str(user_dir)
        try:
            with open(log_path, "wb") as out:
                proc, job = spawn_in_job([str(exe), "-id", str(port), "-hl"], cwd=str(exe.parent),
                                         env=env, stdout=out, stderr=subprocess.STDOUT)
        except OSError as e:
            raise LaunchError("Cannot start %s: %s" % (exe, e)) from e
    except BaseException:
        lock.release()
        raise
    log.info("started headless RizomUV pid %d on port %d (%s), output in %s", proc.pid, port, exe, log_path)
    return LaunchedInstance(proc=proc, port=port, exe=exe, log_path=log_path, job=job,
                            user_dir=user_dir, launch_lock=lock)
