"""The MCP server in memory (SDK Client, both protocol eras) against real RizomUV instances.

Headless runs launch from the snapshot executable into isolated state/instances dirs; the
attach run drives an instance this test started with -hl -id, and checks that the server
leaves it running -- it never quits what it did not launch.
"""
import json
import time

import anyio
import pytest
from mcp import Client

from rizomuv_mcp import discovery, launch
from rizomuv_mcp.config import Config
from rizomuv_mcp.server import build_server

EXPECTED_TOOLS = {"session_info", "connect", "load", "unfold", "pack", "measure", "diagnose", "render_layout",
                  "save", "undo", "run_command"}


def text(result):
    return result.content[0].text


def data(result):
    assert not result.is_error, text(result)
    return result.structured_content


async def call(client, name, args=None, **kw):
    t0 = time.perf_counter()
    result = await client.call_tool(name, args or {}, **kw)
    print("%-14s %6.2f s%s" % (name, time.perf_counter() - t0, "  ERROR " + text(result)[:120] if result.is_error else ""))
    return result


def wait_dead(pid, timeout=15.0):
    end = time.monotonic() + timeout
    while discovery.pid_alive(pid) and time.monotonic() < end:
        time.sleep(0.1)
    return not discovery.pid_alive(pid)


def test_config_command_line_wins_over_the_environment():
    from rizomuv_mcp.config import ConfigError, parse
    assert parse([], env={}) == Config()
    env = {"RIZOMUV_MCP_INSTANCE": "HEADLESS", "RIZOMUV_MCP_PORT": "50111", "RIZOMUV_MCP_LAUNCH_TIMEOUT": "30",
           "RIZOMUV_MCP_LOG_LEVEL": "debug", "RIZOMUV_EXE": r"C:\elsewhere\rizomuv.exe"}
    cfg = parse([], env=env)
    assert (cfg.instance, cfg.port, cfg.launch_timeout, cfg.log_level) == ("headless", 50111, 30.0, "DEBUG")
    assert cfg.exe is None, "RIZOMUV_EXE is resolved by launch.find_rizomuv_exe, which names its source"
    cfg = parse(["--instance", "attach", "--port", "50200", "--exe", r"C:\x\rizomuv.exe", "--link-worker"], env=env)
    assert (cfg.instance, cfg.port, cfg.exe, cfg.link_worker) == ("attach", 50200, r"C:\x\rizomuv.exe", True)
    for bad in ({"RIZOMUV_MCP_PORT": "http"}, {"RIZOMUV_MCP_PORT": "70000"}, {"RIZOMUV_MCP_INSTANCE": "gui"},
                {"RIZOMUV_MCP_LAUNCH_TIMEOUT": "-1"}):
        with pytest.raises(ConfigError, match=next(iter(bad))):
            parse([], env=bad)


def test_tools_and_session_info_never_launch(snap_exe, isolated_dirs):
    async def go():
        async with Client(build_server(Config(instance="headless", exe=str(snap_exe))), mode="legacy") as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
            assert EXPECTED_TOOLS == set(tools)
            assert "0.62 → 0.75 → 0.81" in tools["pack"].description
            assert tools["session_info"].annotations.read_only_hint is True
            assert all(t.annotations.open_world_hint is False for t in tools.values())
            for t in tools.values():
                assert t.description and not t.description.startswith((" ", "\n")), t.name
            info = data(await c.call_tool("session_info", {}))
            assert info["connected"] is False and info["candidates"] == []
            assert info["headless_launch_exe"] == {"path": str(snap_exe), "source": "--exe"}
            # resources without RizomUV: the docs shipped with the binding
            index = json.loads((await c.read_resource("rizomuv://commands")).contents[0].text)
            assert len(index) >= 60 and {"Pack", "Quit", "Get"} <= {i["name"] for i in index}
            quit_row = next(i for i in index if i["name"] == "Quit")
            assert quit_row["allowed_attached"] is False and quit_row["allowed_headless"] is False
            guide = (await c.read_resource("rizomuv://guide/packing")).contents[0].text
            assert "0.81 | 0.71 | 0.62 | 0.47 | 0.26" in guide
            with pytest.raises(Exception, match="Unknown RizomUV command"):
                await c.read_resource("rizomuv://command/NoSuchCommand")
            # a denied command is refused before anything is started
            r = await c.call_tool("run_command", {"command": "Quit"})
            assert r.is_error and "owns the RizomUV lifecycle" in text(r)
            info = data(await c.call_tool("session_info", {}))
            assert info["connected"] is False
    anyio.run(go)


def test_attach_mode_with_nothing_open_says_what_to_do(isolated_dirs):
    async def go():
        async with Client(build_server(Config(instance="attach"))) as c:
            r = await c.call_tool("unfold", {})
            assert r.is_error and "No open RizomUV to attach to" in text(r)
            assert "connect(target='headless')" in text(r)
    anyio.run(go)


@pytest.mark.live
@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_headless_flow(mode, snap_exe, isolated_dirs, example_mesh, tmp_path):
    state = {}

    async def go():
        async with Client(build_server(Config(instance="headless", exe=str(snap_exe))), mode=mode) as c:
            info = data(await call(c, "session_info"))
            assert info["connected"] is False

            r = await call(c, "load", {"path": "relative/ExampleMesh.obj"})
            assert r.is_error and "must be absolute" in text(r)
            info = data(await call(c, "session_info"))
            assert info["connected"] is False, "a refused call must not start RizomUV"

            loaded = data(await call(c, "load", {"path": str(example_mesh)}))
            assert loaded["scene"]["islands"] == 750 and loaded["warning"]["code"] == 6
            info = data(await call(c, "session_info"))
            assert info["mode"] == "headless" and info["owned"] is True
            state["pid"] = info["pid"]

            unfolded = data(await call(c, "unfold"))
            assert unfolded["bijection_failed_islands"]["count"] >= 0

            r = await call(c, "pack", {"extra_params": {"MaxMutations": 50}})
            assert r.is_error and "MaxMutations" in text(r)

            progress = []

            async def on_progress(value, total, message):
                progress.append((value, message))
            packed = data(await call(c, "pack", {"padding_px": 2, "map_resolution": 1024},
                                     progress_callback=on_progress))
            assert packed["params"]["MapResolution"] == 1024 and packed["params"]["Rotate"]["Step"] == 90.0
            m = packed["measure"]
            assert m["islands"] == 750 and 0.6 < m["coverage"] < 0.8 and m["overlap"]["pixels"] == 0
            assert progress and all(msg.startswith("Pack: ") for _, msg in progress), progress
            state["pack_progress"] = progress

            again = data(await call(c, "measure"))
            assert again["parameters_from"] == "last pack" and again["coverage"] == m["coverage"]
            assert again["padding_share_estimate"] == m["padding_share_estimate"]

            d = data(await call(c, "diagnose"))
            codes = [f["code"] for f in d["findings"]]
            assert "degenerate_islands" in codes and d["degenerate_islands"]["ids"] == [4, 6]
            severities = [f["severity"] for f in d["findings"]]
            assert severities == sorted(severities, key={"high": 0, "medium": 1, "info": 2}.get)

            r = await call(c, "render_layout", {"size_px": 512})
            assert not r.is_error, text(r)
            assert [b.type for b in r.content] == ["text", "image"]
            assert r.content[1].mime_type == "image/png"
            png = r.structured_content["path"]
            assert open(png, "rb").read(8) == b"\x89PNG\r\n\x1a\n"
            r = await call(c, "render_layout", {"mode": "stretch", "crop": [0.5, 0.5, 0.25, 1.0]})
            assert r.is_error and "umin < umax" in text(r)

            out = tmp_path / "packed.obj"
            saved = data(await call(c, "save", {"path": str(out)}))
            assert out.is_file() and saved["bytes"] == out.stat().st_size > 0
            r = await call(c, "save", {"path": str(out)})
            assert r.is_error and "already exists" in text(r)
            data(await call(c, "save", {"path": str(out), "overwrite": True}))

            undone = data(await call(c, "undo"))
            assert undone["undone_steps"] == 1

            got = data(await call(c, "run_command", {"command": "Get", "params": "Vars.Infos.Version.Full"}))
            assert got["result"] == info["version"]
            for command, params, reason in (("Quit", None, "owns the RizomUV lifecycle"),
                                            ("Get", "Lib", "crashes RizomUV"),
                                            ("Set", {"Path": "Prefs.Foo", "Value": 1}, "preferences")):
                r = await call(c, "run_command", {"command": command, "params": params})
                assert r.is_error and reason in text(r), text(r)
            big = data(await call(c, "run_command", {"command": "Save", "params": {"Data": True}}))
            assert "result_truncated" in big and len(big["result_truncated"]) == 20000

            doc = (await c.read_resource("rizomuv://command/Pack")).contents[0]
            assert doc.mime_type == "text/markdown" and doc.text.startswith("# Pack")
            assert "Source: RizomUV %s" % info["version"] in doc.text, "live docs expected while connected"

    anyio.run(go)
    assert wait_dead(state["pid"]), "the owned instance must be gone once the client disconnects"


@pytest.mark.live
def test_attached_instance_is_refused_a_load_and_never_quit(snap_exe, isolated_dirs, example_mesh, tmp_path):
    inst = launch.launch_headless(snap_exe)
    try:
        inst.wait_ready(180)
        lock = discovery.InstanceLock(discovery.port_lock_path(inst.port))

        async def go():
            async with Client(build_server(Config(port=inst.port))) as c:
                info = data(await call(c, "session_info"))
                assert info["connected"] is False
                first = data(await call(c, "load", {"path": str(example_mesh)}))   # empty scene: allowed
                assert first["scene"]["islands"] == 750 and "replaced" in first["note"]
                info = data(await call(c, "session_info"))
                assert info["mode"] == "attached" and info["owned"] is False and info["pid"] == inst.pid
                assert not lock.acquire(), "the session holds the client lock of the port"

                r = await call(c, "load", {"path": str(example_mesh)})
                assert r.is_error and "replace_scene=true" in text(r) and "ExampleMesh.obj" in text(r)
                r = await call(c, "run_command", {"command": "Load", "params": {"File": {"Path": "x"}}})
                assert r.is_error and "Use the load tool" in text(r)
                r = await call(c, "run_command", {"command": "Set", "params": {"Path": "Vars.X", "Value": 1}})
                assert r.is_error
                assert data(await call(c, "unfold"))["params"]["WorkingSet"] == "Visible"
                again = data(await call(c, "load", {"path": str(example_mesh), "replace_scene": True}))
                assert again["scene"]["islands"] == 750

        anyio.run(go)
        assert inst.alive(), "the server must never quit an instance it did not launch"
        assert lock.acquire(), "the client lock must be released when the session ends"
        lock.release()

        async def quit_it():
            from rizomuv_mcp import session
            worker = await session.spawn_worker()
            try:
                r = await worker.request("connect", {"port": inst.port}, timeout=120)
                assert r["ok"], r
                r = await worker.request("quit_instance", timeout=30)
                assert r["ok"], r
            finally:
                worker.kill()
                await worker.wait_closed(5)
        anyio.run(quit_it)
        assert inst.proc.wait(30) == 0
    finally:
        inst.terminate()


@pytest.mark.live
@pytest.mark.slow
def test_mech8_pack_and_diagnose(snap_exe, isolated_dirs, mech8):
    """The numbers of report 3 through the whole server: 2,573 islands, coverage 0.62 at
    1024 / 2 px, the two degenerate islands, and the resolution finding."""
    timings = {}

    async def go():
        async with Client(build_server(Config(instance="headless", exe=str(snap_exe)))) as c:
            for name, args in (("load", {"path": str(mech8)}), ("unfold", {}),
                               ("pack", {"padding_px": 2, "map_resolution": 1024}), ("measure", {}),
                               ("diagnose", {}), ("render_layout", {})):
                t0 = time.perf_counter()
                r = await c.call_tool(name, args)
                timings[name] = round(time.perf_counter() - t0, 2)
                assert not r.is_error, text(r)
                if name == "load":
                    assert r.structured_content["scene"]["islands"] == 2573
                if name == "pack":
                    m = r.structured_content["measure"]
                    assert 0.60 <= m["coverage"] <= 0.64, m
                    assert m["islands"] == 2573 and m["overlap"]["pixels"] == 0
                if name == "diagnose":
                    d = r.structured_content
                    codes = {f["code"]: f for f in d["findings"]}
                    assert d["degenerate_islands"]["ids"] == [1183, 1184]
                    assert "resolution_too_low" in codes and codes["resolution_too_low"]["severity"] == "medium"
                    assert "Doubling map_resolution" in codes["resolution_too_low"]["message"]
                    assert "tiny_islands" in codes and "overlap" not in codes
                    assert 0.14 <= d["metrics"]["padding_share_estimate"] <= 0.16
    anyio.run(go)
    print("Mech8 timings (s):", timings)
