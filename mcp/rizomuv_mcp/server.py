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
import math
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
undo reverts one tool step. On the artist's RizomUV the viewports follow your work: load, unfold, optimize, pack, auto_seams and undo frame the result; use view to show the artist something in particular (a tile, islands, the selection, a 3D angle)."""

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
• keep_scale=true keeps the islands' size (a texel density set before stays); without it, Pack rescales them \
to fill the tile.
• distribute: before packing, spread the islands over the tiles, those outside the tile grid included, as the Pack button does when its 'Distribute in tiles' toggle is on; by default it follows that toggle (Prefs.Layout.Distribute, on unless the artist turned it off). distribute=false packs the islands that are in the tiles and leaves those outside where they are, as the button does with the toggle off.
Pack takes ~10–25 s on 2.5k islands. One call = one undo step, two when it distributes first.
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

# AuxGroup: the islands outside every tile, in the root group, are packed with the tiles' -- after the
# IslandGroups distribution the pack tool runs first, as the Pack button does (see pack); without the
# distribution the tool drops it, as the button does.
CANONICAL_PACK = {"RootGroup": "RootGroup", "AuxGroup": "RootGroup", "WorkingSet": "Visible", "Translate": True, "Scaling": {"Mode": 2},
                  "LayoutScalingMode": 2, "RecursionDepth": 1, "ProcessTileSelection": False,
                  "UsePixelUnit": True}

# The camera slides to its new place rather than jumping: in milliseconds, for the framing that
# follows a tool, and for the view tool by default.
FOLLOW_ANIMATE_MS = 300
VIEW_ANIMATE_MS = 400
# Room left around what is framed by a box (a tile, islands), as the A key leaves some.
FRAME_MARGIN = 1.1
# How long a camera call waits for a view RizomUV is still creating at startup, in seconds.
VIEW_CREATION_WAIT_S = 3.0

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

    # -------------------------------------------------------------- the viewports

    def _on_screen(session):
        """The artist's RizomUV, the only one with viewports to look at."""
        return session.attached and not session.headless

    async def _camera(session, progress, view, params=None):
        state = await session.execute("ViewCamera", dict(params or {}, View=view) if params else view,
                                      label="View %s" % view, progress=progress)
        if isinstance(state, dict) and "Error" in state:
            err = state["Error"]
            raise ToolError("RizomUV refused the %s camera: %s" % (view, err.get("Msg", err) if isinstance(err, dict) else err))
        if not isinstance(state, dict):
            raise ToolError("ViewCamera returned %r for the %s view." % (state, view))
        return state

    def _shown(state):
        size = state.get("ViewportSize") or {}
        return (size.get("Width") or 0) > 0 and (size.get("Height") or 0) > 0

    def _not_created(error):
        # CCommandViewCamera: the viewport has no GL canvas yet. RizomUV opens its link port before the
        # startup sequence has built the viewports, so a client that starts it, or attaches right after
        # it started, can be faster than the views.
        return "does not exist yet" in str(error)

    async def _camera_when_created(session, progress, view, wait_s=VIEW_CREATION_WAIT_S):
        """The camera state, waiting a moment for a view still being created at startup."""
        deadline = time.monotonic() + wait_s
        while True:
            try:
                return await _camera(session, progress, view)
            except ToolError as e:
                if not _not_created(e) or time.monotonic() >= deadline:
                    raise
            await anyio.sleep(0.25)

    async def _follow(session, progress, out, views=("UV",)):
        """After a tool that changed what the viewports show, frame them on the result, sliding there,
        so that the artist watching sees it. Only on the artist's own RizomUV, only views on screen,
        and never with --no-follow-view. A camera that cannot be moved costs the tool nothing."""
        if not config.follow_view or not _on_screen(session):
            return
        framed = []
        for view in views:
            try:
                if _shown(await _camera_when_created(session, progress, view)):
                    await _camera(session, progress, view, {"Frame": "All", "Animate": FOLLOW_ANIMATE_MS})
                    framed.append(view)
            except ToolError as e:
                log.info("could not frame the %s view: %s", view, e)
        if framed:
            out["view_framed"] = framed

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
        await _follow(session, progress, out, ("3D", "UV"))
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
        await _follow(session, progress, out)
        return out

    EdgeCouple = Annotated[list[int], Field(min_length=2, max_length=2)]
    EDGES_FIELD = Field(description=(
        "The edges, as couples [polygon, side]: [[12, 0], [12, 1], [40, 3]]. Polygon IDs are the faces in the "
        "order of the file (in an OBJ, the n-th 'f' line, from 0); side i joins the polygon's vertices i and i + 1, "
        "in the order the face lists them (the last side closes back to vertex 0). Either polygon of an edge "
        "names it. These names stay valid after cuts and welds."), min_length=1)

    def _edge_ids(edges):
        flat = []
        for e in edges:
            if not isinstance(e, (list, tuple)) or len(e) != 2:
                raise ToolError("each edge is a couple [polygon, side], got %r" % (e,))
            flat += [int(e[0]), int(e[1])]
        return flat

    async def _seam_edit(command, edges, ctx):
        session, progress = session_of(ctx), _reporter(ctx)
        before = await session.scene(progress)
        if not before.get("has_mesh"):
            raise ToolError("No mesh is loaded: call load first.")
        t0 = time.perf_counter()
        await session.execute(command, {"PrimType": "Edge", "IDs": _edge_ids(edges), "EdgesAsPolyEdgeIDs": True},
                              label=command, progress=progress)
        after = await session.scene(progress)
        session.last_pack = None
        return {"edges": len(edges), "seconds": round(time.perf_counter() - t0, 2),
                "islands_before": before.get("islands"), "islands_after": after.get("islands"), "scene": after,
                "note": "The UVs are not unfolded again: call unfold for the islands to lie flat."}

    @mcp.tool(title="Cut seams", description=(
        "Cut UV seams along the given edges, named by couples [polygon, side]. An edge already cut, or on the "
        "border of the mesh, is left as it is. Only the topology changes: call unfold afterwards. Symmetric "
        "edges are cut too when RizomUV's symmetry is on. One call = one undo step. Fails, naming the couple, "
        "if a couple names no polygon or no side of it."),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True,
                                    open_world_hint=False))
    @_guard
    async def cut(edges: Annotated[list[EdgeCouple], EDGES_FIELD], ctx: Context = None) -> dict[str, Any]:
        return await _seam_edit("Cut", edges, ctx)

    @mcp.tool(title="Weld seams", description=(
        "Weld (sew back) UV seams along the given edges, named by couples [polygon, side]: the two sides of each "
        "cut edge are joined again. An edge that is not cut is left as it is. Only the topology changes: call "
        "unfold afterwards. One call = one undo step. Fails, naming the couple, if a couple names no polygon "
        "or no side of it."),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True,
                                    open_world_hint=False))
    @_guard
    async def weld(edges: Annotated[list[EdgeCouple], EDGES_FIELD], ctx: Context = None) -> dict[str, Any]:
        return await _seam_edit("Weld", edges, ctx)

    @mcp.tool(title="Optimize", description=(
        "Relax the UV islands of the working set without changing their seams: it lowers the distortion of "
        "islands already unfolded (after an unfold, a cut, or on the artist's own UVs) and keeps their place. "
        "working_set is RizomUV's WorkingSet ('Visible' by default, 'Selected' for the selection). extra_params "
        "are merged into RizomUV's Optimize parameters (see command_help('Optimize')): Iterations, "
        "AlignBorders, TriangleFlips, BorderIntersections... One call = one undo step."),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                                    open_world_hint=False))
    @_guard
    async def optimize(
            working_set: Annotated[str, Field(description="RizomUV WorkingSet, e.g. Visible or Selected.")] = "Visible",
            extra_params: Annotated[dict[str, Any] | None, Field(
                description="More Optimize parameters, merged over the defaults.")] = None,
            ctx: Context = None) -> dict[str, Any]:
        session, progress = session_of(ctx), _reporter(ctx)
        params = _deep_merge({"WorkingSet": working_set, "PrimType": "Island"}, extra_params or {})
        t0 = time.perf_counter()
        result = await session.execute("Optimize", params, label="Optimize", progress=progress)
        session.last_pack = None
        out = {"params": params, "seconds": round(time.perf_counter() - t0, 2)}
        if isinstance(result, dict) and result:
            out["rizomuv_result"] = result
        await _follow(session, progress, out)
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
            keep_scale: Annotated[bool, Field(description=(
                "Move and turn the islands without scaling them, so a texel density set before is kept. "
                "They may then not all fit in the tile."))] = False,
            distribute: Annotated[bool | None, Field(description=(
                "Spread the islands over the tiles first, as the Pack button's 'Distribute in tiles' does. "
                "Default: that toggle's setting in RizomUV."))] = None,
            measure: Annotated[bool, Field(description="Measure the result (about 0.5 s).")] = True,
            extra_params: Annotated[dict[str, Any] | None, Field(
                description="More Pack parameters, deep-merged over the canonical ones.")] = None,
            ctx: Context = None) -> dict[str, Any]:
        extra = extra_params or {}
        if keep_scale:
            # no rescale before the layout (Scaling.Mode OFF), none to fill the space (Optimization None),
            # none after it (LayoutScalingMode TRANSLATE_ONLY)
            extra = _deep_merge({"Scaling": {"Mode": 0, "Optimization": "None"}, "LayoutScalingMode": 1}, extra)
        if any(isinstance(v, (int, float)) and not isinstance(v, bool) and v > 1
               for v in _find_key(extra, "MaxMutations")):
            raise ToolError(_MAX_MUTATIONS)
        params = _deep_merge(dict(CANONICAL_PACK, PaddingSizePx=padding_px, MapResolution=map_resolution,
                                  Rotate={"Initial": "Auto", "Min": 0.0, "Max": 360.0, "Step": float(rotation_step)}),
                             extra)
        session, progress = session_of(ctx), _reporter(ctx)
        await session.ensure(progress)
        if distribute is None:
            pref = await session.execute("Get", "Prefs.Layout.Distribute", label="Pack", progress=progress)
            distribute = pref is not False
        t0 = time.perf_counter()
        if distribute:
            # What the Pack button queues first when nothing is selected (CFeaturePacking::ToModelButtons,
            # CreateDistributeParams with DIST_SHUFFLE | DIST_OUT_OF_TILES): the islands outside the tiles
            # are filed into them, without moving. The Pack's AuxGroup never goes without it in the
            # application: alone, it gives the islands left in the root group a share of each tile's free
            # room at their current scale, none when an island already in the tile covers it (an unfold
            # at 3D scale), and they stay outside.
            await session.execute("IslandGroups", {
                "Mode": "DistributeTilesContent", "FreezeIslands": True, "UseTileLocks": True,
                "UseIslandLocks": True, "WorkingSet": "UnLocked", "GroupPaths": ["RootGroup"]},
                label="Pack", progress=progress)
        elif "AuxGroup" not in extra:
            # the button without 'Distribute in tiles': the islands outside the tiles stay where they are
            params.pop("AuxGroup", None)
        await session.execute("Pack", params, label="Pack", progress=progress)
        out = {"params": params, "distributed": bool(distribute), "undo_steps": 2 if distribute else 1,
               "seconds": round(time.perf_counter() - t0, 2)}
        if not distribute:
            out["note"] = ("Not distributed: as with the Pack button when 'Distribute in tiles' is off, the islands "
                           "outside the tiles are left where they are.")
        await _follow(session, progress, out)
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
            await _follow(session, progress, out)
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

    async def _uv_check(session, progress, params):
        report = await session.execute("UvCheck", params, label="Checking the UVs", progress=progress,
                                       timeout_ms=600000)
        if not isinstance(report, dict) or "Verdict" not in report:
            raise ToolError("UvCheck returned no report: %r" % (report,))
        return report

    def _check_summary(check, limit=50):
        out = {"state": check.get("State"), "severity": check.get("Severity"), "count": check.get("Count"),
               "text": check.get("Text")}
        if check.get("Value") is not None:
            out["value"] = check.get("Value")
        for key, name in (("IslandIDs", "island_ids"), ("PolyIDs", "poly_ids"), ("TileKeys", "tiles")):
            ids = check.get(key) or []
            if ids:
                out[name] = _ids(ids, limit)
        return out

    @mcp.tool(title="Validate the UVs", description=(
        "Check whether the UVs are fit for a texture, check by check, as RizomUV's UV Validator does: "
        "overlapping islands, flipped faces, zero-area faces, islands not unfolded, islands crossing a tile "
        "border, padding between islands, margin to the tile border, tile coverage, tiny islands, stretch and "
        "texel density. Returns the verdict (Valid, ValidWithWarnings, NotValid), each check's state with the "
        "islands or polygons it is about, and the statistics. Read only. Pixel thresholds are measured at the "
        "UV set's map resolution unless map_resolution is given. extra_params are UvCheck's other members "
        "(see command_help('UvCheck')): MinCoveragePct, MinIslandPx, MaxStretchPct, TexelDensity (texels per "
        "scene unit), Severity... raster=false skips the overlap, padding and coverage checks (they then read "
        "Stale), for a fast answer on a big mesh."),
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                                    open_world_hint=False))
    @_guard
    async def validate(
            map_resolution: Annotated[int | None, Field(description="Map resolution in pixels.", ge=1)] = None,
            padding_px: Annotated[float | None, Field(
                description="Smallest gap between two islands, in pixels (RizomUV default 16).", ge=0)] = None,
            margin_px: Annotated[float | None, Field(
                description="Smallest gap between an island and its tile border, in pixels (default 8).",
                ge=0)] = None,
            raster: Annotated[bool, Field(description="Run the overlap, padding and coverage checks.")] = True,
            extra_params: Annotated[dict[str, Any] | None, Field(description="More UvCheck members.")] = None,
            ctx: Context = None) -> dict[str, Any]:
        session, progress = session_of(ctx), _reporter(ctx)
        params = {"Raster": raster}
        for key, value in (("MapResolution", map_resolution), ("MinPaddingPx", padding_px),
                           ("MinMarginPx", margin_px)):
            if value is not None:
                params[key] = value
        params = _deep_merge(params, extra_params or {})
        t0 = time.perf_counter()
        report = await _uv_check(session, progress, params)
        checks = report.get("Checks") or {}
        failing = [name for name, c in checks.items() if c.get("State") == "Fail"]
        return {"verdict": report["Verdict"], "errors": report.get("Errors"), "warnings": report.get("Warnings"),
                "passed": report.get("Passed"), "partial": report.get("Partial"), "failing": failing,
                "checks": {name: _check_summary(c) for name, c in checks.items()},
                "stats": {k: v for k, v in (report.get("Stats") or {}).items() if "Histogram" not in k},
                "seconds": round(time.perf_counter() - t0, 2),
                "note": "island_ids and poly_ids can be selected with run_command('Select', ...)."}

    UNIT_METERS = {"km": 1000.0, "m": 1.0, "dm": 0.1, "cm": 0.01, "mm": 0.001, "in": 0.0254, "ft": 0.3048,
                   "yd": 0.9144, "mi": 1609.344}

    async def _density_frame(session, progress):
        """The scene unit, the map resolution and the tile width texel densities are read against."""
        scene_unit = await session.execute("Get", "Lib.Scene.Settings.Unit", label="Reading the scene unit",
                                           progress=progress)
        rez = await session.execute("Get", "Lib.Mesh.RootGroup.Properties.Pack.MapResolution",
                                    label="Reading the map resolution", progress=progress)
        box = await session.execute("Get", "Lib.Mesh.RootGroup.Properties.Box", label="Reading the tile",
                                    progress=progress)
        tile = (box[1] - box[0]) if isinstance(box, list) and len(box) >= 2 and box[1] > box[0] else 1.0
        return str(scene_unit), int(rez or 2048), float(tile)

    def _per_scene_unit(value, unit, scene_unit):
        """Texels per `unit` ("tx/cm") -> texels per scene unit."""
        u = unit[3:] if unit.startswith("tx/") else unit
        if u == scene_unit:
            return value
        if u not in UNIT_METERS or scene_unit not in UNIT_METERS:
            raise ToolError("Cannot convert %s to texels per scene unit: the scene unit is %r. Give the unit as "
                            "'tx/%s', or set the scene unit (run_command Set Lib.Scene.Settings.Unit)."
                            % (unit, scene_unit, scene_unit))
        return value * UNIT_METERS[scene_unit] / UNIT_METERS[u]

    def _in_unit(per_scene, unit, scene_unit):
        try:
            return per_scene / _per_scene_unit(1.0, unit, scene_unit)
        except ToolError:
            return None

    async def _density_report(session, progress, unit, scene_unit, rez, tolerance_pct):
        report = await _uv_check(session, progress, {"Raster": False, "TexelDensityTolerancePct": tolerance_pct})
        stats = report.get("Stats") or {}
        median = stats.get("TexelDensityMedian") or 0.0
        check = (report.get("Checks") or {}).get("TexelDensity") or {}
        off = _ids(check.get("IslandIDs"))
        off["tolerance_pct"] = tolerance_pct
        return {"median": _in_unit(median, unit, scene_unit), "unit": unit,
                "median_per_scene_unit": median, "scene_unit": scene_unit, "map_resolution": rez,
                "islands_off_median": off}

    @mcp.tool(title="Texel density", description=(
        "Read or set the texel density: how many texture pixels fall on a length of the model's surface, at the "
        "UV set's map resolution. action='read' gives the median density (weighted by surface) and the islands "
        "more than tolerance_pct away from it. action='set' scales the islands of the working set so that each "
        "has the density value (per island, so all match), or with uniform=true scales them all by one factor "
        "so their average reaches it (their relative sizes stay). Each island is scaled about its centre: "
        "islands may overlap afterwards, so pack next with keep_scale=true, or the pack rescales them and the "
        "density changes again. unit is 'tx/cm' by default (tx/m, tx/mm, tx/in, tx/ft...). One set = one undo "
        "step."),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                                    open_world_hint=False))
    @_guard
    async def texel_density(
            action: Annotated[Literal["read", "set"], Field(description="read, or set.")] = "read",
            value: Annotated[float | None, Field(description="The density to set, in unit.", gt=0)] = None,
            unit: Annotated[str, Field(description="Texels per length unit, e.g. tx/cm.")] = "tx/cm",
            uniform: Annotated[bool, Field(description="set: one factor for all, relative sizes kept.")] = False,
            working_set: Annotated[str, Field(description="RizomUV WorkingSet for set.")] = "Visible",
            tolerance_pct: Annotated[float, Field(description="read: tolerance around the median.", gt=0)] = 10.0,
            ctx: Context = None) -> dict[str, Any]:
        session, progress = session_of(ctx), _reporter(ctx)
        scene = await session.scene(progress)
        if not scene.get("has_mesh"):
            raise ToolError("No mesh is loaded: call load first.")
        scene_unit, rez, tile = await _density_frame(session, progress)
        out = {}
        if action == "set":
            if value is None:
                raise ToolError("action='set' needs value.")
            target = _per_scene_unit(value, unit, scene_unit)
            # islands brought to 1:1 (one UV unit per scene unit) then scaled by factor: a density of
            # factor * map_resolution / tile_width texels per scene unit
            factor = target * tile / rez
            await session.execute("Deform", {
                "WorkingSet": working_set, "PrimType": "Island", "ResetIslandScale": True,
                "Clustering": "Single" if uniform else "Multi", "Anchoring": "Center",
                "Transform": [factor, 0, 0, 0, factor, 0, 0, 0, 1]}, label="Setting the texel density",
                progress=progress)
            session.last_pack = None
            out["set"] = {"value": value, "unit": unit, "uniform": uniform, "working_set": working_set,
                          "scale_factor": factor}
            out["note"] = "Islands were scaled about their centres: pack(keep_scale=true) before saving."
        out.update(await _density_report(session, progress, unit, scene_unit, rez, tolerance_pct))
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

    def _fit_distance(state, width, height):
        """The orthographic distance (the world height the view shows) that holds a width x height
        box, with FRAME_MARGIN around it."""
        size = state.get("ViewportSize") or {}
        aspect = (size.get("Width") or 1) / max(size.get("Height") or 1, 1)
        return FRAME_MARGIN * max(height, width / aspect, 1e-6)

    def _visible_height(state):
        """The world height the view shows at its target: what a pan of 1 view height moves."""
        distance = float(state.get("Distance") or 0.0)
        if state.get("Perspective"):
            return 2.0 * distance * math.tan(math.radians(float(state.get("FOV") or 45.0)) / 2.0)
        return distance

    def _camera_out(state):
        keep = ("Target", "Distance", "Orientation", "Perspective", "FOV", "ViewportSize")
        return {k: state[k] for k in keep if k in state}

    @mcp.tool(title="Move the view", description=(
        "Move the camera of RizomUV's 3D view, UV view or both, sliding there (animate_ms, 0 jumps), so that "
        "the artist sees what you are talking about. Only on the artist's own RizomUV: a headless one has no "
        "screen. Nothing in the scene changes and nothing goes on the undo stack. "
        "frame: 'all' fits what is visible (the A key), 'selection' the selection (the F key), 'tile' the tile "
        "grid (UV only). tile=[u, v] shows that UV tile (UDIM 1001 is [0, 0]). islands=[ids] fits these islands "
        "in the UV view (the 3D view cannot frame islands: use frame='selection' on a selection). standard_view "
        "puts the 3D camera on an axis. orbit turns the 3D camera around its target, in degrees. zoom > 1 comes "
        "closer (2 = twice as close), < 1 goes back. pan moves along the screen, in view heights (right=0.5 "
        "moves half a view to the right). perspective switches the 3D view's projection. They apply in this "
        "order: the framing (frame, tile or islands), standard_view, orbit, zoom, pan. A view hidden in the "
        "artist's layout is left alone and reported. Returns each view's camera after the move."),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                                    open_world_hint=False))
    @_guard
    async def view(
            view: Annotated[Literal["3D", "UV", "both"] | None, Field(
                description="The view to move. Default: both, or the one the arguments are for (tile and "
                            "islands are UV, standard_view, orbit and perspective are 3D).")] = None,
            frame: Annotated[Literal["all", "selection", "tile"] | None, Field(
                description="all: what is visible; selection: the selection; tile: the tile grid (UV).")] = None,
            tile: Annotated[list[int] | None, Field(
                description="[u, v] of the UV tile to show; [0, 0] is the 0-1 tile (UDIM 1001).",
                min_length=2, max_length=2)] = None,
            islands: Annotated[list[int] | None, Field(
                description="Island ids to fit in the UV view.", min_length=1)] = None,
            standard_view: Annotated[Literal["top", "bottom", "front", "rear", "left", "right"] | None, Field(
                description="Put the 3D camera on this axis view.")] = None,
            orbit: Annotated[dict[Literal["horizontal", "vertical"], float] | None, Field(
                description="Turn the 3D camera around its target, in degrees: {horizontal, vertical}.")] = None,
            zoom: Annotated[float | None, Field(description="> 1 comes closer, < 1 goes back.", gt=0)] = None,
            pan: Annotated[dict[Literal["right", "up"], float] | None, Field(
                description="Move along the screen, in view heights: {right, up}.")] = None,
            perspective: Annotated[bool | None, Field(description="3D: perspective or orthographic.")] = None,
            animate_ms: Annotated[int, Field(description="Time to slide there, in ms; 0 jumps.",
                                             ge=0, le=5000)] = VIEW_ANIMATE_MS,
            ctx: Context = None) -> dict[str, Any]:
        session, progress = session_of(ctx), _reporter(ctx)
        await session.ensure(progress)
        if not _on_screen(session):
            raise ToolError("This RizomUV is headless: it has no viewport to move. The view tool drives the "
                            "artist's own RizomUV; render_layout shows a headless layout.")
        if sum(x is not None for x in (frame, tile, islands)) > 1:
            raise ToolError("frame, tile and islands each choose what to fit: give one of them.")
        uv_only = tile is not None or islands is not None or frame == "tile"
        only_3d = standard_view is not None or orbit is not None or perspective is not None
        if view is None:
            view = "UV" if uv_only and not only_3d else "3D" if only_3d and not uv_only else "both"
        views = ("3D", "UV") if view == "both" else (view,)
        if view == "3D" and uv_only:
            raise ToolError("tile, islands and frame='tile' are for the UV view, not the 3D one.")
        if view == "UV" and only_3d:
            raise ToolError("standard_view, orbit and perspective are for the 3D view; the UV view never turns.")
        NOT_CREATED = ("the %s view is not created yet: RizomUV is still starting (its link opens before its "
                       "viewports); call view again in a moment")

        async def read(name):
            try:
                return await _camera_when_created(session, progress, name)
            except ToolError as e:
                if _not_created(e):
                    return None
                raise

        if not any((frame, tile, islands, standard_view, orbit, zoom, pan, perspective is not None)):
            # a plain read
            states = {name: await read(name) for name in views}
            return {name: _camera_out(st) if st is not None else {"skipped": NOT_CREATED % name}
                    for name, st in states.items()}

        box = None
        out = {}
        if islands is not None:
            r = await session.op("uv_box", {"islands": islands, "attached": session.attached},
                                 label="Island bounds", progress=progress)
            if r.get("missing"):
                out["missing_islands"] = r["missing"][:50]
            if r.get("box") is None:
                raise ToolError("None of these ids is an island of the scene (it has %s islands)."
                                % (await session.scene(progress)).get("islands"))
            box = r["box"]
        elif tile is not None:
            box = [float(tile[0]), float(tile[1]), float(tile[0]) + 1.0, float(tile[1]) + 1.0]

        for name in views:
            state = await read(name)
            if state is None:
                out[name] = {"skipped": NOT_CREATED % name}
                continue
            if not _shown(state):
                out[name] = {"skipped": "the %s view is not on screen in the artist's layout" % name}
                continue
            params = {"Animate": animate_ms}
            if name == "UV" and box is not None:
                params["Target"] = [(box[0] + box[2]) / 2, (box[1] + box[3]) / 2, 0.0]
                params["Distance"] = _fit_distance(state, box[2] - box[0], box[3] - box[1])
            elif frame == "tile" and name == "3D":
                pass   # the 3D view has no tile grid: with view=both only the UV view frames it
            elif frame:
                params["Frame"] = frame.capitalize()
            if name == "3D":
                if standard_view:
                    params["StandardView"] = standard_view.capitalize()
                if orbit:
                    params["Orbit"] = {"Horizontal": float(orbit.get("horizontal", 0.0)),
                                       "Vertical": float(orbit.get("vertical", 0.0))}
                if perspective is not None:
                    params["Perspective"] = bool(perspective)
            if zoom:
                params["Zoom"] = float(zoom)
            if len(params) > 1:
                state = await _camera(session, progress, name, params)
            if pan:
                # in view heights of the camera just reached, which the previous call returned
                h = _visible_height(state)
                state = await _camera(session, progress, name, {
                    "Pan": {"Right": float(pan.get("right", 0.0)) * h, "Up": float(pan.get("up", 0.0)) * h},
                    "Animate": animate_ms})
            out[name] = _camera_out(state)
        return out

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
        if action == "set_current":
            await _follow(session, progress, out)
        return out

    @mcp.tool(title="Transfer a texture", description=(
        "Bake a texture from one UV set onto another UV set of the same mesh: the image painted on "
        "source_uv_set is resampled onto the layout of dest_uv_set and written to output_file (format from the "
        "extension: .png, .tga, .jpg, .bmp, .tif). Use it after re-unwrapping a mesh that already has textures. "
        "source_uv_set defaults to the current UV set and source_file to the texture already assigned to that "
        "set; dest_uv_set must be another set (make one with uv_sets action='duplicate' before "
        "changing its UVs). Set normal_map=true for a tangent-space normal map. output_file must be "
        "absolute and new unless overwrite=true. The scene is not changed, unless show=true: then the result "
        "becomes the texture of dest_uv_set, that set is made current and the viewports show it textured, as "
        "the panel's 'Show it after Transfer' does (the source set keeps its own texture, so making it current "
        "again shows the original). extra_params are merged into "
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
            show: Annotated[bool, Field(description=(
                "Show the result in RizomUV: it becomes the texture of dest_uv_set, which is made current, "
                "and the viewports show it textured."))] = False,
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
        if not show:
            out["note"] = ("The image is written; the scene is unchanged. In RizomUV it becomes visible once loaded "
                           "as the texture of %s (show=true does it)." % dest_uv_set)
            return out
        # What the panel does with its result (CFeatureTextureTransfer::AssignResult, ShowSet). A source set
        # without a texture of its own gets the file it was read from, so that making it current again shows
        # the original rather than the result. The set made current loads its texture as the user texture.
        shown = out["show"] = {"texture": str(out_path) if "Maps" not in params else None}
        current = await session.execute("Get", "Lib.CurrentUVSetName", label="UV sets", progress=progress)

        def texture_path(name):
            # the current set's texture lives on the mesh, the others' on their own UV set
            return ("Lib.Mesh.RootGroup.Properties.Texture" if name == current
                    else "Lib.UVSets.%s.RootGroup.Properties.Texture" % name)
        assigned_source = await session.execute("Get", texture_path(source), label="UV sets", progress=progress)
        if not isinstance(assigned_source, str) or not assigned_source.strip():
            await session.execute("Set", {"Path": texture_path(source), "Value": src_path.as_posix()},
                                  label="Show the result", progress=progress)
            shown["source_texture_assigned"] = str(src_path)
        if current != dest_uv_set:
            await session.execute("Uvset", {"Mode": "SetCurrent", "Name": dest_uv_set}, label="Show the result",
                                  progress=progress)
            session.last_pack = None
        if shown["texture"]:
            await session.execute("Set", {"Path": "Lib.Mesh.RootGroup.Properties.Texture",
                                          "Value": out_path.as_posix()}, label="Show the result", progress=progress)
        for path, value in (("Vars.Viewport.Texture", "User"), ("Vars.Viewport.Viewport3D.Textured", True),
                            ("Vars.Viewport.ViewportUV.Textured", True)):
            await session.execute("Set", {"Path": path, "Value": value}, label="Show the result", progress=progress)
        shown["current_uv_set"] = dest_uv_set
        out["note"] = ("The result is the texture of %s, now the current UV set, and the viewports show it; "
                       "uv_sets set_current %s shows the original." % (dest_uv_set, source))
        await _follow(session, progress, out, ("3D", "UV"))
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
        await _follow(session, progress, out)
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
