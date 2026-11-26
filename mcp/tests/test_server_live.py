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
                  "save", "undo", "run_command", "transfer_texture", "uv_sets", "command_help", "cut", "weld",
                  "optimize", "validate", "texel_density", "view"}


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
    assert parse([], env={"RIZOMUV_MCP_FOLLOW_VIEW": "0"}).follow_view is False
    assert parse([], env={"RIZOMUV_MCP_FOLLOW_VIEW": "1"}).follow_view is True
    assert parse(["--no-follow-view"], env={"RIZOMUV_MCP_FOLLOW_VIEW": "1"}).follow_view is False
    for bad in ({"RIZOMUV_MCP_PORT": "http"}, {"RIZOMUV_MCP_PORT": "70000"}, {"RIZOMUV_MCP_INSTANCE": "gui"},
                {"RIZOMUV_MCP_LAUNCH_TIMEOUT": "-1"}, {"RIZOMUV_MCP_FOLLOW_VIEW": "maybe"}):
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
                r = await call(c, "view", {"frame": "all"})
                assert r.is_error and "headless" in text(r)
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


@pytest.mark.live
def test_transfer_texture(snap_exe, isolated_dirs, example_mesh, tmp_path):
    """A texture of the Default set goes onto a repacked copy of it, and back; the refusals name the fix."""
    import struct, zlib
    source = tmp_path / "grid.png"
    rows = b"".join(b"\0" + bytes(c for x in range(256) for c in (x, y, (x ^ y) & 255)) for y in range(256))
    chunk = lambda kind, body: (struct.pack(">I", len(body)) + kind + body
                                + struct.pack(">I", zlib.crc32(kind + body)))
    source.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 256, 256, 8, 2, 0, 0, 0))
                       + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
    out = tmp_path / "on_b.png"

    async def go():
        async with Client(build_server(Config(instance="headless", exe=str(snap_exe))), mode="legacy") as c:
            data(await call(c, "load", {"path": str(example_mesh)}))
            r = await call(c, "transfer_texture", {"dest_uv_set": "B", "output_file": str(out)})
            assert r.is_error and "Its UV sets: Default" in text(r)
            data(await call(c, "uv_sets", {"action": "duplicate", "name": "B"}))
            r = await call(c, "transfer_texture", {"dest_uv_set": "B", "output_file": str(out)})
            assert r.is_error and "no texture assigned" in text(r)
            r = await call(c, "transfer_texture", {"dest_uv_set": "Default", "output_file": str(out),
                                                   "source_file": str(source)})
            assert r.is_error and "both 'Default'" in text(r)
            data(await call(c, "uv_sets", {"action": "set_current", "name": "B"}))
            data(await call(c, "pack", {"padding_px": 8, "measure": False}))
            data(await call(c, "uv_sets", {"action": "set_current", "name": "Default"}))
            res = data(await call(c, "transfer_texture", {"dest_uv_set": "B", "output_file": str(out),
                                                          "source_file": str(source), "size_px": 128}))
            assert (res["source_uv_set"], res["width"], res["height"]) == ("Default", 128, 128)
            assert out.is_file() and 0 < res["coverage"] <= 1
            r = await call(c, "transfer_texture", {"dest_uv_set": "B", "output_file": str(out),
                                                   "source_file": str(source)})
            assert r.is_error and "overwrite=true" in text(r)
            back = data(await call(c, "transfer_texture", {"source_uv_set": "B", "dest_uv_set": "Default",
                                                           "source_file": str(out),
                                                           "output_file": str(tmp_path / "back.png")}))
            assert back["dest_uv_set"] == "Default" and (tmp_path / "back.png").is_file()
    anyio.run(go)

@pytest.mark.live
def test_uv_sets(snap_exe, isolated_dirs, example_mesh):
    async def go():
        async with Client(build_server(Config(instance="headless", exe=str(snap_exe))), mode="legacy") as c:
            data(await call(c, "load", {"path": str(example_mesh)}))
            state = data(await call(c, "uv_sets"))
            assert state["current"] == "Default" and [s["name"] for s in state["uv_sets"]] == ["Default"]
            state = data(await call(c, "uv_sets", {"action": "duplicate", "name": "B"}))
            assert [s["name"] for s in state["uv_sets"]] == ["Default", "B"] and state["current"] == "Default"
            r = await call(c, "uv_sets", {"action": "duplicate", "name": "B"})
            assert r.is_error and "exists already" in text(r)
            state = data(await call(c, "uv_sets", {"action": "rename", "source": "B", "name": "Lightmap"}))
            assert [s["name"] for s in state["uv_sets"]] == ["Default", "Lightmap"]
            state = data(await call(c, "uv_sets", {"action": "set_current", "name": "Lightmap"}))
            assert state["current"] == "Lightmap"
            r = await call(c, "uv_sets", {"action": "delete", "name": "Lightmap"})
            assert r.is_error and "set_current" in text(r)
            data(await call(c, "uv_sets", {"action": "create", "name": "Fresh"}))
            data(await call(c, "uv_sets", {"action": "set_current", "name": "Default"}))
            state = data(await call(c, "uv_sets", {"action": "delete", "name": "Lightmap"}))
            assert [s["name"] for s in state["uv_sets"]] == ["Default", "Fresh"]
            r = await call(c, "uv_sets", {"action": "set_current", "name": "Nope"})
            assert r.is_error and "UV sets: Default, Fresh" in text(r)
    anyio.run(go)

CUBE_OBJ = """v 0 0 0
v 1 0 0
v 1 1 0
v 0 1 0
v 0 0 1
v 1 0 1
v 1 1 1
v 0 1 1
f 1 4 3 2
f 5 6 7 8
f 1 2 6 5
f 2 3 7 6
f 3 4 8 7
f 4 1 5 8
"""


@pytest.mark.live
def test_cut_and_weld_by_polygon_and_side(snap_exe, isolated_dirs, tmp_path):
    cube = tmp_path / "cube.obj"
    cube.write_text(CUBE_OBJ)
    face0 = [[0, 0], [0, 1], [0, 2], [0, 3]]

    async def go():
        async with Client(build_server(Config(instance="headless", exe=str(snap_exe))), mode="legacy") as c:
            data(await call(c, "load", {"path": str(cube)}))
            cut = data(await call(c, "cut", {"edges": face0}))
            assert cut["islands_after"] == cut["islands_before"] + 1, cut
            weld = data(await call(c, "weld", {"edges": face0}))
            assert weld["islands_after"] == cut["islands_before"], weld
            data(await call(c, "undo"))
            assert data(await call(c, "session_info"))  # still connected
            r = await call(c, "cut", {"edges": [[99, 0]]})
            assert r.is_error and "no polygon 99" in text(r)
            r = await call(c, "cut", {"edges": [[0, 7]]})
            assert r.is_error and "no side 7" in text(r)
    anyio.run(go)

@pytest.mark.live
def test_validate_texel_density_and_optimize(snap_exe, isolated_dirs, example_mesh):
    async def go():
        async with Client(build_server(Config(instance="headless", exe=str(snap_exe))), mode="legacy") as c:
            data(await call(c, "load", {"path": str(example_mesh)}))
            data(await call(c, "unfold"))
            v = data(await call(c, "validate", {"raster": False}))
            assert v["verdict"] in ("Valid", "ValidWithWarnings", "NotValid")
            assert {"OverlappingIslands", "FlippedFaces", "TexelDensity"} <= set(v["checks"])
            assert v["stats"]["Islands"] > 0 and v["checks"]["OverlappingIslands"]["state"] == "Stale"

            td = data(await call(c, "texel_density", {"action": "set", "value": 0.5, "unit": "tx/cm"}))
            assert td["set"]["scale_factor"] > 0
            assert abs(td["median"] - 0.5) < 0.005, td
            assert td["islands_off_median"]["count"] == 0, td
            in_mm = data(await call(c, "texel_density", {"unit": "tx/mm"}))
            assert abs(in_mm["median"] - 0.05) < 0.0005, in_mm

            data(await call(c, "pack", {"keep_scale": True, "measure": False}))
            after = data(await call(c, "texel_density"))
            assert abs(after["median"] - 0.5) < 0.005, after

            o = data(await call(c, "optimize", {"extra_params": {"Iterations": 2}}))
            assert o["params"]["Iterations"] == 2

            r = await call(c, "texel_density", {"action": "set"})
            assert r.is_error and "needs value" in text(r)
            r = await call(c, "cut", {"edges": [[0, 0, 0]]})
            assert r.is_error
    anyio.run(go)


def test_command_help_needs_no_rizomuv(isolated_dirs):
    async def go():
        async with Client(build_server(Config(instance="headless")), mode="legacy") as c:
            listing = data(await c.call_tool("command_help", {}))
            names = {i["name"] for i in listing["commands"]}
            assert {"Cut", "Select", "Uvset", "TextureTransfer"} <= names and "Quit" not in names
            cut = next(i for i in listing["commands"] if i["name"] == "Cut")
            assert cut["artist_instance"] is True and cut["summary"]
            doc = data(await c.call_tool("command_help", {"name": "Cut"}))
            assert "PARAMETER" in doc["doc"] and doc["run_command"]["artist_instance"] is True
            tt = data(await c.call_tool("command_help", {"name": "TextureTransfer"}))
            assert tt["run_command"] == {"artist_instance": False, "headless": True, "category": "save_export",
                                         "note": tt["run_command"]["note"]} and "transfer_texture" in tt["run_command"]["note"]
            r = await c.call_tool("command_help", {"name": "cut"})
            assert r.is_error and "Did you mean 'Cut'" in text(r)
            guide = data(await c.call_tool("command_help", {"name": "guide:packing"}))
            assert "map_resolution dominates" in guide["text"]
            info = data(await c.call_tool("session_info", {}))
            assert info["connected"] is False
    anyio.run(go)

def test_auto_seams_is_offered_only_with_a_graph(isolated_dirs, tmp_path):
    async def go():
        async with Client(build_server(Config(instance="headless")), mode="legacy") as c:
            assert "auto_seams" not in {t.name for t in (await c.list_tools()).tools}
        async with Client(build_server(Config(instance="headless", graph=str(tmp_path / "g.json"))),
                          mode="legacy") as c:
            assert "auto_seams" in {t.name for t in (await c.list_tools()).tools}
    anyio.run(go)


@pytest.mark.live
def test_auto_seams_runs_the_graph_it_is_set_up_with(snap_exe, isolated_dirs, example_mesh, tmp_path):
    """The headless instance starts with the node graph off (a fresh user dir): the server turns it
    on and restarts it, then runs the graph file of its configuration."""
    graph = tmp_path / "planar.rzgraph.json"
    graph.write_text(json.dumps({
        "Version": 1,
        "Nodes": {"classify": {"Type": "ClassifyPlanar", "Params": {"AngleTolerance": 5.0}},
                  "project": {"Type": "Project", "Params": {"Algorithm": "Planar"}}},
        "Links": [["classify.Mesh", "project.Mesh"], ["classify.Planar", "project.Regions"]],
        "Output": "project.Mesh"}), encoding="utf-8")

    async def go():
        async with Client(build_server(Config(instance="headless", exe=str(snap_exe), graph=str(graph))),
                          mode="legacy") as c:
            r = await call(c, "auto_seams", {})
            assert r.is_error and "load first" in text(r)
            data(await call(c, "load", {"path": str(example_mesh)}))
            res = data(await call(c, "auto_seams", {}))
            assert res["graph"] == graph.name and res["nodes_run"] == 2
            assert res["measure"]["islands"] == res["scene"]["islands"] > 0
            graph.write_text("not json", encoding="utf-8")
            r = await call(c, "auto_seams", {})
            assert r.is_error and "is not JSON" in text(r)
    anyio.run(go)



def _quit(port):
    async def go():
        from rizomuv_mcp import session
        worker = await session.spawn_worker()
        try:
            r = await worker.request("connect", {"port": port}, timeout=120)
            assert r["ok"], r
            r = await worker.request("quit_instance", timeout=30)
            assert r["ok"], r
        finally:
            worker.kill()
            await worker.wait_closed(5)
    anyio.run(go)


def _grid_png(path):
    import struct, zlib
    rows = b"".join(b"\0" + bytes(c for x in range(256) for c in (x, y, (x ^ y) & 255)) for y in range(256))
    chunk = lambda kind, body: (struct.pack(">I", len(body)) + kind + body
                                + struct.pack(">I", zlib.crc32(kind + body)))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 256, 256, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
    return path


@pytest.mark.live
def test_the_viewports_follow_the_tools_and_the_view_tool_moves_them(snap_exe, isolated_dirs, example_mesh, tmp_path):
    """On a RizomUV on screen (not headless), the tools frame the viewports on their result, and the
    view tool frames, zooms, pans and turns them; with follow_view off the tools leave them alone."""
    inst = launch.launch_headless(snap_exe, headless=False)
    try:
        inst.wait_ready(180)

        async def go():
            async with Client(build_server(Config(port=inst.port))) as c:
                loaded = data(await call(c, "load", {"path": str(example_mesh)}))
                assert "UV" in loaded.get("view_framed", []), loaded
                assert "UV" in data(await call(c, "unfold")).get("view_framed", [])
                assert "UV" in data(await call(c, "pack", {"measure": False})).get("view_framed", [])

                read = data(await call(c, "view"))
                assert set(read) == {"3D", "UV"} and read["UV"]["Distance"] > 0

                uv = data(await call(c, "view", {"tile": [1, 0], "animate_ms": 0}))["UV"]
                assert uv["Target"][:2] == pytest.approx([1.5, 0.5]) and uv["Distance"] >= 1.1

                zoomed = data(await call(c, "view", {"view": "UV", "zoom": 2, "animate_ms": 0}))["UV"]
                assert zoomed["Distance"] == pytest.approx(uv["Distance"] / 2, rel=1e-6)
                panned = data(await call(c, "view", {"view": "UV", "pan": {"right": 0.5}, "animate_ms": 0}))["UV"]
                assert panned["Target"][0] - zoomed["Target"][0] == pytest.approx(0.5 * zoomed["Distance"], rel=1e-3)

                isl = data(await call(c, "view", {"islands": [0, 1, 2], "animate_ms": 0}))["UV"]
                assert 0.0 <= isl["Target"][0] <= 1.0 and 0.0 <= isl["Target"][1] <= 1.0
                assert isl["Distance"] < uv["Distance"]

                both = data(await call(c, "view", {"frame": "all", "animate_ms": 0}))
                assert set(both) == {"3D", "UV"}
                front = data(await call(c, "view", {"standard_view": "front", "animate_ms": 0}))["3D"]
                turned = data(await call(c, "view", {"orbit": {"horizontal": 30}, "animate_ms": 0}))["3D"]
                assert turned["Orientation"]["Horizontal"] != pytest.approx(front["Orientation"]["Horizontal"])
                assert set(data(await call(c, "view", {"frame": "all"}))) == {"3D", "UV"}   # animated

                for args, words in (({"view": "3D", "tile": [0, 0]}, "UV view"),
                                    ({"view": "UV", "orbit": {"horizontal": 10}}, "3D view"),
                                    ({"frame": "all", "tile": [0, 0]}, "give one of them"),
                                    ({"islands": [10 ** 7]}, "None of these ids")):
                    r = await call(c, "view", args)
                    assert r.is_error and words in text(r), text(r)

                # transfer_texture show=true: the result is the texture of the destination set, made
                # current, and the views show it; the source set keeps the image it was read from
                source = _grid_png(tmp_path / "grid.png")
                data(await call(c, "uv_sets", {"action": "duplicate", "name": "B"}))
                data(await call(c, "uv_sets", {"action": "set_current", "name": "B"}))
                data(await call(c, "pack", {"padding_px": 8, "measure": False}))
                data(await call(c, "uv_sets", {"action": "set_current", "name": "Default"}))
                result = tmp_path / "on_b.png"
                shown = data(await call(c, "transfer_texture", {"dest_uv_set": "B", "output_file": str(result),
                                                                "source_file": str(source), "show": True}))
                assert shown["show"]["current_uv_set"] == "B" and result.is_file()
                get = lambda path: call(c, "run_command", {"command": "Get", "params": path})
                assert data(await get("Lib.CurrentUVSetName"))["result"] == "B"
                assert data(await get("Lib.Mesh.RootGroup.Properties.Texture"))["result"] == result.as_posix()
                assert data(await get("Vars.Viewport.Texture"))["result"] == "User"
                assert data(await get("Vars.Viewport.ViewportUV.Textured"))["result"] is True
                data(await call(c, "uv_sets", {"action": "set_current", "name": "Default"}))
                assert data(await get("Lib.Mesh.RootGroup.Properties.Texture"))["result"] == source.as_posix()

            async with Client(build_server(Config(port=inst.port, follow_view=False))) as c:
                assert "view_framed" not in data(await call(c, "unfold"))

        anyio.run(go)
        _quit(inst.port)
        assert inst.proc.wait(30) == 0
    finally:
        inst.terminate()


def _big_squares_obj(path):
    """Four separate 6 x 6 squares, their UVs their XY: much larger than the tile, one centred on it,
    the three others centred outside it -- the layout an unfold at 3D scale leaves."""
    lines, n = [], 0
    for cx, cy in ((0.5, 0.5), (-4.0, 0.5), (5.0, 0.5), (0.5, -4.0)):
        corners = [(cx - 3, cy - 3), (cx + 3, cy - 3), (cx + 3, cy + 3), (cx - 3, cy + 3)]
        for x, y in corners:
            lines += ["v %g %g 0" % (x, y), "vt %g %g" % (x, y)]
        lines.append("f " + " ".join("%d/%d" % (n + i, n + i) for i in range(1, 5)))
        n += 4
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.mark.live
def test_pack_brings_in_the_islands_a_large_one_leaves_no_room_for(snap_exe, isolated_dirs, tmp_path):
    """As the Pack button with nothing selected: the islands outside the tile are filed into it first
    (IslandGroups), then packed, even when the island already in the tile is larger than it -- the case
    a Pack with AuxGroup and no distribution got wrong. distribute=false leaves them outside, as the
    button does with 'Distribute in tiles' off."""
    mesh = _big_squares_obj(tmp_path / "squares.obj")

    async def go():
        async with Client(build_server(Config(instance="headless", exe=str(snap_exe))), mode="legacy") as c:
            data(await call(c, "load", {"path": str(mesh)}))
            alone = data(await call(c, "pack", {"distribute": False}))
            assert alone["distributed"] is False and alone["undo_steps"] == 1
            assert alone["measure"]["outside_tile"]["islands"] == 3 and "AuxGroup" not in alone["params"]
            data(await call(c, "undo"))
            packed = data(await call(c, "pack", {}))
            assert packed["distributed"] is True and packed["undo_steps"] == 2
            assert packed["measure"]["outside_tile"]["islands"] == 0
            assert packed["measure"]["overlap"]["island_pairs"] == 0
    anyio.run(go)


def _square_grid_obj(path, n=6):
    """n x n separate unit squares, their UVs their XY, 0.5 apart: far outside the 0-1 tile."""
    lines, k = [], 0
    for i in range(n):
        for j in range(n):
            cx, cy = i * 1.5, j * 1.5
            for x, y in ((cx, cy), (cx + 1, cy), (cx + 1, cy + 1), (cx, cy + 1)):
                lines += ["v %g %g 0" % (x, y), "vt %g %g" % (x, y)]
            lines.append("f " + " ".join("%d/%d" % (k + q, k + q) for q in range(1, 5)))
            k += 4
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.mark.live
def test_pack_onto_a_two_tile_grid(snap_exe, isolated_dirs, tmp_path):
    """columns/rows set the tile grid first (one more undo step), the distribution spreads the islands
    over both tiles, and the measure judges them against the grid, not the 0-1 tile. (Four squares are
    not enough: the Pack puts them all in the first tile, which they fill.)"""
    mesh = _square_grid_obj(tmp_path / "grid.obj")

    async def go():
        async with Client(build_server(Config(instance="headless", exe=str(snap_exe))), mode="legacy") as c:
            data(await call(c, "load", {"path": str(mesh)}))
            packed = data(await call(c, "pack", {"columns": 2, "rows": 1}))
            assert packed["tile_grid"]["columns"] == 2 and packed["tile_grid"]["rows"] == 1
            assert packed["tile_grid"]["changed"] is True and packed["undo_steps"] == 3
            m = packed["measure"]
            assert m["tile_grid"] == [2, 1]
            assert m["outside_tile"]["islands"] == 0          # inside [0, 2] x [0, 1]
            assert m["across_tiles"]["islands"] == 0
            assert m["overlap"]["island_pairs"] == 0
            per_tile = {tuple(t["tile"]): t["coverage"] for t in m["coverage_per_tile"]}
            assert set(per_tile) == {(0, 0), (1, 0)} and all(cv > 0.3 for cv in per_tile.values())
            # the grid stays: a second pack without columns/rows keeps it and does not count its step
            again = data(await call(c, "pack", {}))
            assert again["tile_grid"] == {"columns": 2, "rows": 1, "changed": False}
            assert again["undo_steps"] == 2 and again["measure"]["outside_tile"]["islands"] == 0
            # measure on its own reads the grid from the scene
            assert data(await call(c, "measure"))["tile_grid"] == [2, 1]
    anyio.run(go)


def test_measure_judges_islands_against_the_tile_grid():
    """Pure metrics: a square in tile 1002 is outside the 0-1 tile, inside a 2x1 grid; one over the
    border between the two tiles is reported across_tiles."""
    import numpy as np
    from rizomuv_mcp import metrics

    def snap(squares):
        uv, ids = [], []
        for u0, v0, w in squares:
            k = len(uv)
            uv += [(u0, v0), (u0 + w, v0), (u0 + w, v0 + w), (u0, v0 + w)]
            ids += [k, k + 1, k + 2, k + 3]
        n = len(squares)
        return metrics.UVSnapshot(np.full(n, 4, np.int64), np.asarray(ids, np.int64),
                                  np.asarray(uv, np.float64), np.arange(n, dtype=np.int64))

    s = snap([(0.1, 0.1, 0.5), (1.2, 0.2, 0.5), (0.8, 0.5, 0.4)])
    one = metrics.measure(s, 256)
    assert one["tile_grid"] == [1, 1] and one["outside_tile"]["islands"] == 2
    assert "coverage_per_tile" not in one
    two = metrics.measure(s, 256, tiles=(2, 1))
    assert two["outside_tile"]["islands"] == 0
    assert two["across_tiles"] == {"islands": 1, "example_ids": [2]}
    cov = {tuple(t["tile"]): t["coverage"] for t in two["coverage_per_tile"]}
    assert abs(cov[(1, 0)] - (0.25 + 0.2 * 0.4)) < 0.02
    assert abs(two["coverage"] - (cov[(0, 0)] + cov[(1, 0)]) / 2) < 1e-3
