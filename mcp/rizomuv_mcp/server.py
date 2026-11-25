"""The MCP server: tools, resources and the lifespan that owns the RizomUV session.

Two rules shape this module:
- stdout is the JSON-RPC wire. The SDK claims fd 1 inside run() and diverts it to stderr
  for everything else; before that nothing may print, and nothing native may be imported
  that could keep a stdout handle: the binding and numpy live in the link worker process.
  (No `sys.stdout = sys.stderr` either: the SDK would then serve the wire on stderr.)
- Every failure a model can act on is a ToolError with a sentence: the SDK shows the model
  nothing but "Error executing tool X" for any other exception.

No `from __future__ import annotations` here: pydantic reads the tool signatures.
"""
import base64
import functools
import difflib
import itertools
import json
import logging
import os
import re
import time
import types
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from mcp_types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from pydantic import Field

from . import __version__, docs, launch, paths, policy
from .session import Session

log = logging.getLogger(__name__)

RESULT_LIMIT = 20000
RENDERS_KEPT = 20

INSTRUCTIONS = """\
Drives RizomUV (UV unwrapping) through RizomUVLink. The first tool that needs RizomUV attaches to the \
RizomUV the artist has open -- their live scene -- or, if none is open, starts a private headless one \
(it holds a licence seat until the session ends). Typical flow: load -> unfold -> pack -> measure/diagnose -> \
render_layout -> save. Numbers rank layouts; images explain them -- the eye ranks layouts badly. Do not \
iterate on pack parameters to chase tenths of a percent: call diagnose and act on its findings. On the \
artist's instance: never load over their scene without asking, save to new files, and remember that \
undo reverts one tool step."""

PACK_DESCRIPTION = """\
Pack the UV islands into the 0–1 tile, then measure the result independently (coverage and overlap are \
recomputed from the UVs, not taken from RizomUV's own report, which exceeds 1 when islands stack).
Measured on a 2,573-island hard-surface scene — read before changing parameters:
• map_resolution dominates once islands are many: at 2 px padding, 1024 → 2048 → 4096 gave coverage \
0.62 → 0.75 → 0.81 (+30 % relative), while every other pack parameter combined moved it by +0.3 %.
• padding_px is in pixels of map_resolution; small islands pay for it: at 1024, padding 0/1/2/4/8 px gave \
0.81/0.71/0.62/0.47/0.26. diagnose reports the share of the map the padding takes.
• rotation_step below 90° is a trap: 45° and 15° cost 6–8 % of coverage against 90° and take 2–3× longer \
(axis-aligned islands meet flat edge to flat edge; rotated ones leave wedges).
• MaxMutations is deliberately not exposed: above a few hundred islands it burns minutes for no gain.
Pack takes ~10–25 s on 2.5k islands. One call = one undo step.
extra_params are deep-merged into RizomUV's Pack parameters (command_help('Pack') documents them); \
the measured guidance is command_help('guide:packing')."""

MEASURE_LINES = """\
• coverage — share of the 0–1 tile covered by islands, rasterized at map_resolution (pixel centres); \
1.0 would mean no empty space.
• uv_area — analytic sum of the island UV areas; above coverage when islands overlap or leave the tile.
• overlap — pixels covered by two DIFFERENT islands, the island pairs involved and examples (overlap \
inside one island, a triangulation artefact, is never counted).
• outside_tile — islands whose bounds leave [0, 1].
• island_size_px — mean / median / p10 island size in pixels at map_resolution.
• tiny_islands — islands under 4 px across, and slivers under 2 px thick.
• border_length_uv — total island border length in UV units (padding is paid along it).
• padding_share_estimate — share of the map the padding takes (only when padding_px is known)."""

MEASURE_DESCRIPTION = """\
Measure the UV layout independently of RizomUV: every number is recomputed from the UVs.
""" + MEASURE_LINES + """
map_resolution and padding_px default to the last pack's, else 1024 and unknown. About 0.5 s on 2.5k \
islands. Use it to compare layouts; diagnose says what to change."""

DIAGNOSE_DESCRIPTION = """\
Diagnose the UV layout and say which lever fixes what. Returns findings first — sorted by severity \
(high, medium, info), each a code and a message naming the fix (overlap, outside_tile, flipped_uvs, \
degenerate_islands, resolution_too_low, tiny_islands, texel_density_uneven, stretch, or clean) — then:
• metrics — the measure numbers:
""" + MEASURE_LINES + """
• degenerate_islands — islands with zero 3D area: pack cannot size them; left out of every statistic.
• texel_density — pixels per 3D unit on average, and how far islands stray from it (ratio p5/p95, \
islands more than 10 % / 25 % off).
• stretch — per-polygon distortion relative to its island (p1/p99), and the share of the 3D surface \
stretched or compressed by more than 25 %.
• flipped_uv_area — RizomUV's mirrored UV area.
Call it instead of iterating on pack parameters. Defaults as measure; 2–4 s on 2.5k islands."""

RENDER_DESCRIPTION = """\
Render the UV layout to a PNG and return the image. It explains mechanisms — where the empty space \
goes, how islands meet, which islands are distorted — and must NOT be used to rank layouts: the eye \
ranks them badly (a worse pack looked denser in testing); compare measure's coverage instead.
mode='islands': island borders on white. mode='stretch': a distortion heat map centred on the mesh's \
average texel density (grey = none, red/blue = stretched/compressed). crop=[umin, vmin, umax, vmax] \
zooms on part of the UV space (other UDIM tiles too); the longer side of the image is size_px. \
Image row 0 is the top of the tile (v max). The 20 newest renders are kept in the server's folder."""

GUIDE_PACKING = """\
# Packing UVs with RizomUV: what moves the numbers

Measured on a 2,573-island hard-surface scene (Mech8: 41,804 polygons), RizomUV 2027.0, coverage
recomputed independently from the UVs (rasterized at the map resolution).

## map_resolution dominates once islands are many

| map_resolution (2 px padding) | coverage |
|---|---|
| 1024 | 0.62 |
| 2048 | 0.75 |
| 4096 | 0.81 |

That is +30 % relative, while every other pack parameter combined moved coverage by +0.3 %.
Pack at the resolution the texture will be baked at.

## padding_px is paid by the small islands

Padding is in pixels of map_resolution, around every island. At 1024:

| padding_px | 0 | 1 | 2 | 4 | 8 |
|---|---|---|---|---|---|
| coverage | 0.81 | 0.71 | 0.62 | 0.47 | 0.26 |

diagnose estimates the share of the map the padding takes (border length × padding / 2 / resolution).
When it is above 10 %, doubling map_resolution at the same padding in pixels recovers at least half of
that share in coverage (0.62 → 0.75 at 1024 → 2048, 2 px).

## rotation_step below 90° is a trap

45° and 15° steps cost 6–8 % of coverage against 90° and take 2–3× longer: axis-aligned islands meet
flat edge to flat edge, rotated ones leave wedges.

## MaxMutations is not exposed

Above a few hundred islands it burns minutes for no gain; the pack tool refuses it above 1.

## Timings

Pack takes ~10–25 s on 2.5k islands (1024: ~10 s, 2048: ~18–20 s, padding 0: ~25 s). measure takes
~0.5 s, diagnose 2–4 s, render_layout ~0.6 s at 1024.

## Workflow

1. pack with the target map_resolution and the padding the texture needs.
2. diagnose, and act on its findings (resolution, degenerate or tiny islands, texel density, stretch).
3. render_layout to see why — never to rank two layouts: the eye ranks them badly.
"""

# AuxGroup: an island outside every tile is taken into the root group and packed with it, as the
# Pack button does; without it such an island stays where it is.
CANONICAL_PACK = {"RootGroup": "RootGroup", "AuxGroup": "RootGroup", "WorkingSet": "Visible", "Translate": True, "Scaling": {"Mode": 2},
                  "LayoutScalingMode": 2, "RecursionDepth": 1, "ProcessTileSelection": False,
                  "UsePixelUnit": True}

_MAX_MUTATIONS = ("MaxMutations above 1 is refused: above a few hundred islands it burns minutes for no gain "
                  "(measured on a 2,573-island scene). Coverage moves with map_resolution and padding_px; "
                  "call diagnose to see which one to change.")


# ------------------------------------------------------------------ helpers

def _guard(fn):
    """Any exception a tool lets out becomes a ToolError with its text: the SDK would
    otherwise hide the message from the model."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except ToolError:
            raise
        except launch.LaunchError as e:
            raise ToolError(str(e)) from e
        except Exception as e:   # noqa: BLE001
            log.exception("tool %s failed", fn.__name__)
            raise ToolError("Internal error in the RizomUV MCP server: %s: %s" % (type(e).__name__, e)) from e
    return wrapper


def _reporter(ctx):
    async def report(elapsed, message):
        try:
            await ctx.report_progress(round(float(elapsed), 1), None, message)
        except Exception as e:   # noqa: BLE001 -- progress is best effort
            log.debug("progress not delivered: %r", e)
    return report


def _absolute(path, what="path"):
    if not isinstance(path, str) or not path.strip():
        raise ToolError("%s is empty." % what)
    p = Path(os.path.expandvars(path.strip()))
    if not p.is_absolute():
        raise ToolError("%s must be absolute (RizomUV's working directory is not yours): %r" % (what, path))
    return p


def _deep_merge(base, extra):
    out = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _find_key(tree, key):
    if isinstance(tree, dict):
        for k, v in tree.items():
            if k == key:
                yield v
            yield from _find_key(v, key)
    elif isinstance(tree, list):
        for v in tree:
            yield from _find_key(v, key)


def _summarize_warning(warning):
    if not isinstance(warning, dict):
        return {"message": str(warning)[:500]}
    out = {"message": warning.get("Msg"), "code": warning.get("Code")}
    topo = warning.get("Topo3D")
    if isinstance(topo, dict) and isinstance(topo.get("Errors"), dict):
        out["topology_errors"] = len(topo["Errors"])
        first = next(iter(topo["Errors"].values()), None)
        if isinstance(first, dict) and (first.get("CustomMsg") or first.get("Msg")):
            out["first_error"] = str(first.get("CustomMsg") or first.get("Msg"))[:300]
    return out


def _ids(ids, limit=50):
    ids = list(ids or [])
    return {"count": len(ids), "ids": ids[:limit]}


def _prune(folder, keep):
    files = sorted(folder.glob("layout-*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[keep:]:
        try:
            old.unlink()
        except OSError:
            pass


def _analysis_defaults(session, map_resolution, padding_px):
    """measure/diagnose arguments, defaulting to the last pack's. A padding in pixels of the
    pack's resolution is rescaled when judged at another resolution."""
    last = session.last_pack
    res = map_resolution or (last["map_resolution"] if last else None) or 1024
    pad = padding_px
    source = "arguments"
    if pad is None and last and last.get("padding_px") is not None:
        pad = last["padding_px"] * res / last["map_resolution"]
        source = "last pack"
    elif map_resolution is None and last:
        source = "last pack"
    return res, pad, source


# ------------------------------------------------------------------ the server

def build_server(config) -> MCPServer:
    """The MCP server for one configuration; each client connection gets its own Session."""
    holder = types.SimpleNamespace(session=None, docs={})
    render_counter = itertools.count(1)

    @asynccontextmanager
    async def lifespan(app):
        session = Session(config)
        holder.session = session
        try:
            yield session
        finally:
            holder.session = None
            # the client may be tearing us down; the instance we own must still be quit
            with anyio.CancelScope(shield=True):
                with anyio.move_on_after(5):
                    await session.close()

    mcp = MCPServer("rizomuv", title="RizomUV", instructions=INSTRUCTIONS, version=__version__,
                    lifespan=lifespan, log_level=config.log_level)

    def session_of(ctx) -> Session:
        return ctx.request_context.lifespan_context

    # -------------------------------------------------------------- session

    @mcp.tool(title="RizomUV session", description=(
        "Report the RizomUV connection without changing it: attached (the artist's own open RizomUV) or a "
        "private headless one this server started, its version, port and pid, a summary of its scene, the "
        "RizomUV instances found on this machine (and whether another assistant holds them), the last pack "
        "parameters, and which executable a headless launch would use. Never starts RizomUV."),
        annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False))
    @_guard
    async def session_info(ctx: Context) -> dict[str, Any]:
        return await session_of(ctx).info()

    @mcp.tool(title="Connect to RizomUV", description=(
        "Choose or switch the RizomUV this session drives (other tools connect on their own; call this only "
        "to choose). target='auto' attaches to the RizomUV the artist has open, else starts a private "
        "headless one; 'attach' only attaches; 'headless' always uses a private headless instance (it holds a "
        "licence seat until the session ends). port attaches to exactly the instance on that link port (one "
        "started with -id <port>). Switching away from a headless instance this server started quits it; the "
        "artist's RizomUV is never quit. Returns what session_info returns."),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                    open_world_hint=False))
    @_guard
    async def connect(
            target: Annotated[Literal["auto", "attach", "headless"], Field(
                description="auto: the artist's open RizomUV, else a private headless one; attach: only "
                            "attach; headless: a private headless instance.")] = "auto",
            port: Annotated[int | None, Field(description="Attach to exactly this RizomUVLink port.",
                                              ge=1, le=65535)] = None,
            ctx: Context = None) -> dict[str, Any]:
        session = session_of(ctx)
        await session.switch(target, port, _reporter(ctx))
        return await session.info()

    # -------------------------------------------------------------- scene

    @mcp.tool(title="Load a mesh", description=(
        "Load a mesh file (OBJ, FBX, USD...) into RizomUV with its UVs, replacing the whole scene; not "
        "undoable. path must be absolute. On the artist's own RizomUV this refuses while their scene holds a "
        "mesh, because loading over the link never offers to save it: ask the artist first, then pass "
        "replace_scene=true. Returns a summary of the new scene; topology warnings come summarized."),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False,
                                    open_world_hint=False))
    @_guard
    async def load(
            path: Annotated[str, Field(description="Absolute path of the mesh file.")],
            replace_scene: Annotated[bool, Field(
                description="Required to replace a scene the artist has open in their RizomUV.")] = False,
            ctx: Context = None) -> dict[str, Any]:
        p = _absolute(path)
        if not p.is_file():
            raise ToolError("No such file: %s" % p)
        session, progress = session_of(ctx), _reporter(ctx)
        await session.ensure(progress)
        if session.attached:
            scene = await session.scene(progress)
            if scene.get("has_mesh") and not replace_scene:
                raise ToolError(
                    "The artist's RizomUV has a scene open (%s, %s polygons, %s islands). Loading replaces it "
                    "without asking to save it. Ask the artist, then call load with replace_scene=true."
                    % (scene.get("file") or "unsaved", scene.get("polygons"), scene.get("islands")))
        t0 = time.perf_counter()
        result = await session.execute(
            "Load", {"File": {"Path": p.as_posix(), "XYZUVW": True, "ImportGroups": True, "UVWProps": True},
                     "__Focus": True}, label="Load", progress=progress)
        seconds = round(time.perf_counter() - t0, 2)
        out = {"loaded": str(p)}
        if isinstance(result, dict):
            if "Error" in result:
                err = result["Error"]
                raise ToolError("RizomUV could not load %s: %s" % (
                    p, err.get("Msg", err) if isinstance(err, dict) else err))
            if "Warning" in result:
                out["warning"] = _summarize_warning(result["Warning"])
        session.last_pack = None
        out["scene"] = await session.scene(progress)
        out["seconds"] = seconds
        if session.attached:
            out["note"] = "Loaded into the artist's RizomUV: the scene they had open was replaced."
        return out

    @mcp.tool(title="Unfold", description=(
        "Unfold (flatten) the UV islands of the working set to minimize distortion; seams are not changed. "
        "working_set is RizomUV's WorkingSet ('Visible' by default, 'Selected' for the selection). extra_params "
        "are merged into RizomUV's Unfold parameters (see command_help('Unfold')). Returns the islands whose "
        "unfold is not bijective (they overlap themselves: cut them) and the time. One call = one undo step."),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                                    open_world_hint=False))
    @_guard
    async def unfold(
            working_set: Annotated[str, Field(description="RizomUV WorkingSet, e.g. Visible or Selected.")] = "Visible",
            extra_params: Annotated[dict[str, Any] | None, Field(
                description="More Unfold parameters, merged over the defaults.")] = None,
            ctx: Context = None) -> dict[str, Any]:
        session, progress = session_of(ctx), _reporter(ctx)
        params = _deep_merge({"WorkingSet": working_set, "PrimType": "Island"}, extra_params or {})
        t0 = time.perf_counter()
        result = await session.execute("Unfold", params, label="Unfold", progress=progress)
        out = {"params": params, "seconds": round(time.perf_counter() - t0, 2)}
        if isinstance(result, dict):
            out["bijection_failed_islands"] = _ids(result.get("BijectionFailedIslandIDs"))
            rest = {k: v for k, v in result.items() if k != "BijectionFailedIslandIDs"}
            if rest:
                out["rizomuv_result"] = rest
        return out

    @mcp.tool(title="Pack", description=PACK_DESCRIPTION,
              annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                                          open_world_hint=False))
    @_guard
    async def pack(
            padding_px: Annotated[int, Field(description="Padding around islands, in pixels of map_resolution.",
                                             ge=0, le=64)] = 2,
            map_resolution: Annotated[int, Field(description="Texture resolution the layout is packed for.",
                                                 ge=64, le=16384)] = 1024,
            rotation_step: Annotated[float, Field(description="Rotation step in degrees; below 90 costs coverage "
                                                              "and time.", ge=1, le=360)] = 90,
            measure: Annotated[bool, Field(description="Measure the result (about 0.5 s).")] = True,
            extra_params: Annotated[dict[str, Any] | None, Field(
                description="More Pack parameters, deep-merged over the canonical ones.")] = None,
            ctx: Context = None) -> dict[str, Any]:
        extra = extra_params or {}
        if any(isinstance(v, (int, float)) and not isinstance(v, bool) and v > 1
               for v in _find_key(extra, "MaxMutations")):
            raise ToolError(_MAX_MUTATIONS)
        params = _deep_merge(dict(CANONICAL_PACK, PaddingSizePx=padding_px, MapResolution=map_resolution,
                                  Rotate={"Initial": "Auto", "Min": 0.0, "Max": 360.0, "Step": float(rotation_step)}),
                             extra)
        session, progress = session_of(ctx), _reporter(ctx)
        t0 = time.perf_counter()
        await session.execute("Pack", params, label="Pack", progress=progress)
        out = {"params": params, "seconds": round(time.perf_counter() - t0, 2)}
        res = params.get("MapResolution")
        pad = params.get("PaddingSizePx") if params.get("UsePixelUnit") else None
        session.last_pack = {"map_resolution": int(res) if isinstance(res, (int, float)) else map_resolution,
                             "padding_px": pad}
        if measure:
            try:
                out["measure"] = await session.op(
                    "measure", {"res": session.last_pack["map_resolution"], "padding_px": pad,
                                "attached": session.attached}, label="Measuring", progress=progress)
            except ToolError as e:
                out["measure_error"] = str(e)
        return out

    if config.graph:
        @mcp.tool(title="Automatic seams", description=(
            "Unwrap the whole visible mesh automatically: RizomUV's node graph cuts the seams, unfolds and "
            "packs, in one call and one undo step. It replaces the UVs and seams the mesh has. The graph is "
            "the one this server is set up with; it takes from a few seconds to a few minutes on large meshes. "
            "Returns what the graph did, then measures the layout (as measure does) unless measure=false. "
            "Needs a mesh loaded, and a RizomUV with the node graph turned on."),
            annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False,
                                        open_world_hint=False))
        @_guard
        async def auto_seams(
                measure: Annotated[bool, Field(description="Measure the result (about 0.5 s).")] = True,
                ctx: Context = None) -> dict[str, Any]:
            graph_path = Path(config.graph)
            try:
                text = graph_path.read_text(encoding="utf-8")
                doc = json.loads(text)
            except OSError as e:
                raise ToolError("The graph this server is set up with cannot be read (%s): %s" % (graph_path, e))
            except ValueError as e:
                raise ToolError("The graph this server is set up with is not JSON (%s): %s" % (graph_path, e))
            session, progress = session_of(ctx), _reporter(ctx)
            if not await session.graph_enabled(progress):
                if session.owned:
                    raise ToolError(
                        "The node graph is off in the headless RizomUV this server started: the build has no "
                        "Prefs.Experimental.Graph (it is older than the switch), or turning it on failed (see the "
                        "server log). Point the server at a newer RizomUV, or use unfold and pack.")
                raise ToolError(
                    "The node graph is off in this RizomUV. It is turned on by the preference "
                    "Prefs.Experimental.Graph, read when RizomUV starts: the artist sets it to true (for instance "
                    "Set {Path: Prefs.Experimental.Graph, Value: true} then SavePreferences, in the script editor) "
                    "and restarts RizomUV.")
            scene = await session.scene(progress)
            if not scene.get("has_mesh"):
                raise ToolError("No mesh is loaded: call load first.")
            t0 = time.perf_counter()
            try:
                result = await session.execute("Graph", {"Graph": text}, label="Automatic seams",
                                               progress=progress, timeout_ms=1800000)
            except ToolError as e:
                # the Graph task gives the mesh back as it was when a node fails; a lost link is
                # another matter, and its message already says what is known
                if "link" in str(e).lower():
                    raise
                raise ToolError("The graph failed and the mesh is as it was: %s" % e) from None
            seconds = round(time.perf_counter() - t0, 2)
            if isinstance(result, dict) and "Error" in result:
                err = result["Error"]
                raise ToolError("The graph failed and the mesh is as it was: %s" % (
                    err.get("Msg", err) if isinstance(err, dict) else err))
            report = result if isinstance(result, dict) else {}
            session.last_pack = None
            out = {"graph": graph_path.name, "seconds": seconds,
                   "nodes_run": len(report.get("Executed") or []),
                   "nodes_skipped": len(report.get("Skipped") or [])}
            if isinstance(report.get("Coverage"), dict):
                cov = report["Coverage"]
                out["coverage_of_the_mesh"] = {k: cov[k] for k in ("Ratio", "UncoveredParts") if k in cov}
            # The stages of a cascade warn about what they leave to the next stage (a region with
            # flipped triangles goes down Rest): the measure below is the verdict on the result.
            out["stage_warnings"] = len(report.get("Warnings") or [])
            out["scene"] = await session.scene(progress)
            if measure:
                try:
                    out["measure"] = await session.op("measure", {"res": 1024, "padding_px": None,
                                                                  "attached": session.attached},
                                                      label="Measuring", progress=progress)
                except ToolError as e:
                    out["measure_error"] = str(e)
            if session.attached:
                out["note"] = "Done in the artist's RizomUV: one undo reverts the whole graph."
            return out

    # -------------------------------------------------------------- analysis

    @mcp.tool(title="Measure the layout", description=MEASURE_DESCRIPTION,
              annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False))
    @_guard
    async def measure(
            map_resolution: Annotated[int | None, Field(description="Resolution to judge at (default: last pack's, "
                                                                    "else 1024).", ge=64, le=16384)] = None,
            padding_px: Annotated[float | None, Field(description="Padding in pixels at map_resolution (default: "
                                                                  "last pack's).", ge=0, le=256)] = None,
            ctx: Context = None) -> dict[str, Any]:
        session, progress = session_of(ctx), _reporter(ctx)
        await session.ensure(progress)
        res, pad, source = _analysis_defaults(session, map_resolution, padding_px)
        out = await session.op("measure", {"res": res, "padding_px": pad, "attached": session.attached},
                               label="Measuring", progress=progress)
        out["parameters_from"] = source
        return out

    @mcp.tool(title="Diagnose the layout", description=DIAGNOSE_DESCRIPTION,
              annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False))
    @_guard
    async def diagnose(
            map_resolution: Annotated[int | None, Field(description="Resolution to judge at (default: last pack's, "
                                                                    "else 1024).", ge=64, le=16384)] = None,
            padding_px: Annotated[float | None, Field(description="Padding in pixels at map_resolution (default: "
                                                                  "last pack's).", ge=0, le=256)] = None,
            ctx: Context = None) -> dict[str, Any]:
        session, progress = session_of(ctx), _reporter(ctx)
        await session.ensure(progress)
        res, pad, source = _analysis_defaults(session, map_resolution, padding_px)
        out = await session.op("diagnose", {"res": res, "padding_px": pad, "attached": session.attached,
                                            "tmp_dir": str(paths.state_dir("tmp"))},
                               label="Diagnosing", progress=progress)
        out["parameters_from"] = source
        return out

    @mcp.tool(title="Render the layout", description=RENDER_DESCRIPTION,
              annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=False, open_world_hint=False))
    @_guard
    async def render_layout(
            size_px: Annotated[int, Field(description="Longer side of the image in pixels.", ge=256, le=2048)] = 1024,
            mode: Annotated[Literal["islands", "stretch"], Field(
                description="islands: borders on white; stretch: distortion heat map.")] = "islands",
            crop: Annotated[list[float] | None, Field(
                description="[umin, vmin, umax, vmax] to zoom on; default the 0–1 tile.",
                min_length=4, max_length=4)] = None,
            ctx: Context = None) -> CallToolResult:
        if crop is not None and not (crop[2] > crop[0] and crop[3] > crop[1]):
            raise ToolError("crop is [umin, vmin, umax, vmax] with umin < umax and vmin < vmax, not %r." % (crop,))
        session, progress = session_of(ctx), _reporter(ctx)
        folder = paths.state_dir("renders")
        name = "layout-%s-%d-%s.png" % (time.strftime("%Y%m%d-%H%M%S"), next(render_counter), mode)
        t0 = time.perf_counter()
        r = await session.op("render", {"path": str(folder / name), "size_px": size_px, "mode": mode, "crop": crop},
                             label="Rendering", progress=progress)
        _prune(folder, RENDERS_KEPT)
        data = Path(r["path"]).read_bytes()
        area = "the 0–1 tile" if crop is None else "u %g–%g, v %g–%g" % (crop[0], crop[2], crop[1], crop[3])
        what = ("island borders on white" if mode == "islands" else
                "stretch heat map (grey = as the average texel density, red/blue = stretched/compressed)")
        caption = ("UV layout of %s, %s, %d×%d px (row 0 = top). It shows mechanisms, it does not rank layouts: "
                   "compare coverage from measure. Saved as %s." % (area, what, r["width"], r["height"], r["path"]))
        structured = dict(r, mode=mode, crop=crop, seconds=round(time.perf_counter() - t0, 2))
        return CallToolResult(content=[TextContent(type="text", text=caption),
                                       ImageContent(type="image", data=base64.b64encode(data).decode("ascii"),
                                                    mime_type="image/png")],
                              structured_content=structured)

    # -------------------------------------------------------------- output, history

    @mcp.tool(title="Save", description=(
        "Save the scene with its UVs to a NEW file; the format follows the extension (.obj, .fbx, .usd...). "
        "path must be absolute; an existing file is refused unless overwrite=true. The artist's current file "
        "in RizomUV is left alone (their next Ctrl+S still goes to their own file). Checks that the file was "
        "written: a demo licence silently writes nothing."),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False,
                                    open_world_hint=False))
    @_guard
    async def save(
            path: Annotated[str, Field(description="Absolute path of the file to write.")],
            overwrite: Annotated[bool, Field(description="Allow replacing an existing file.")] = False,
            ctx: Context = None) -> dict[str, Any]:
        p = _absolute(path)
        if p.exists() and not overwrite:
            raise ToolError("%s already exists. save writes new files; pass overwrite=true to replace it (ask the "
                            "artist first if it is theirs)." % p)
        if not p.parent.is_dir():
            raise ToolError("The folder %s does not exist." % p.parent)
        before = p.stat().st_mtime_ns if p.exists() else None
        session, progress = session_of(ctx), _reporter(ctx)
        t0 = time.perf_counter()
        await session.execute("Save", {"File": {"Path": p.as_posix(), "UVWProps": True},
                                       "__DontUpdateGUIFilePath": True}, label="Save", progress=progress)
        if session.attached:
            # Save clears the artist's unsaved-work flag in current builds; any command sets it again
            await session.execute("Get", "Vars.Infos.Version.Full", label="Save")
        if not p.is_file() or p.stat().st_size == 0 or (before is not None and p.stat().st_mtime_ns == before):
            raise ToolError("RizomUV did not write %s (a demo licence silently skips saving)." % p)
        return {"path": str(p), "bytes": p.stat().st_size, "seconds": round(time.perf_counter() - t0, 2)}

    async def _uv_sets_state(session, progress):
        names = await session.execute("ItemNames", "Lib.UVSets", label="UV sets", progress=progress)
        names = [str(n) for n in names] if isinstance(names, list) else []
        current = await session.execute("Get", "Lib.CurrentUVSetName", label="UV sets", progress=progress)
        sets = []
        for name in names:
            texture = await session.execute("Get", "Lib.UVSets.%s.RootGroup.Properties.Texture" % name,
                                            label="UV sets", progress=progress)
            sets.append({"name": name, "current": name == current,
                         "texture": texture if isinstance(texture, str) and texture else None})
        return {"current": current, "uv_sets": sets}

    @mcp.tool(title="UV sets", description=(
        "List and manage the UV sets of the mesh: several UV layouts of the same polygons (lightmap, "
        "texture set, an old and a new unwrap). Tools such as unfold and pack work on the CURRENT set. "
        "action='list' (default) changes nothing. 'duplicate' copies a set (source, default the current one) "
        "into a new set name; 'create' adds an empty set name to unwrap from scratch (no seams, its UVs are the 3D "
        "positions: cut and unfold it); 'rename' renames "
        "source (default the current set) to name; 'delete' removes the set name (never the current or the "
        "last one); 'set_current' makes name the set the other tools work on. Every action except list is one "
        "undo step. Returns the sets after the action, the current one, and the texture assigned to each."),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False,
                                    open_world_hint=False))
    @_guard
    async def uv_sets(
            action: Annotated[Literal["list", "duplicate", "create", "rename", "delete", "set_current"], Field(
                description="What to do; list changes nothing.")] = "list",
            name: Annotated[str | None, Field(
                description="The set created, renamed to, deleted or made current.")] = None,
            source: Annotated[str | None, Field(
                description="duplicate and rename: the set to start from (default: the current set).")] = None,
            ctx: Context = None) -> dict[str, Any]:
        session, progress = session_of(ctx), _reporter(ctx)
        await session.ensure(progress)
        if action == "list":
            return await _uv_sets_state(session, progress)
        if not name or not name.strip():
            raise ToolError("uv_sets %s needs a name." % action)
        name = name.strip()
        before = await _uv_sets_state(session, progress)
        known = [s["name"] for s in before["uv_sets"]]
        if action in ("duplicate", "create") and name in known:
            raise ToolError("A UV set named %r exists already. UV sets: %s." % (name, ", ".join(known)))
        if action in ("delete", "set_current") and name not in known:
            raise ToolError("%r is not a UV set of this scene. UV sets: %s." % (name, ", ".join(known)))
        if source and action in ("duplicate", "rename") and source not in known:
            raise ToolError("source %r is not a UV set of this scene. UV sets: %s." % (source, ", ".join(known)))
        if action == "delete" and name == before["current"]:
            raise ToolError("%r is the current UV set and cannot be deleted; make another set current first "
                            "(action='set_current')." % name)
        mode = {"duplicate": "Duplicate", "create": "Create", "rename": "Rename", "delete": "Delete",
                "set_current": "SetCurrent"}[action]
        params = {"Mode": mode, "Name": name}
        if source and action in ("duplicate", "rename"):
            params["Source"] = source
        result = await session.execute("Uvset", params, label="UV sets", progress=progress)
        if isinstance(result, dict) and "Error" in result:
            err = result["Error"]
            raise ToolError("RizomUV refused: %s" % (err.get("Msg", err) if isinstance(err, dict) else err))
        if action == "set_current":
            session.last_pack = None   # the last pack was of another layout
        out = await _uv_sets_state(session, progress)
        out["done"] = "%s %s" % (action, name)
        return out

    @mcp.tool(title="Transfer a texture", description=(
        "Bake a texture from one UV set onto another UV set of the same mesh: the image painted on "
        "source_uv_set is resampled onto the layout of dest_uv_set and written to output_file (format from the "
        "extension: .png, .tga, .jpg, .bmp, .tif). Use it after re-unwrapping a mesh that already has textures. "
        "source_uv_set defaults to the current UV set and source_file to the texture already assigned to that "
        "set; dest_uv_set must be another set (make one with uv_sets action='duplicate' before "
        "changing its UVs). Set normal_map=true for a tangent-space normal map. output_file must be "
        "absolute and new unless overwrite=true. The scene is not changed. extra_params are merged into "
        "RizomUV's TextureTransfer parameters (Samples, Padding, ColorSpace, Maps for a texture set...)."),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                    open_world_hint=False))
    @_guard
    async def transfer_texture(
            dest_uv_set: Annotated[str, Field(description="The UV set the new image is made for.")],
            output_file: Annotated[str, Field(description="Absolute path of the image to write.")],
            source_uv_set: Annotated[str | None, Field(
                description="The UV set the source image is painted on (default: the current set).")] = None,
            source_file: Annotated[str | None, Field(
                description="Absolute path of the source image (default: the texture of source_uv_set).")] = None,
            normal_map: Annotated[bool, Field(description="The image is a tangent-space normal map.")] = False,
            size_px: Annotated[int | None, Field(
                description="Width and height of the output image (default: the source image's size).",
                ge=16, le=16384)] = None,
            overwrite: Annotated[bool, Field(description="Allow replacing an existing file.")] = False,
            extra_params: Annotated[dict[str, Any] | None, Field(
                description="More TextureTransfer parameters, merged over these.")] = None,
            ctx: Context = None) -> dict[str, Any]:
        out_path = _absolute(output_file, "output_file")
        if out_path.exists() and not overwrite:
            raise ToolError("%s already exists. Pass overwrite=true to replace it (ask the artist first if it is "
                            "theirs)." % out_path)
        if not out_path.parent.is_dir():
            raise ToolError("The folder %s does not exist." % out_path.parent)
        session, progress = session_of(ctx), _reporter(ctx)
        await session.ensure(progress)
        sets = await session.execute("ItemNames", "Lib.UVSets", label="UV sets", progress=progress)
        sets = [str(s) for s in sets] if isinstance(sets, list) else []
        source = source_uv_set or await session.execute("Get", "Lib.CurrentUVSetName", label="UV sets",
                                                        progress=progress)
        for name, what in ((source, "source_uv_set"), (dest_uv_set, "dest_uv_set")):
            if sets and name not in sets:
                raise ToolError("%s %r is not a UV set of this scene. Its UV sets: %s." % (what, name, ", ".join(sets)))
        if source == dest_uv_set:
            raise ToolError("source_uv_set and dest_uv_set are both %r: a transfer goes from one UV set to "
                            "another. UV sets of this scene: %s." % (source, ", ".join(sets)))
        if source_file:
            src_path = _absolute(source_file, "source_file")
        else:
            assigned = await session.execute("Get", "Lib.UVSets.%s.RootGroup.Properties.Texture" % source,
                                             label="UV sets", progress=progress)
            if not isinstance(assigned, str) or not assigned.strip():
                raise ToolError("The UV set %r has no texture assigned; give source_file." % source)
            src_path = Path(assigned)
        if not src_path.is_file():
            raise ToolError("No such file: %s" % src_path)
        if src_path.resolve() == out_path.resolve():
            raise ToolError("output_file is the source image; write the result to another file.")
        params = {"SourceUVSet": source, "DestUVSet": dest_uv_set, "SourceFile": src_path.as_posix(),
                  "OutputFile": out_path.as_posix(), "NormalMap": bool(normal_map)}
        if size_px:
            params.update(Width=size_px, Height=size_px)
        params = _deep_merge(params, extra_params or {})
        before = out_path.stat().st_mtime_ns if out_path.exists() else None
        t0 = time.perf_counter()
        result = await session.execute("TextureTransfer", params, label="Texture transfer", progress=progress,
                                       timeout_ms=600000)
        seconds = round(time.perf_counter() - t0, 2)
        if isinstance(result, dict) and "Error" in result:
            err = result["Error"]
            raise ToolError("RizomUV could not transfer the texture: %s" % (
                err.get("Msg", err) if isinstance(err, dict) else err))
        if "Maps" not in params and (not out_path.is_file() or
                                     (before is not None and out_path.stat().st_mtime_ns == before)):
            raise ToolError("RizomUV did not write %s (a demo licence silently skips it)." % out_path)
        report = result.get("TextureTransfer") if isinstance(result, dict) else None
        out = {"output_file": str(out_path), "source_file": str(src_path), "source_uv_set": source,
               "dest_uv_set": dest_uv_set, "seconds": seconds}
        if isinstance(report, dict):
            keep = ("Width", "Height", "BitDepth", "NormalMap", "Coverage", "Device", "Maps")
            out.update({k.lower() if k != "NormalMap" else "normal_map": report[k] for k in keep if k in report})
        out["note"] = ("The image is written; the scene is unchanged. In RizomUV it becomes visible once loaded "
                       "as the texture of %s." % dest_uv_set)
        return out

    @mcp.tool(title="Undo", description=(
        "Undo the last RizomUV steps (default 1). Each unfold, pack or other scene-changing call is one step; "
        "load, save, measure, diagnose and render_layout are not on the undo stack. On the artist's own "
        "RizomUV, undo walks their history too: if they worked in RizomUV since your last call, it reverts "
        "their action, not yours."),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False,
                                    open_world_hint=False))
    @_guard
    async def undo(
            steps: Annotated[int, Field(description="How many steps to undo.", ge=1, le=20)] = 1,
            ctx: Context = None) -> dict[str, Any]:
        session, progress = session_of(ctx), _reporter(ctx)
        results = []
        for i in range(steps):
            results.append(await session.execute("Undo", {}, label="Undo %d/%d" % (i + 1, steps), progress=progress))
        out = {"undone_steps": steps, "results": results}
        if session.attached:
            out["note"] = ("This is the artist's RizomUV: if they worked in it since your last call, their own "
                           "last action was undone.")
        return out

    @mcp.tool(title="Run a RizomUV command", description=(
        "Run another RizomUV command through RizomUVLink (command_help lists them and documents each, "
        "parameters included: read it before the first call of a command). Prefer the dedicated tools, which carry guardrails. A policy "
        "decides what passes: on the artist's RizomUV only reads, undoable scene edits and undo; on a private "
        "headless instance also loading, saving, exporting and setting session values. Never Quit/Exit, "
        "preferences, or Get('Lib') / Get('Lib.Mesh') (they crash RizomUV or cannot be serialized). params is "
        "the command's parameter table, or a path string for Get, Count, ItemNames and Eval. Results longer "
        "than 20,000 characters of JSON are truncated."),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False,
                                    open_world_hint=False))
    @_guard
    async def run_command(
            command: Annotated[str, Field(description="RizomUV command name, e.g. Get, ItemNames, Select, Cut.")],
            params: Annotated[dict[str, Any] | list[Any] | str | int | float | bool | None, Field(
                description="Parameter table, or a path string for Get/Count/ItemNames/Eval.")] = None,
            ctx: Context = None) -> dict[str, Any]:
        command = command.strip()
        on_attached, on_headless = policy.check(command, params, True), policy.check(command, params, False)
        if not on_attached[0] and not on_headless[0]:
            raise ToolError(on_headless[1])
        session, progress = session_of(ctx), _reporter(ctx)
        await session.ensure(progress)
        allowed, reason = on_attached if session.attached else on_headless
        if not allowed:
            raise ToolError(reason)
        t0 = time.perf_counter()
        value = await session.execute(command, params if params is not None else {}, label=command,
                                      progress=progress)
        if command == "Load":
            session.last_pack = None
        out = {"command": command, "seconds": round(time.perf_counter() - t0, 2), "policy": reason}
        text = json.dumps(value, allow_nan=True, default=repr)
        if len(text) > RESULT_LIMIT:
            out["result_truncated"] = text[:RESULT_LIMIT]
            out["note"] = ("The result is %d characters of JSON; only the first %d are shown. Read a narrower "
                           "path (ItemNames lists a container's children)." % (len(text), RESULT_LIMIT))
        else:
            out["result"] = value
        return out

    # -------------------------------------------------------------- resources

    async def docs_table():
        """({command: doc}, source). Live from the connected RizomUV (cached per version),
        else the module shipped with the binding. Never starts RizomUV."""
        session = holder.session
        if session is not None and session.connected and session.version:
            version = session.version
            if version in holder.docs:
                return holder.docs[version], "RizomUV %s" % version
            cache = paths.state_dir("docs") / (re.sub(r"[^\w.-]", "_", version) + ".json")
            try:
                table = json.loads(cache.read_text(encoding="utf-8"))
                holder.docs[version] = table
                return table, "RizomUV %s" % version
            except (OSError, ValueError):
                pass
            if not session.inflight and not session._lock.locked():
                tmp = paths.state_dir("tmp") / ("RizomUVLinkBase-%d.py" % os.getpid())
                try:
                    await session.op("docs", {"path": str(tmp)}, label="Reading the command docs")
                    table = docs.parse_module_file(tmp)
                    if table:
                        cache.write_text(json.dumps(table), encoding="utf-8")
                        holder.docs[version] = table
                        return table, "RizomUV %s" % version
                except (ToolError, OSError) as e:
                    log.info("live command docs unavailable, using the shipped module: %s", e)
                finally:
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
        shipped = docs.shipped_module_path()
        if shipped is None:
            return {}, "no documentation found"
        if "shipped" not in holder.docs:
            text = shipped.read_text(encoding="utf-8", errors="replace")
            holder.docs["shipped"] = (docs.parse_module_text(text), docs.module_version(text))
        table, version = holder.docs["shipped"]
        return table, "the RizomUVLinkBase.py shipped with the binding (RizomUV %s)" % (version or "?")

    def rule_of(name):
        return policy.rule_info(name) or {"category": "unclassified", "allowed_attached": False,
                                          "allowed_headless": False,
                                          "note": "not classified by this server, so not passed through"}

    @mcp.tool(title="RizomUV command help", description=(
        "The documentation of RizomUV's own commands, for run_command. Without name: every command with a "
        "one-line summary and whether run_command passes it on the artist's RizomUV and on a private headless "
        "one. With name: that command's full documentation, its parameters and their defaults. "
        "name='guide:packing' gives the measured packing guide. Never starts RizomUV; once connected the "
        "documentation is the running version's."),
        annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False))
    @_guard
    async def command_help(
            name: Annotated[str | None, Field(
                description="A command name (e.g. Cut, Select, IslandGroups), or 'guide:packing'.")] = None,
            ctx: Context = None) -> dict[str, Any]:
        if name and name.strip().lower() in ("guide:packing", "guide/packing", "packing guide"):
            return {"guide": "packing", "text": GUIDE_PACKING}
        table, source = await docs_table()
        if not name or not name.strip():
            commands = []
            for cmd in sorted(table):
                rule = rule_of(cmd)
                if not (rule["allowed_attached"] or rule["allowed_headless"]):
                    continue
                commands.append({"name": cmd, "summary": docs.summary_line(table[cmd], 120),
                                 "artist_instance": rule["allowed_attached"], "headless": rule["allowed_headless"]})
            return {"commands": commands, "guides": ["guide:packing"], "source": source,
                    "note": ("Only the commands run_command passes are listed. Prefer the dedicated tools (load, "
                             "unfold, pack, save, uv_sets, transfer_texture...) where one exists.")}
        cmd = name.strip()
        if cmd not in table:
            folded = {n.lower(): n for n in table}.get(cmd.lower())
            close = folded or next(iter(difflib.get_close_matches(cmd, list(table), n=1, cutoff=0.6)), None)
            raise ToolError("Unknown RizomUV command %r.%s Call command_help without name to list them."
                            % (cmd, " Did you mean %r?" % close if close else ""))
        rule = rule_of(cmd)
        return {"name": cmd, "doc": table[cmd] or "(no documentation)",
                "run_command": {"artist_instance": rule["allowed_attached"], "headless": rule["allowed_headless"],
                                "category": rule["category"], "note": rule["note"]},
                "source": source}

    @mcp.resource("rizomuv://commands", name="commands", title="RizomUV commands", mime_type="application/json",
                  description="Every RizomUV command: category, whether run_command passes it on the artist's "
                              "instance and on a headless one, and a one-line summary.")
    async def commands_index() -> str:
        table, source = await docs_table()
        items = []
        for name in table:
            rule = rule_of(name)
            items.append({"name": name, "category": rule["category"], "allowed_attached": rule["allowed_attached"],
                          "allowed_headless": rule["allowed_headless"], "summary": docs.summary_line(table[name]),
                          "note": rule["note"]})
        return json.dumps(items, indent=1)

    @mcp.resource("rizomuv://command/{name}", name="command_doc", title="RizomUV command documentation",
                  mime_type="text/markdown",
                  description="The documentation of one RizomUV command (its parameters and defaults), and what "
                              "run_command allows with it.")
    async def command_doc(name: str) -> str:
        table, source = await docs_table()
        if name not in table:
            folded = {n.lower(): n for n in table}.get(name.lower())
            hint = " Did you mean '%s'?" % folded if folded else " command_help lists them."
            raise ResourceNotFoundError("Unknown RizomUV command '%s'.%s" % (name, hint))
        rule = rule_of(name)
        allowed = {True: "yes", False: "no"}
        return "\n".join([
            "# %s" % name, "", table[name] or "(no documentation)", "", "---", "",
            "run_command: %s on the artist's RizomUV, %s on a private headless one (%s: %s)."
            % (allowed[rule["allowed_attached"]], allowed[rule["allowed_headless"]], rule["category"], rule["note"]),
            "", "Source: %s." % source, ""])

    @mcp.resource("rizomuv://guide/packing", name="packing_guide", title="Packing guide",
                  mime_type="text/markdown",
                  description="What moves UV coverage in RizomUV's Pack, measured: resolution, padding, rotation.")
    def packing_guide() -> str:
        return GUIDE_PACKING

    return mcp
