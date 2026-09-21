"""rizomuv-mcp.exe, the Windows launcher MCP clients start (mcp/launcher/rizomuv-mcp.cpp).

Two layers. The launcher's own contract is checked against a stub server written into a
temp dir: install resolution, arguments and environment handed to the child, exit codes,
stdout left to the child, and the kill-on-close job that takes the whole process tree down
with the launcher. Then the real chain -- launcher, embedded Python, boot.py, the vendored
SDK, the server -- is driven over raw pipes exactly as a client does: initialize and
tools/list, nothing but JSON-RPC on stdout, and a killed launcher leaving nothing behind.

Nothing here starts RizomUV: the real server is only asked for initialize and tools/list,
with --instance attach and private state and instance dirs.

The launcher under test: RIZOMUV_MCP_TEST_LAUNCHER, else RizomUVApp/bin/rizomuv-mcp.exe
(makefiledist.php, or mcp/launcher/build.cmd). The Python it runs: RIZOMUV_MCP_TEST_PYTHON,
else the python.exe next to the RizomUV the other tests use (conftest's snap_exe) -- the
embeddable CPython an install ships. Each test skips when what it needs is missing.
"""
import ctypes
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the launcher is Windows-only")

MCP_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = MCP_DIR.parents[2]
BOOT = MCP_DIR / "boot.py"

EXPECTED_TOOLS = {"session_info", "connect", "load", "unfold", "pack", "measure", "diagnose",
                  "render_layout", "save", "undo", "run_command"}

DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000

# Speaks just enough MCP over stdio to be driven like the real server, and reports what the
# launcher handed it. Stdlib only: it runs under the embeddable CPython with -I -S.
STUB_SERVER = r'''
import ctypes, json, os, subprocess, sys

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    msg = json.loads(line)
    rid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if rid is None:
        continue
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion"), "capabilities": {"tools": {}},
                  "serverInfo": {"name": "launcher-stub", "version": "0"}}
    elif method == "tools/call" and params["name"] == "probe":
        result = {"pid": os.getpid(), "executable": sys.executable, "argv": sys.argv,
                  "isolated": sys.flags.isolated, "no_site": sys.flags.no_site,
                  "utf8_mode": sys.flags.utf8_mode,
                  "console_window": bool(ctypes.WinDLL("kernel32").GetConsoleWindow()),
                  "env": {k: os.environ.get(k) for k in ("RIZOMUV_MCP_APP_DIR", "RIZOMUV_MCP_LAUNCHER")}}
    elif method == "tools/call" and params["name"] == "spawn_sleeper":
        p = subprocess.Popen([sys.executable, "-I", "-S", "-c", "import time; time.sleep(300)"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        result = {"pid": p.pid}
    elif method == "tools/call" and params["name"] == "exit":
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
        sys.exit(int(params["arguments"]["code"]))
    else:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": method}})
        continue
    send({"jsonrpc": "2.0", "id": rid, "result": result})
'''

# ------------------------------------------------------------------ processes (Win32)

if sys.platform == "win32":
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    _k32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)


class _ProcessEntry(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]


def pid_alive(pid):
    handle = _k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    code = wintypes.DWORD()
    _k32.GetExitCodeProcess(handle, ctypes.byref(code))
    _k32.CloseHandle(handle)
    return code.value == 259   # STILL_ACTIVE


def kill_pid(pid):
    handle = _k32.OpenProcess(0x0001, False, pid)   # PROCESS_TERMINATE
    if handle:
        _k32.TerminateProcess(handle, 1)
        _k32.CloseHandle(handle)


def descendants(root_pid):
    """Every live process below root_pid, from one toolhelp snapshot."""
    snap = _k32.CreateToolhelp32Snapshot(0x2, 0)   # TH32CS_SNAPPROCESS
    if not snap or snap == wintypes.HANDLE(-1).value:
        return []
    children = {}
    entry = _ProcessEntry()
    entry.dwSize = ctypes.sizeof(entry)
    ok = _k32.Process32FirstW(snap, ctypes.byref(entry))
    while ok:
        children.setdefault(entry.th32ParentProcessID, []).append(entry.th32ProcessID)
        ok = _k32.Process32NextW(snap, ctypes.byref(entry))
    _k32.CloseHandle(snap)
    found, todo = [], [root_pid]
    while todo:
        for child in children.get(todo.pop(), []):
            if child != root_pid and child not in found:
                found.append(child)
                todo.append(child)
    return found


def wait_dead(pids, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(pid_alive(p) for p in pids):
            return []
        time.sleep(0.1)
    return [p for p in pids if pid_alive(p)]


# ------------------------------------------------------------------ an MCP client over pipes

class McpPipe:
    """Drives a server the way an MCP client does: newline-delimited JSON-RPC on its stdin,
    responses matched by id on its stdout, stderr collected on the side. Every raw stdout
    line is kept, so a test can prove nothing but JSON-RPC ever reached the wire."""

    def __init__(self, args, env, creationflags=0, cwd=None):
        self.proc = subprocess.Popen([str(a) for a in args], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                                     creationflags=creationflags, cwd=cwd)
        self.stdout_lines = []
        self.stderr_lines = []
        self._responses = queue.Queue()
        self._next_id = 0
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self):
        for line in self.proc.stdout:
            self.stdout_lines.append(line)
            try:
                self._responses.put(json.loads(line))
            except ValueError:
                pass

    def _read_stderr(self):
        for line in self.proc.stderr:
            self.stderr_lines.append(line.decode("utf-8", "replace").rstrip())

    def notify(self, method, params=None):
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def request(self, method, params=None, timeout=60.0):
        self._next_id += 1
        rid = self._next_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = self._responses.get(timeout=0.2)
            except queue.Empty:
                if self.proc.poll() is not None:
                    raise AssertionError("the server exited (%s) before answering %s; stderr:\n%s"
                                         % (self.proc.returncode, method, "\n".join(self.stderr_lines[-20:])))
                continue
            if msg.get("id") == rid:
                return msg
        raise AssertionError("no answer to %s within %.0f s; stderr:\n%s"
                             % (method, timeout, "\n".join(self.stderr_lines[-20:])))

    def initialize(self, timeout=60.0):
        response = self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                               "clientInfo": {"name": "test_launcher", "version": "0"}},
                                timeout)
        self.notify("notifications/initialized")
        return response["result"]

    def call(self, tool, arguments=None):
        return self.request("tools/call", {"name": tool, "arguments": arguments or {}})["result"]

    def close_stdin_and_wait(self, timeout=30.0):
        self.proc.stdin.close()
        return self.proc.wait(timeout)

    def kill_tree(self):
        """Test hygiene: only ever the launcher and what runs below it."""
        tree = descendants(self.proc.pid) if self.proc.poll() is None else []
        self.proc.kill()
        for pid in tree:
            kill_pid(pid)

    def _send(self, obj):
        self.proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        self.proc.stdin.flush()


# ------------------------------------------------------------------ fixtures

def _existing(path):
    return Path(path) if path and Path(path).is_file() else None


@pytest.fixture(scope="module")
def launcher():
    path = Path(os.environ.get("RIZOMUV_MCP_TEST_LAUNCHER")
                or REPO_ROOT / "RizomUVApp" / "bin" / "rizomuv-mcp.exe")
    if not path.is_file():
        pytest.skip("no launcher at %s: build it with mcp/launcher/build.cmd" % path)
    return path


@pytest.fixture(scope="module")
def python_exe(request):
    override = _existing(os.environ.get("RIZOMUV_MCP_TEST_PYTHON"))
    if override:
        return override
    # The embeddable CPython sits next to rizomuv.exe, in a bin dir as in an install.
    python = request.getfixturevalue("snap_exe").parent / "python.exe"
    if not python.is_file():
        pytest.skip("no python.exe next to %s (set RIZOMUV_MCP_TEST_PYTHON)" % python.parent)
    return python


@pytest.fixture
def fake_install(tmp_path):
    """The layout the launcher recognises as an install; the files only have to exist."""
    app = tmp_path / "app"
    (app / "RizomUVLink" / "mcp").mkdir(parents=True)
    (app / "python.exe").write_bytes(b"")
    (app / "RizomUVLink" / "mcp" / "boot.py").write_bytes(b"")
    return app


@pytest.fixture
def stub_install(tmp_path):
    """An install dir holding only the stub server as its boot script: the python comes from
    RIZOMUV_MCP_PYTHON, so nothing of the runtime has to be copied."""
    app = tmp_path / "stub-app"
    (app / "RizomUVLink" / "mcp").mkdir(parents=True)
    (app / "RizomUVLink" / "mcp" / "boot.py").write_text(STUB_SERVER, encoding="utf-8")
    return app


def clean_env(**overrides):
    # Anything steering the launcher or the server from the developer's shell would make
    # these tests test that shell instead; the vendor override is the one worth keeping.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("RIZOMUV_MCP_") or k == "RIZOMUV_MCP_VENDOR"}
    env.update({k: str(v) for k, v in overrides.items()})
    return env


def run_launcher(launcher, args=(), env=None, cwd=None):
    return subprocess.run([str(launcher), *args], env=env or clean_env(), cwd=cwd,
                          stdin=subprocess.DEVNULL, capture_output=True, timeout=60)


def stderr_lines(result):
    return result.stderr.decode("utf-8", "replace").splitlines()


def same_path(a, b):
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


# ------------------------------------------------------------------ install resolution

def test_launcher_info_from_app_dir(launcher, fake_install):
    result = run_launcher(launcher, ["--launcher-info"], clean_env(RIZOMUV_MCP_APP_DIR=fake_install))
    assert result.returncode == 0
    assert result.stdout == b"", "the launcher must never write to stdout: it is the child's wire"
    lines = stderr_lines(result)
    python = fake_install / "python.exe"
    boot = fake_install / "RizomUVLink" / "mcp" / "boot.py"
    assert lines == [
        "rizomuv-mcp: install  : %s   [from RIZOMUV_MCP_APP_DIR]" % fake_install,
        "rizomuv-mcp: python   : %s" % python,
        "rizomuv-mcp: boot     : %s" % boot,
        'rizomuv-mcp: command  : "%s" -I -S -X utf8 "%s"' % (python, boot),
    ]


def test_launcher_info_with_both_files_overridden(launcher, fake_install):
    python = fake_install / "python.exe"
    boot = fake_install / "RizomUVLink" / "mcp" / "boot.py"
    result = run_launcher(launcher, ["--launcher-info"],
                          clean_env(RIZOMUV_MCP_PYTHON=python, RIZOMUV_MCP_BOOT=boot))
    assert result.returncode == 0 and result.stdout == b""
    assert stderr_lines(result)[0] == "rizomuv-mcp: install  : (none, both files overridden)"


def test_a_launcher_inside_an_install_runs_that_install(launcher, fake_install):
    # The copy pinned in {app}: its own directory beats the registry.
    pinned = fake_install / "rizomuv-mcp.exe"
    shutil.copy2(launcher, pinned)
    result = run_launcher(pinned, ["--launcher-info"])
    assert result.returncode == 0 and result.stdout == b""
    assert stderr_lines(result)[0] == \
        "rizomuv-mcp: install  : %s   [from the launcher's own directory]" % fake_install


def test_a_relative_app_dir_reaches_the_child_absolute(launcher, fake_install):
    # The server resolves rizomuv.exe against RIZOMUV_MCP_APP_DIR, from whatever its cwd is.
    result = run_launcher(launcher, ["--launcher-info"], clean_env(RIZOMUV_MCP_APP_DIR="app"),
                          cwd=fake_install.parent)
    assert result.returncode == 0
    assert stderr_lines(result)[0] == "rizomuv-mcp: install  : %s   [from RIZOMUV_MCP_APP_DIR]" % fake_install


@pytest.mark.parametrize("variable, value, message", [
    ("RIZOMUV_MCP_APP_DIR", "nope", "RIZOMUV_MCP_APP_DIR is set but {path} has no python.exe"),
    ("RIZOMUV_MCP_PYTHON", "nope\\python.exe", "RIZOMUV_MCP_PYTHON is set but is not a file: {path}"),
    ("RIZOMUV_MCP_BOOT", "nope\\boot.py", "RIZOMUV_MCP_BOOT is set but is not a file: {path}"),
])
def test_an_override_pointing_at_nothing_exits_4(launcher, tmp_path, variable, value, message):
    # An explicit choice is never second-guessed: no fallback to another install.
    path = tmp_path / value
    result = run_launcher(launcher, [], clean_env(**{variable: path}))
    assert result.returncode == 4
    assert result.stdout == b""
    assert stderr_lines(result) == ["rizomuv-mcp: " + message.format(path=path)]


def test_an_app_dir_without_the_mcp_server_exits_4(launcher, fake_install):
    (fake_install / "RizomUVLink" / "mcp" / "boot.py").unlink()
    result = run_launcher(launcher, [], clean_env(RIZOMUV_MCP_APP_DIR=fake_install))
    assert result.returncode == 4 and result.stdout == b""
    assert stderr_lines(result) == ["rizomuv-mcp: RIZOMUV_MCP_APP_DIR is set but %s has no "
                                    "RizomUVLink\\mcp\\boot.py" % fake_install]


def test_no_install_found_exits_3(launcher, tmp_path):
    # A bare copy, so its own directory is no install; what is left is the registry.
    bare = tmp_path / "rizomuv-mcp.exe"
    shutil.copy2(launcher, bare)
    result = run_launcher(bare, ["--launcher-info"])
    if result.returncode == 0:
        pytest.skip("this machine has an installed RizomUV with the MCP server: %s"
                    % stderr_lines(result)[0])
    assert result.returncode == 3
    assert result.stdout == b""
    assert stderr_lines(result)[-1] == (
        "rizomuv-mcp: no RizomUV installation with the MCP server was found. Install RizomUV "
        "2027.0 or later, or set RIZOMUV_MCP_APP_DIR to an installation directory.")


# ------------------------------------------------------------------ the child: stub server

def _stub_session(launcher, python_exe, stub_install, args=(), creationflags=0):
    env = clean_env(RIZOMUV_MCP_APP_DIR=stub_install, RIZOMUV_MCP_PYTHON=python_exe)
    return McpPipe([launcher, *args], env, creationflags=creationflags)


def test_the_child_gets_the_contract(launcher, python_exe, stub_install):
    session = _stub_session(launcher, python_exe, stub_install, ["--instance", "attach", "a b"])
    try:
        assert session.initialize()["serverInfo"]["name"] == "launcher-stub"
        probe = session.call("probe")
        assert same_path(probe["executable"], python_exe)
        assert (probe["isolated"], probe["no_site"], probe["utf8_mode"]) == (1, 1, 1)
        assert same_path(probe["argv"][0], stub_install / "RizomUVLink" / "mcp" / "boot.py")
        assert probe["argv"][1:] == ["--instance", "attach", "a b"], "arguments must pass verbatim"
        assert same_path(probe["env"]["RIZOMUV_MCP_APP_DIR"], stub_install)
        assert same_path(probe["env"]["RIZOMUV_MCP_LAUNCHER"], launcher)

        sleeper = session.call("spawn_sleeper")["pid"]
        assert pid_alive(sleeper)
        # EOF is how a client asks a stdio server to stop: the launcher returns the child's
        # code, and closing its job takes down whatever the server left running.
        assert session.close_stdin_and_wait() == 0
        assert wait_dead([probe["pid"], sleeper]) == []
        assert all(line.startswith(b"{") for line in session.stdout_lines)
    finally:
        session.kill_tree()


def test_the_exit_code_is_the_childs(launcher, python_exe, stub_install):
    session = _stub_session(launcher, python_exe, stub_install)
    try:
        session.initialize()
        session.call("exit", {"code": 7})
        assert session.proc.wait(30) == 7
    finally:
        session.kill_tree()


def test_killing_the_launcher_kills_the_whole_tree(launcher, python_exe, stub_install):
    # TerminateProcess is how MCP clients stop a server on Windows: nothing may survive it,
    # least of all a headless RizomUV holding a licence seat.
    session = _stub_session(launcher, python_exe, stub_install)
    try:
        session.initialize()
        python_pid = session.call("probe")["pid"]
        sleeper = session.call("spawn_sleeper")["pid"]
        assert pid_alive(python_pid) and pid_alive(sleeper)
        session.proc.kill()
        session.proc.wait(10)
        assert wait_dead([python_pid, sleeper]) == []
    finally:
        session.kill_tree()


@pytest.mark.parametrize("flag", [DETACHED_PROCESS, CREATE_NO_WINDOW],
                         ids=["DETACHED_PROCESS", "CREATE_NO_WINDOW"])
def test_no_console_window_whatever_the_client_passes(launcher, python_exe, stub_install, flag):
    # Electron clients pass windowsHide (CREATE_NO_WINDOW), some pass DETACHED_PROCESS: a
    # console window popping up on the artist's desktop would be the bug either way.
    session = _stub_session(launcher, python_exe, stub_install, creationflags=flag)
    try:
        session.initialize()
        assert session.call("probe")["console_window"] is False
        assert session.close_stdin_and_wait() == 0
    finally:
        session.kill_tree()


# ------------------------------------------------------------------ the real chain

@pytest.fixture
def real_server(launcher, python_exe, vendor_dir, tmp_path):
    if not BOOT.is_file():
        pytest.skip("no %s" % BOOT)
    env = clean_env(RIZOMUV_MCP_PYTHON=python_exe, RIZOMUV_MCP_BOOT=BOOT,
                    RIZOMUV_MCP_STATE_DIR=tmp_path / "state",
                    RIZOMUV_INSTANCES_DIR=tmp_path / "instances")
    session = McpPipe([launcher, "--instance", "attach"], env)
    yield session
    session.kill_tree()


def test_the_real_server_answers_through_the_launcher(real_server):
    # The first import of the vendor after it was written can take tens of seconds (the
    # antivirus scans every new file once), hence the long first timeout.
    init = real_server.initialize(timeout=180)
    assert init["protocolVersion"]
    assert init["serverInfo"]["name"]
    tools = {t["name"] for t in real_server.request("tools/list")["result"]["tools"]}
    assert EXPECTED_TOOLS <= tools, "missing tools: %s" % sorted(EXPECTED_TOOLS - tools)

    assert real_server.close_stdin_and_wait() == 0
    for line in real_server.stdout_lines:
        assert json.loads(line).get("jsonrpc") == "2.0", "not JSON-RPC on stdout: %r" % line[:200]


def test_killing_the_launcher_leaves_no_server_behind(real_server):
    real_server.initialize(timeout=180)
    real_server.request("tools/list")
    tree = descendants(real_server.proc.pid)
    assert tree, "the launcher has no child"
    real_server.proc.kill()
    real_server.proc.wait(10)
    assert wait_dead(tree) == [], "still running after the launcher was killed"
