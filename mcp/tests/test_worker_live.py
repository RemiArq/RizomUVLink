"""The link worker against a real headless RizomUV: every op, the error kinds, and the
stale answer a client killed mid-command leaves for the next one.

The worker is spawned exactly as the server spawns it (session.spawn_worker), and talked
to over its pipes. Every instance is started here and stopped here.
"""
import asyncio
import re

import anyio
import pytest

from rizomuv_mcp import docs, launch, session

pytestmark = pytest.mark.live

VERSION = re.compile(r"^\d{4}\.\d+\.\d+")
CANONICAL_PACK = {"RootGroup": "RootGroup", "WorkingSet": "Visible", "Translate": True, "Scaling": {"Mode": 2},
                  "LayoutScalingMode": 2, "RecursionDepth": 1, "ProcessTileSelection": False,
                  "UsePixelUnit": True, "PaddingSizePx": 2, "MapResolution": 1024,
                  "Rotate": {"Initial": "Auto", "Min": 0.0, "Max": 360.0, "Step": 90.0}}


@pytest.fixture
def instance(snap_exe, isolated_dirs):
    inst = launch.launch_headless(snap_exe)
    try:
        inst.wait_ready(180)
        yield inst
    finally:
        inst.terminate()
    assert not inst.alive()


def load_params(mesh):
    return {"File": {"Path": mesh.as_posix(), "XYZUVW": True, "ImportGroups": True, "UVWProps": True},
            "__Focus": True}


def ok(response):
    assert response["ok"], response
    return response["result"]


def kind(response):
    assert not response["ok"], response
    return response["error"]["kind"]


def test_every_op_against_a_headless_instance(instance, example_mesh, tmp_path):
    async def go():
        worker = await session.spawn_worker()
        try:
            assert ok(await worker.request("ping", timeout=60)) == {"pong": True}
            probe = {"command": "Get", "params": "Vars.Infos.Version.Full"}
            assert kind(await worker.request("execute", probe, timeout=30)) == "not_connected"

            info = ok(await worker.request("connect", {"port": instance.port}, timeout=120))
            assert VERSION.match(info["version"])
            assert info["headless"] is True and info["startup_done"] is True
            assert info["token_ok"] is None                       # no token asked for
            assert info["identifier"] == str(instance.port)

            # errors: a command error leaves the socket usable; malformed requests are refused
            bad = await worker.request("execute", {"command": "NoSuchCommand", "params": {}}, timeout=30)
            assert kind(bad) == "rizomuv" and "NoSuchCommand" in bad["error"]["message"]
            assert kind(await worker.request("nope", timeout=30)) == "bad_request"
            assert kind(await worker.request("execute", {"cmd": "Get"}, timeout=30)) == "bad_request"
            assert VERSION.match(ok(await worker.request("execute", probe, timeout=30))["value"])

            empty = ok(await worker.request("scene", timeout=30))
            assert empty == {"has_mesh": False, "polygons": 0, "islands": 0, "file": None}
            nomesh = await worker.request("measure", {"res": 512}, timeout=30)
            assert kind(nomesh) == "rizomuv" and "no mesh" in nomesh["error"]["message"]

            loaded = ok(await worker.request("execute", {"command": "Load", "params": load_params(example_mesh)},
                                             timeout=120))["value"]
            assert "Error" not in loaded
            scene = ok(await worker.request("scene", timeout=30))
            assert scene["has_mesh"] and scene["polygons"] == 31516 and scene["islands"] == 750
            assert scene["file"].endswith("ExampleMesh.obj")

            ok(await worker.request("execute", {"command": "Pack", "params": CANONICAL_PACK}, timeout=180))
            m = ok(await worker.request("measure", {"res": 1024, "padding_px": 2}, timeout=60))
            assert m["islands"] == 750 and m["overlap"]["pixels"] == 0
            assert 0.6 < m["coverage"] < 0.8
            assert m["padding_share_estimate"] is not None

            d = ok(await worker.request("diagnose", {"res": 1024, "padding_px": 2, "tmp_dir": str(tmp_path)},
                                        timeout=120))
            assert d["findings"] and d["metrics"]["islands"] == 750
            # ExampleMesh carries two zero-area islands, which Pack leaves where they are
            assert d["degenerate_islands"] == {"count": 2, "ids": [4, 6]}
            assert d["texel_density"] is not None and d["stretch"] is not None
            assert not list(tmp_path.glob("diag-*.obj")), "the temporary OBJ must be deleted"

            png = tmp_path / "layout.png"
            r = ok(await worker.request("render", {"path": str(png), "size_px": 512, "mode": "islands"}, timeout=60))
            assert r["width"] == r["height"] == 512 and png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
            crop = tmp_path / "crop.png"
            r = ok(await worker.request("render", {"path": str(crop), "size_px": 512, "mode": "stretch",
                                                   "crop": [0.0, 0.0, 0.5, 0.25]}, timeout=60))
            assert (r["width"], r["height"]) == (512, 256) and r["bytes"] == crop.stat().st_size
            bad = await worker.request("render", {"path": str(crop), "crop": [0.5, 0, 0.5, 1]}, timeout=30)
            assert kind(bad) == "bad_request"

            module = tmp_path / "RizomUVLinkBase.py"
            r = ok(await worker.request("docs", {"path": str(module)}, timeout=60))
            assert r["version"] == info["version"]
            table = docs.parse_module_file(module)
            assert len(table) >= 60 and "Pack" in table and "MapResolution" in table["Pack"]

            assert ok(await worker.request("quit_instance", timeout=30))["quit"] is True
            assert await anyio.to_thread.run_sync(instance.proc.wait, 30) == 0
            assert ok(await worker.request("shutdown", timeout=30)) == {"bye": True}
            await worker.wait_closed(10)
            assert worker.proc.returncode == 0
        finally:
            worker.kill()
            await worker.wait_closed(5)

    anyio.run(go)


def test_link_failures_are_classified_and_survived(instance, example_mesh, capfd):
    async def go():
        first = await session.spawn_worker()
        second = None
        try:
            ok(await first.request("connect", {"port": instance.port}, timeout=120))
            ok(await first.request("execute", {"command": "Load", "params": load_params(example_mesh)}, timeout=120))

            # A client killed mid-Pack leaves the server in its heartbeat loop: the next request on
            # the port (from any client) used to receive the Pack's output. connect must see through it.
            pending = await first.submit("execute", {"command": "Pack", "params": CANONICAL_PACK})
            await anyio.sleep(1.5)
            assert not pending.done(), "Pack finished too fast to test anything"
            first.kill()
            await first.wait_closed(10)

            second = await session.spawn_worker()
            info = ok(await second.request("connect", {"port": instance.port}, timeout=120))
            assert VERSION.match(info["version"]), info
            got = ok(await second.request("execute", {"command": "Get", "params": "Vars.Infos.Headless"},
                                          timeout=30))["value"]
            assert got is True
            err = capfd.readouterr().err
            # an old build answers the stale output (discarded); a new one answers "busy" meanwhile
            assert "discarded an answer that is not the version" in err or "busy with another client" in err, err

            # The instance dies mid-command: silence, then link_lost; the socket is then unusable
            pending = await second.submit("execute", {"command": "Pack", "params": CANONICAL_PACK,
                                                      "timeout_ms": 3000})
            await anyio.sleep(1.0)
            assert not pending.done()
            await anyio.to_thread.run_sync(instance.terminate)
            lost = await asyncio.wait_for(asyncio.shield(pending), 60)
            assert kind(lost) == "link_lost" and "not responding" in lost["error"]["message"]
            again = await second.request("execute", {"command": "Get", "params": "Vars.Infos.Headless"}, timeout=30)
            assert kind(again) == "not_connected"
        finally:
            for w in (first, second):
                if w is not None:
                    w.kill()
                    await w.wait_closed(5)

    anyio.run(go)


def test_the_worker_starts_the_way_the_server_did(monkeypatch, tmp_path):
    """Through boot.py when the server came through it (same vendor on sys.path), as a
    module when installed with pip (no boot.py next to the package)."""
    import sys
    boot = tmp_path / "boot.py"
    boot.write_text("", encoding="utf-8")
    monkeypatch.setenv("RIZOMUV_MCP_BOOT_SCRIPT", str(boot))
    assert session.worker_command("DEBUG") == [sys.executable, "-I", "-S", "-X", "utf8", str(boot),
                                               "--link-worker", "--log-level", "DEBUG"]
    monkeypatch.setattr(session.paths, "boot_script", lambda: None)
    assert session.worker_command() == [sys.executable, "-m", "rizomuv_mcp", "--link-worker", "--log-level", "INFO"]


def test_a_dead_worker_fails_its_pending_requests(isolated_dirs):
    """No RizomUV needed: the reader resolves what was pending when the pipe closes."""
    async def go():
        worker = await session.spawn_worker()
        try:
            assert ok(await worker.request("ping", timeout=60)) == {"pong": True}
            # connect to a port nobody listens on: the worker sits in its sacrificial Get
            pending = await worker.submit("connect", {"port": 9, "startup_timeout": 30})
            await anyio.sleep(0.5)
            worker.kill()
            response = await asyncio.wait_for(asyncio.shield(pending), 30)
            assert kind(response) == "worker_dead"
            late = await worker.request("ping", timeout=10)
            assert kind(late) == "worker_dead"
        finally:
            worker.kill()
            await worker.wait_closed(5)

    anyio.run(go)
