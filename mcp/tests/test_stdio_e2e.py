"""The real server process over stdio, started exactly as the launcher starts it:

    <app python.exe> -I -S -X utf8 <RizomUVLink>/mcp/boot.py

and driven by the SDK's own stdio client in the handshake era (what Claude Desktop and
Cursor speak). The protocol must survive a real headless RizomUV writing its log, and no
RizomUV may outlive the session.
"""
import os
import sys
import time
from pathlib import Path

import anyio
import pytest
from mcp import Client, StdioServerParameters, stdio_client

from rizomuv_mcp import discovery

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="runs the app's embedded python.exe")

BOOT = Path(__file__).resolve().parents[1] / "boot.py"
EXPECTED_TOOLS = {"session_info", "connect", "load", "unfold", "pack", "measure", "diagnose", "render_layout",
                  "save", "undo", "run_command"}


@pytest.fixture
def server_params(snap_exe, vendor_dir, isolated_dirs):
    python = snap_exe.parent / "python.exe"
    if not python.is_file():
        pytest.skip("no python.exe next to %s" % snap_exe)
    env = {k: v for k, v in os.environ.items() if not k.startswith("RIZOMUV_")}
    env.update({"RIZOMUV_EXE": str(snap_exe), "RIZOMUV_MCP_INSTANCE": "headless",
                "RIZOMUV_MCP_STATE_DIR": str(isolated_dirs.state),
                "RIZOMUV_INSTANCES_DIR": str(isolated_dirs.instances)})
    return StdioServerParameters(command=str(python), args=["-I", "-S", "-X", "utf8", str(BOOT)], env=env)


def test_initialize_time(server_params, tmp_path):
    """Spawn to initialize answered, twice (the second run has warm caches); nothing starts RizomUV."""
    times = []

    async def once():
        with open(tmp_path / "stderr-init.txt", "a", encoding="utf-8") as errlog:
            t0 = time.perf_counter()
            async with Client(stdio_client(server_params, errlog=errlog), mode="legacy") as c:
                times.append(round(time.perf_counter() - t0, 2))
                assert EXPECTED_TOOLS == {t.name for t in (await c.list_tools()).tools}
    anyio.run(once)
    anyio.run(once)
    print("spawn -> initialize answered (s): first %s, second %s" % tuple(times))
    assert times[1] < 30


@pytest.mark.live
def test_stdio_load_pack_measure(server_params, example_mesh, tmp_path):
    state = {}
    errpath = tmp_path / "server-stderr.txt"

    async def go():
        with open(errpath, "w", encoding="utf-8") as errlog:
            async with Client(stdio_client(server_params, errlog=errlog), mode="legacy") as c:
                assert EXPECTED_TOOLS == {t.name for t in (await c.list_tools()).tools}
                for name, args in (("load", {"path": str(example_mesh)}), ("pack", {}), ("measure", {}),
                                   ("session_info", {})):
                    t0 = time.perf_counter()
                    r = await c.call_tool(name, args)
                    state[name] = round(time.perf_counter() - t0, 2)
                    assert not r.is_error, r.content[0].text
                    state[name + "_result"] = r.structured_content
    anyio.run(go)
    print("stdio timings (s):", {k: v for k, v in state.items() if not k.endswith("_result")})
    assert state["pack_result"]["measure"]["islands"] == 750
    assert 0.6 < state["measure_result"]["coverage"] < 0.8
    info = state["session_info_result"]
    assert info["mode"] == "headless" and info["owned"] is True
    pid = info["pid"]
    end = time.monotonic() + 15
    while discovery.pid_alive(pid) and time.monotonic() < end:
        time.sleep(0.1)
    assert not discovery.pid_alive(pid), "RizomUV pid %d outlived the MCP session" % pid
    # the instance's own log went to its file, never onto the wire (the session would have broken)
    logs = list((tmp_path / "state" / "logs").glob("rizomuv-*.log"))
    assert logs and "is ready" in logs[0].read_text(encoding="utf-8", errors="replace")
