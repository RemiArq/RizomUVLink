"""Finding rizomuv.exe, port pairs, the launch lock shared with RizomUVLink.py, the job
object, exit codes, and one real headless launch."""
import os
import signal
import socket
import subprocess
import sys
import textwrap
import time

import pytest

from rizomuv_mcp import discovery, launch, paths, tcptable

WINDOWS = sys.platform == "win32"
windows_only = pytest.mark.skipif(not WINDOWS, reason="Windows launch path")


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


# ------------------------------------------------------------------ finding the executable

@pytest.fixture
def no_install(monkeypatch, tmp_path):
    """No registry install, no env, and a package dir with nothing around it."""
    for var in ("RIZOMUV_EXE", "RIZOMUV_MCP_APP_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(launch, "registry_installs", lambda: [])
    monkeypatch.setattr(launch, "mac_bundles", lambda: [])
    pkg = tmp_path / "repo" / "RizomUVLink" / "RizomUVLink"
    pkg.mkdir(parents=True)
    monkeypatch.setattr(paths, "package_dir", lambda: pkg)
    return pkg


def _touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


def test_an_explicit_exe_wins_and_is_never_second_guessed(no_install, tmp_path, monkeypatch):
    exe = _touch(tmp_path / "custom" / "rizomuv.exe")
    other = _touch(tmp_path / "other" / "rizomuv.exe")
    monkeypatch.setenv("RIZOMUV_EXE", str(other))
    assert launch.find_rizomuv_exe(str(exe)) == (exe, "--exe")
    assert launch.find_rizomuv_exe() == (other, "RIZOMUV_EXE")
    with pytest.raises(launch.LaunchError, match="--exe names .*nope"):
        launch.find_rizomuv_exe(str(tmp_path / "nope.exe"))
    monkeypatch.setenv("RIZOMUV_EXE", str(tmp_path / "gone.exe"))
    with pytest.raises(launch.LaunchError, match="RIZOMUV_EXE names"):
        launch.find_rizomuv_exe()


@windows_only
def test_the_search_order(no_install, tmp_path, monkeypatch):
    dev = _touch(no_install.parent.parent / "RizomUVApp" / "bin" / "rizomuv.exe")
    assert launch.find_rizomuv_exe() == (dev, "the development tree")
    registered = _touch(tmp_path / "Program Files" / "RizomUV 2027.0" / "rizomuv.exe")
    monkeypatch.setattr(launch, "registry_installs",
                        lambda: [((2027, 0), "HKLM\\SOFTWARE\\Rizom Lab\\RizomUV VS RS 2027.0", registered.parent)])
    assert launch.find_rizomuv_exe() == (registered, "HKLM\\SOFTWARE\\Rizom Lab\\RizomUV VS RS 2027.0")
    shipped = _touch(no_install.parent / "rizomuv.exe")
    assert launch.find_rizomuv_exe() == (shipped, "the install this server ships with")
    app = _touch(tmp_path / "app" / "rizomuv.exe")
    monkeypatch.setenv("RIZOMUV_MCP_APP_DIR", str(app.parent))
    assert launch.find_rizomuv_exe() == (app, "RIZOMUV_MCP_APP_DIR")


@windows_only
def test_not_found_lists_every_place_tried(no_install, tmp_path, monkeypatch):
    monkeypatch.setenv("RIZOMUV_MCP_APP_DIR", str(tmp_path / "app"))
    with pytest.raises(launch.LaunchError) as info:
        launch.find_rizomuv_exe()
    msg = str(info.value)
    for fragment in ("RizomUV was not found", "RIZOMUV_MCP_APP_DIR: ", "the install this server ships with: ",
                     "the development tree: ", "--exe", "RIZOMUV_EXE"):
        assert fragment in msg


@windows_only
def test_the_registry_is_readable():
    for version, source, install in launch.registry_installs():   # none on a build box: no crash either
        assert isinstance(version, tuple) and source.startswith("HKLM") and install.name


# ------------------------------------------------------------------ ports and the launch lock

def test_a_picked_pair_is_free():
    port = launch.pick_port_pair()
    assert launch.PORT_MIN <= port <= launch.PORT_MAX
    for p in (port, port + 1):
        with socket.socket() as s:
            s.bind(("127.0.0.1", p))


def test_a_pair_whose_neighbour_is_taken_is_skipped():
    port = launch.pick_port_pair()
    with socket.socket() as s:
        s.bind(("127.0.0.1", port + 1))
        s.listen()
        assert not launch._pair_free(port)
        assert launch.pick_port_pair(start=port) not in (port, port + 1)


@windows_only
def test_a_port_held_on_the_wildcard_address_is_not_free():
    port = launch.pick_port_pair()
    with socket.socket() as s:
        s.bind(("0.0.0.0", port))
        s.listen()
        # Windows accepts the specific bind anyway: only the listener table tells
        assert launch._bindable(port)
        assert not launch._pair_free(port)
        assert launch.pick_port_pair(start=port) != port


def test_the_launch_lock_is_reentrant_within_this_process():
    port = launch.pick_port_pair()
    outer, inner = launch.LaunchLock(port), launch.LaunchLock(port)
    assert outer.acquire() and inner.acquire()
    assert not launch.LaunchLock.free(port)
    inner.release()
    assert not launch.LaunchLock.free(port), "still held by the outer one"
    outer.release()
    assert launch.LaunchLock.free(port)


_CLAUNCHLOCK_CHILD = r"""
import sys
sys.path.append(sys.argv[1])
from RizomUVLink import CLaunchLock
lock = CLaunchLock(int(sys.argv[2]))
ok = lock.Acquire()
print("locked" if ok else "refused", flush=True)
if ok:
    sys.stdin.read()
"""


def test_the_launch_lock_is_the_one_of_rizomuvlink(pkg_dir):
    """RunRizomUV() in a DCC bridge and this server must refuse each other."""
    port = launch.pick_port_pair()
    with launch.LaunchLock(port):
        child = subprocess.run([sys.executable, "-c", _CLAUNCHLOCK_CHILD, str(pkg_dir), str(port)],
                               input="", capture_output=True, text=True, timeout=60)
        assert child.stdout.strip() == "refused", child.stderr
    bridge = subprocess.Popen([sys.executable, "-c", _CLAUNCHLOCK_CHILD, str(pkg_dir), str(port)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert bridge.stdout.readline().strip() == "locked"
        assert not launch.LaunchLock(port).acquire()
        assert not launch.LaunchLock.free(port)
        assert launch.pick_port_pair(start=port) != port, "a port someone is launching on is skipped"
    finally:
        bridge.kill()
        bridge.wait()
    assert launch.LaunchLock.free(port)


# ------------------------------------------------------------------ exit codes

@pytest.mark.parametrize("code, words", [
    (1, "licence"), (2, "fatal error"), (3, "fatal error"), (6, "LM-X"), (8, "2027.0.417"),
    (0xC0000005, "access violation"), (0xC0000374, "heap corruption"), (0xC0001234, "0xC0001234"),
    (0, "quit normally"), (42, "exit code 42"),
])
def test_exit_codes_are_explained(code, words):
    assert words in launch.explain_exit_code(code)


def test_a_posix_signal_is_named():
    # Windows' signal module has no SIGKILL: the number is all there is to say there
    expected = "SIGKILL" if hasattr(signal, "SIGKILL") else "signal 9"
    assert expected in launch.explain_exit_code(-9)


# ------------------------------------------------------------------ launches with a fake exe

def _fake_exe(tmp_path, body):
    """A .bat standing in for rizomuv.exe: CreateProcess runs it through cmd.exe."""
    path = tmp_path / "fake rizomuv" / "rizomuv.bat"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("@echo off\r\n" + body.replace("\n", "\r\n"), encoding="ascii")
    return path


@windows_only
def test_an_instance_that_stops_early_is_explained(tmp_path, isolated_dirs):
    exe = _fake_exe(tmp_path, "echo fake startup line %*\necho owner=%RIZOMUV_LINK_OWNER_PID% "
                              "userdir=%RIZOMUV_USER_DIR%\nexit /b 8\n")
    port = launch.pick_port_pair()
    inst = launch.launch_headless(exe, port=port)
    assert inst.log_path == isolated_dirs.state / "logs" / ("rizomuv-%d.log" % port)
    assert _wait_for(lambda: not inst.alive(), 10)
    with pytest.raises(launch.LaunchError) as info:
        inst.check_ready()
    msg = str(info.value)
    assert "exit code 8" in msg and "2027.0.417" in msg and str(inst.log_path) in msg
    assert "StartupError.log" in msg
    log = inst.log_path.read_text()
    assert "fake startup line -id %d -hl" % port in log, "the command line the app receives"
    assert "owner=%d" % os.getpid() in log
    assert "userdir=%s" % (isolated_dirs.state / "userdir") in log
    assert "fake startup line" in msg, "the tail of the output is quoted"
    assert launch.LaunchLock.free(port), "the launch lock is released once the outcome is known"


@windows_only
def test_an_instance_that_never_listens_is_stopped(tmp_path, isolated_dirs):
    exe = _fake_exe(tmp_path, "ping -n 30 127.0.0.1 > nul\n")
    port = launch.pick_port_pair()
    inst = launch.launch_headless(exe, port=port)
    assert not launch.LaunchLock.free(port), "held while the instance comes up"
    with pytest.raises(launch.LaunchError, match="within 1 s.*stopped"):
        inst.wait_ready(timeout=1.0)
    assert not inst.alive() and not discovery.pid_alive(inst.pid)
    assert inst.job is None, "the job is closed with the instance"
    assert launch.LaunchLock.free(port)


@windows_only
def test_a_missing_exe_is_a_launch_error(tmp_path, isolated_dirs):
    port = launch.pick_port_pair()
    with pytest.raises(launch.LaunchError, match="Cannot start"):
        launch.launch_headless(tmp_path / "nope.exe", port=port)
    assert launch.LaunchLock.free(port)


# ------------------------------------------------------------------ the job object

_JOB_CHILD = textwrap.dedent(r"""
    import site, sys
    site.addsitedir(sys.argv[1])
    sys.path.append(sys.argv[2])
    from rizomuv_mcp import launch
    proc, job = launch.spawn_in_job([sys.executable, "-c", "import time; time.sleep(120)"],
                                    job_backend=sys.argv[3])
    print(proc.pid, job.backend if job else None, flush=True)
    sys.stdin.read()
""")


@windows_only
@pytest.mark.parametrize("backend", ["pywin32", "ctypes"])
def test_killing_the_job_owner_kills_what_it_started(backend, vendor_dir, mcp_dir):
    """What happens to a headless RizomUV when an MCP client kills the server."""
    owner = subprocess.Popen([sys.executable, "-c", _JOB_CHILD, str(vendor_dir), str(mcp_dir), backend],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        pid, used = owner.stdout.readline().split()
        pid = int(pid)
        assert used == backend
        assert discovery.pid_alive(pid)
    finally:
        owner.kill()      # TerminateProcess: no atexit, no cleanup, only the kernel
        owner.wait()
    assert _wait_for(lambda: not discovery.pid_alive(pid), 10), "the sleeper outlived its job owner"


# ------------------------------------------------------------------ a real headless RizomUV

@windows_only
@pytest.mark.live
@pytest.mark.slow
def test_a_real_headless_launch(snap_exe, isolated_dirs):
    port = launch.pick_port_pair()
    inst = launch.launch_headless(snap_exe, port=port)
    try:
        assert inst.job is not None and inst.job.backend == "pywin32"
        assert not launch.LaunchLock.free(port)
        inst.wait_ready(timeout=180)
        assert launch.LaunchLock.free(port)
        assert launch.port_listening(port)
        assert inst.pid in tcptable.listening_pids(port)
        listening = "Listening RizomUVLink requests on url: tcp://127.0.0.1:%d" % port
        assert _wait_for(lambda: listening in inst.log_path.read_text(errors="replace"), 20), \
            "the instance's output goes to its log file"
        assert (isolated_dirs.state / "userdir").is_dir()
        assert inst.alive()
    finally:
        code = inst.terminate()
    assert code is not None and not inst.alive()
    assert _wait_for(lambda: not discovery.pid_alive(inst.pid), 10)
    assert not launch.port_listening(port)
