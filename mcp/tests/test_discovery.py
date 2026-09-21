"""Discovery files, pid liveness, the client lock, and the TCP table check."""
import json
import os
import socket
import subprocess
import sys
import time

import pytest

from rizomuv_mcp import discovery, paths, tcptable

WINDOWS = sys.platform == "win32"


def _record(pid, **over):
    data = {"schema": 1, "pid": pid, "process_start": discovery.process_start_ticks(pid) or 0,
            "port": 50000, "notify_port": 50001, "endpoint": "tcp://127.0.0.1:50000",
            "port_source": "default", "headless": False, "scripted_launch": False,
            "version": "2027.0.999.gabcdef12", "version_major": 2027, "version_minor": 0,
            "exe": sys.executable, "user_dir": "C:\\Users\\x\\Documents\\RizomUV\\2027\\",
            "token": "9f" * 16, "owner_pid": None, "started_unix": 1789122130}
    data.update(over)
    return data


def _write(directory, data, name=None):
    path = directory / (name or "%s.json" % data["pid"])
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


# ------------------------------------------------------------------ directories

def test_instances_dir_follows_the_app_rule(monkeypatch, tmp_path):
    monkeypatch.setenv("RIZOMUV_INSTANCES_DIR", str(tmp_path / "custom"))
    assert paths.instances_dir() == tmp_path / "custom"
    assert not (tmp_path / "custom").exists(), "a reader must not create the directory"
    monkeypatch.delenv("RIZOMUV_INSTANCES_DIR")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lad"))
    if WINDOWS:
        assert paths.instances_dir() == tmp_path / "lad" / "rizomuv" / "instances"
    elif sys.platform == "darwin":
        assert str(paths.instances_dir()).endswith("Library/Application Support/RizomUV/instances")
    else:
        assert str(paths.instances_dir()).endswith(".rizomuv/instances")


def test_state_dir_is_created_on_demand(isolated_dirs):
    assert paths.state_dir() == isolated_dirs.state
    logs = paths.state_dir("logs")
    assert logs == isolated_dirs.state / "logs" and logs.is_dir()


# ------------------------------------------------------------------ records

def test_a_full_record_is_read(isolated_dirs):
    path = _write(isolated_dirs.instances, _record(os.getpid(), owner_pid=4242, headless=True,
                                                   port_source="command_line", notify_port=None))
    rec = discovery.read_record(path)
    assert rec.pid == os.getpid() and rec.port == 50000 and rec.notify_port is None
    assert rec.owner_pid == 4242 and rec.headless and rec.port_source == "command_line"
    assert rec.token == "9f" * 16 and rec.version == "2027.0.999.gabcdef12"
    assert rec.path == path and not rec.artist_session
    assert discovery.read_record(_write(isolated_dirs.instances, _record(os.getpid()))).artist_session


def test_only_live_well_formed_records_are_listed(isolated_dirs, dead_pid):
    d = isolated_dirs.instances
    live = _write(d, _record(os.getpid(), started_unix=100))
    _write(d, _record(dead_pid, started_unix=200))
    (d / "1234.json").write_text("{not json", encoding="utf-8")
    _write(d, _record(5678), name="5678.json.tmp")                  # being written
    _write(d, _record(os.getpid(), schema=2), name="1.json")         # another schema (and pid mismatch)
    rec = _record(2222)
    del rec["token"]
    _write(d, rec)                                                   # incomplete
    _write(d, _record(3333, port="50000"))                           # wrong type
    _write(d, _record(3334, port=0))                                 # impossible port
    _write(d, _record(os.getpid()), name="9999.json")                # pid is not the file's
    (d / ("%d.lock" % os.getpid())).write_bytes(b"")                 # a client lock, not a record

    assert [r.path for r in discovery.list_instances()] == [live]
    everything = discovery.list_instances(alive_only=False)
    assert [r.pid for r in everything] == [dead_pid, os.getpid()], "newest first, dead included"


def test_missing_directory_lists_nothing(tmp_path):
    assert discovery.list_instances(tmp_path / "nowhere") == []


@pytest.mark.skipif(not WINDOWS, reason="process identity is checked on Windows only")
def test_a_reused_pid_is_not_taken_for_the_instance(isolated_dirs):
    d = isolated_dirs.instances
    ticks = discovery.process_start_ticks(os.getpid())
    assert ticks and ticks > 10 ** 17, "FILETIME ticks of this process"
    _write(d, _record(os.getpid(), process_start=ticks + 1))
    assert discovery.list_instances() == [], "another process with the same pid"
    _write(d, _record(os.getpid(), exe="C:\\Program Files\\Rizom Lab\\RizomUV 2027.0\\rizomuv.exe"))
    assert discovery.list_instances() == [], "the pid now runs another executable"
    _write(d, _record(os.getpid(), exe=sys.executable.upper()))
    assert len(discovery.list_instances()) == 1, "image names compare case-insensitively"


def test_an_unparsable_file_is_read_again_once(isolated_dirs, monkeypatch):
    path = _write(isolated_dirs.instances, _record(os.getpid()))
    good = path.read_text(encoding="utf-8")
    reads = iter(["{torn", good])
    monkeypatch.setattr(discovery, "_load_text", lambda p: next(reads))
    assert discovery.read_record(path).pid == os.getpid()
    reads = iter(["{torn", "{still torn", good])
    monkeypatch.setattr(discovery, "_load_text", lambda p: next(reads))
    assert discovery.read_record(path) is None


def test_pid_alive(dead_pid):
    assert discovery.pid_alive(os.getpid())
    assert not discovery.pid_alive(dead_pid)
    assert not discovery.pid_alive(0) and not discovery.pid_alive(-1)
    if WINDOWS:
        assert discovery.pid_alive(4), "the System process: not ours to open, but alive"


def test_lock_paths(isolated_dirs):
    rec = discovery.read_record(_write(isolated_dirs.instances, _record(os.getpid())))
    assert discovery.lock_path(rec) == isolated_dirs.instances / ("%d.lock" % os.getpid())
    assert discovery.lock_path(17544) == isolated_dirs.instances / "17544.lock"
    assert discovery.port_lock_path(54587) == isolated_dirs.instances / "port-54587.lock"


# ------------------------------------------------------------------ the client lock

_LOCK_CHILD = r"""
import sys
sys.path.append(sys.argv[1])
from rizomuv_mcp.discovery import InstanceLock
lock = InstanceLock(sys.argv[2])
ok = lock.acquire()
print("locked" if ok else "refused", flush=True)
if ok and sys.argv[3] == "hold":
    sys.stdin.read()
sys.exit(0 if ok else 1)
"""


def _lock_child(mcp_dir, path, mode):
    return subprocess.Popen([sys.executable, "-c", _LOCK_CHILD, str(mcp_dir), str(path), mode],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)


def test_the_lock_is_exclusive_across_processes(isolated_dirs, mcp_dir):
    path = discovery.lock_path(os.getpid())
    holder = _lock_child(mcp_dir, path, "hold")
    try:
        assert holder.stdout.readline().strip() == "locked"
        mine = discovery.InstanceLock(path)
        assert not mine.acquire() and not mine.held
        with pytest.raises(discovery.LockRefused):
            with discovery.InstanceLock(path):
                pass
    finally:
        holder.kill()   # not a clean release: the OS must drop the lock of a dead holder
        holder.wait()
    assert mine.acquire() and mine.held
    assert mine.acquire(), "acquire is idempotent for the holder"
    other = _lock_child(mcp_dir, path, "try")
    assert other.stdout.readline().strip() == "refused"
    assert other.wait(10) == 1
    mine.release()
    assert not mine.held
    again = _lock_child(mcp_dir, path, "try")
    assert again.stdout.readline().strip() == "locked"
    assert again.wait(10) == 0


def test_the_lock_refuses_a_second_handle_of_the_same_process(isolated_dirs):
    path = discovery.port_lock_path(50000)
    with discovery.InstanceLock(path) as first:
        assert first.held
        assert not discovery.InstanceLock(path).acquire()
    assert discovery.InstanceLock(path).acquire()


# ------------------------------------------------------------------ TCP table

pytestmark_tcp = pytest.mark.skipif(not WINDOWS, reason="the TCP table check is Windows only")


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


@pytestmark_tcp
def test_a_client_of_the_port_is_seen_until_it_disconnects():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        assert os.getpid() in tcptable.listening_pids(port)
        client = socket.create_connection(("127.0.0.1", port))
        accepted, _ = server.accept()
        try:
            me = (os.getpid(), os.path.basename(sys.executable))
            assert _wait_for(lambda: me in tcptable.foreign_clients(port, set()))
            assert tcptable.foreign_clients(port, {os.getpid()}) == []
            # A port with no client at all. Not port + 1: Windows hands ephemeral ports
            # out in sequence, so the client's own end may well be port + 1, and the
            # server side of this very connection is then a row whose remote port it is.
            with socket.socket() as idle:
                idle.bind(("127.0.0.1", 0))
                idle.listen()
                assert tcptable.foreign_clients(idle.getsockname()[1], set()) == []
        finally:
            client.close()
        assert _wait_for(lambda: tcptable.foreign_clients(port, set()) == [])
        accepted.close()


@pytestmark_tcp
def test_a_client_in_another_process_is_named():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        child = subprocess.Popen([sys.executable, "-c",
                                  "import socket,sys; s=socket.create_connection(('127.0.0.1', %d)); "
                                  "print('up', flush=True); sys.stdin.read()" % port],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            assert child.stdout.readline().strip() == "up"
            accepted, _ = server.accept()
            want = [(child.pid, os.path.basename(sys.executable))]
            assert _wait_for(lambda: tcptable.foreign_clients(port, {os.getpid()}) == want)
            assert tcptable.foreign_clients(port, {os.getpid(), child.pid}) == []
        finally:
            child.kill()
            child.wait()
        assert _wait_for(lambda: tcptable.foreign_clients(port, {os.getpid()}) == [])
        accepted.close()


def test_the_check_never_fails_off_windows(monkeypatch):
    monkeypatch.setattr(tcptable.sys, "platform", "linux")
    assert tcptable.foreign_clients(1, set()) == []
    assert tcptable.listening_pids(1) is None
