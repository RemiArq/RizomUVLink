"""Turn a UV snapshot (and the scene's 3D side) into findings an assistant can act on.

The point is to answer "what is wrong with this layout and which lever fixes it", so that
nobody iterates on pack parameters to chase tenths of a percent. Every threshold below was
calibrated on a 2,573-island hard-surface scene (Mech8) measured through RizomUV itself;
the numbers quoted in the messages come from those runs.

numpy in, plain Python out: `diagnose` returns a dict that survives `json.dumps`.
"""

from __future__ import annotations

import time

import numpy as np

from . import metrics

# An island whose 3D area is below this share of the scene's has no usable 3D size: RizomUV
# clamps its stretch to 1 (Poly::S at area3D <= FLT_MIN) and Pack leaves it at its 3D XY,
# far outside the tile on Mech8 (islands 1183 and 1184, v ~ 9). The OBJ that carries the 3D
# side is written with 6 significant digits, so "zero" has to tolerate that rounding.
DEGENERATE_REL_AREA = 1e-9

PADDING_SHARE_LIMIT = 0.10
MEAN_ISLAND_PX_LIMIT = 16.0
TINY_SHARE_MEDIUM = 0.20
TEXEL_SPREAD_LIMIT = 1.10
STRETCH_BAND = (0.8, 1.25)
STRETCH_SHARE_LIMIT = 0.02
# Below this many pixels of flipped area at the map resolution a flip is invisible: Mech8 as
# loaded reports NegativeUVArea = -1.4e-6, about 1.4 px at 1024.
FLIP_VISIBLE_PX = 16.0

_ORDER = {"high": 0, "medium": 1, "info": 2}


def _finding(severity, code, message):
    return {"severity": severity, "code": code, "message": message}


def _ids(ids, limit=10):
    ids = [int(i) for i in ids]
    text = ", ".join(str(i) for i in ids[:limit])
    return text + (", ..." if len(ids) > limit else "")


def _pct(share):
    """A share as a short percentage: 14.87 %, 3.2 %, 0.05 %."""
    x = 100.0 * share
    if x >= 10:
        return f"{x:.0f} %"
    if x >= 1:
        return f"{x:.1f} %"
    return f"{x:.2g} %"


def _sig(x, digits=4):
    return float(f"{x:.{digits}g}")


def _areas_3d(mesh3d, snap, notes):
    """Per-polygon 3D area, or None when the 3D mesh does not match the snapshot."""
    positions, face_sizes, face_pos_ids = mesh3d
    positions = np.asarray(positions, np.float64).reshape(-1, 3)
    face_sizes = np.asarray(face_sizes, np.int64)
    if not np.array_equal(face_sizes, snap.poly_sizes):
        notes.append(f"The 3D mesh ({face_sizes.size} faces) does not match the UV snapshot "
                     f"({snap.poly_sizes.size} polygons) face for face, so degenerate islands, texel "
                     "density and stretch were not computed.")
        return None
    tris, tri_poly = metrics.fan(face_sizes, face_pos_ids)
    p = positions[tris]
    area = 0.5 * np.linalg.norm(np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]), axis=1)
    return np.bincount(tri_poly, weights=area, minlength=face_sizes.size)


def diagnose(snap: metrics.UVSnapshot, res: int, padding_px: float | None = None, mesh3d=None,
             negative_uv_area: float | None = None, notes: list[str] | None = None) -> dict:
    """Findings (most severe first), the measure dict and the 3D-based statistics.

    `mesh3d` is `objmesh.read_obj`'s (positions, face_sizes, face_pos_ids) for the same
    scene, polygon order equal to the snapshot's; without it degenerate islands, texel
    density and stretch are unknown. `negative_uv_area` is RizomUV's
    `Lib.Mesh.Quality.NegativeUVArea` (<= 0). `notes` are carried into the result, for
    context the caller knows (why the 3D side is missing, for instance).
    """
    t0 = time.perf_counter()
    res = int(res)
    notes = list(notes or [])
    pisl = snap.poly_island_ids
    present = np.unique(pisl)
    n_ids = int(present.max()) + 1 if present.size else 0

    area3_poly = _areas_3d(mesh3d, snap, notes) if mesh3d is not None else None
    if mesh3d is None:
        notes.append("Computed without the scene's 3D mesh: degenerate islands, texel density and "
                     "stretch are unknown.")

    degenerate = np.zeros(0, np.int64)
    isl3d = None
    if area3_poly is not None:
        isl3d = np.bincount(pisl, weights=area3_poly, minlength=n_ids)
        total3d = float(isl3d[present].sum())
        if total3d > 0:
            degenerate = present[isl3d[present] <= DEGENERATE_REL_AREA * total3d]
        else:
            notes.append("The 3D mesh has no area at all; texel density and stretch were not computed.")
            area3_poly = isl3d = None

    m = metrics.measure(snap, res, padding_px, exclude_islands=degenerate)

    # UV area per polygon and per island, from the same fan triangulation as the metrics.
    tris, tri_poly = metrics.fan(snap.poly_sizes, snap.poly_uvw_ids)
    p = snap.uv[tris]
    e1, e2 = p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]
    tri_uv = 0.5 * np.abs(e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0])
    uv_poly = np.bincount(tri_poly, weights=tri_uv, minlength=snap.poly_sizes.size)
    isluv = np.bincount(pisl, weights=uv_poly, minlength=n_ids)
    analysed = present[~np.isin(present, degenerate)]

    texel = stretch = None
    if isl3d is not None and analysed.size:
        texel, stretch = _texel_and_stretch(snap, res, analysed, degenerate, isluv, isl3d, uv_poly,
                                            area3_poly)

    findings = []
    ov = m["overlap"]
    if ov["pixels"]:
        pairs = ", ".join(f"{a}–{b}" for a, b in ov["example_pairs"][:5])
        more = "at least " if ov.get("island_pairs_is_lower_bound") else ""
        findings.append(_finding(
            "high", "overlap",
            f"Islands overlap: {ov['pixels']} px at {res} px are covered by two different islands "
            f"({more}{ov['island_pairs']} island pairs, {ov['islands']} islands; e.g. {pairs}). "
            "Overlapping islands share texels. pack separates them; if they overlap after a pack, they "
            "were outside its working set (hidden or locked) or padding_px was 0."))

    collapsed = analysed[isluv[analysed] <= 1e-15]
    if collapsed.size:
        findings.append(_finding(
            "high", "collapsed_uv_islands",
            f"{collapsed.size} islands have no UV area at all (every UV on a point or a line; ids "
            f"{_ids(collapsed)}): they get no texels. Unfold them."))

    out = m["outside_tile"]
    if out["islands"]:
        findings.append(_finding(
            "high", "outside_tile",
            f"{out['islands']} islands extend beyond the 0–1 tile (ids {_ids(out['example_ids'])}). "
            "Their texels land in another tile or wrap around, unless the layout is meant for UDIM "
            "tiles; pack moves them back in."))

    if negative_uv_area is not None and negative_uv_area < 0:
        flipped_px = -negative_uv_area * res * res
        if flipped_px >= FLIP_VISIBLE_PX:
            findings.append(_finding(
                "high", "flipped_uvs",
                f"RizomUV reports flipped UVs (Quality.NegativeUVArea = {negative_uv_area:.3g}, about "
                f"{flipped_px:.0f} px at {res} px): those polygons show the texture mirrored or fold "
                "over their neighbours. Unfold the islands concerned again, or cut where they fold."))
        else:
            notes.append(f"RizomUV reports a negligible flipped UV area ({negative_uv_area:.2g}, about "
                         f"{flipped_px:.1f} px at {res} px); not a finding.")

    if degenerate.size:
        where = ""
        if not _inside_tile(snap, degenerate):
            where = " — here outside the tile"
        findings.append(_finding(
            "medium", "degenerate_islands",
            f"{degenerate.size} degenerate islands (zero 3D area, ids {_ids(degenerate)}) are left "
            f"out of every statistic here. Pack cannot size them and leaves them where they are{where}. "
            "Delete or repair them in the modelling tool."))

    findings.extend(_resolution_findings(m, res, padding_px))

    tiny = m["tiny_islands"]
    if tiny["under_4px"]:
        share = tiny["under_4px"] / max(analysed.size, 1)
        findings.append(_finding(
            "medium" if share >= TINY_SHARE_MEDIUM else "info", "tiny_islands",
            f"{tiny['under_4px']} islands ({_pct(share)}) are under 4 px across at {res} px, "
            f"{tiny['slivers_under_2px']} of them slivers under 2 px thick: they get almost no texels "
            "and bleed into their padding. A higher map_resolution helps; so does welding tiny islands "
            "onto a neighbour where the seam is not needed."))

    if texel is not None:
        spread = texel["ratio_p95"] / texel["ratio_p5"] if texel["ratio_p5"] > 0 else float("inf")
        off = texel["islands_off_25pct"]
        examples = _ids(texel["example_ids_off_25pct"])
        if spread > TEXEL_SPREAD_LIMIT:
            findings.append(_finding(
                "medium", "texel_density_uneven",
                f"Texel density is uneven: the middle 90 % of islands span ×{spread:.2f} around the "
                f"average of {texel['px_per_unit']} px per 3D unit, and {off} islands are more than 25 % "
                f"off" + (f" (e.g. ids {examples})" if off else "") + ". Unless those islands were "
                "scaled on purpose, they will look blurrier or sharper than their neighbours."))
        elif off:
            findings.append(_finding(
                "info", "texel_density_uneven",
                f"{off} islands have a texel density more than 25 % away from the average (ids "
                f"{examples}); fine if they were scaled on purpose."))

    if stretch is not None and stretch["area_share_outside_0.8_1.25"] > STRETCH_SHARE_LIMIT:
        findings.append(_finding(
            "medium", "stretch",
            f"{_pct(stretch['area_share_outside_0.8_1.25'])} of the 3D surface is stretched or "
            "compressed by more than 25 % relative to its island's average (per polygon, p1–p99 "
            f"{stretch['poly_ratio_p1']}–{stretch['poly_ratio_p99']}). Unfold or Optimize those islands "
            "again, or add cuts where the distortion concentrates; render_layout mode='stretch' shows "
            "where."))

    if not any(f["severity"] in ("high", "medium") for f in findings):
        checked = ["no overlap between islands", "nothing outside the tile"]
        if texel is not None:
            checked.append("even texel density")
        if stretch is not None:
            checked.append("no significant stretch")
        findings.append(_finding(
            "info", "clean",
            f"No defect found at {res} px: {', '.join(checked)} (coverage {m['coverage']})."))

    findings.sort(key=lambda f: _ORDER[f["severity"]])
    return {
        "findings": findings,
        "metrics": m,
        "degenerate_islands": (None if isl3d is None
                               else {"count": int(degenerate.size),
                                     "ids": [int(i) for i in degenerate[:50]]}),
        "texel_density": texel,
        "stretch": stretch,
        "flipped_uv_area": None if negative_uv_area is None else float(negative_uv_area),
        "notes": notes,
        "seconds": round(time.perf_counter() - t0, 2),
    }


def _inside_tile(snap, islands):
    corner_island = np.repeat(snap.poly_island_ids, snap.poly_sizes)
    uv = snap.uv[snap.poly_uvw_ids[np.isin(corner_island, islands)]]
    eps = metrics.OUTSIDE_TILE_EPS
    return bool(uv.size == 0 or ((uv >= -eps) & (uv <= 1 + eps)).all())


def _resolution_findings(m, res, padding_px):
    """The finding that stops parameter chasing: when the map is too coarse for the islands."""
    mean = m["island_size_px"]["mean"]
    share = m["padding_share_estimate"]
    if mean is None:
        return []
    if padding_px is None:
        if mean >= MEAN_ISLAND_PX_LIMIT:
            return []
        return [_finding(
            "medium", "resolution_too_low",
            f"Islands average {mean} px across on the {res} px map (under {MEAN_ISLAND_PX_LIMIT:.0f} px). "
            "Pass padding_px (the padding the layout was packed with) to see how much of the map the "
            "padding takes; with many small islands, map_resolution is the lever, not rotation steps "
            "or MaxMutations.")]
    if share is not None and share > PADDING_SHARE_LIMIT:
        gain = 100.0 * share / 2.0
        gain_text = f"{gain:.0f}" if gain >= 10 else f"{gain:.1f}"
        return [_finding(
            "medium", "resolution_too_low",
            f"Padding takes about {_pct(share)} of the {res} px map (islands average {mean} px across). "
            "Doubling map_resolution at the same padding in pixels should recover at least "
            f"{gain_text} points of coverage (measured: a 2,573-island scene went from 0.62 to 0.75 at "
            "1024 → 2048, 2 px). Rotation steps below 90° and MaxMutations will not help here.")]
    if mean < MEAN_ISLAND_PX_LIMIT:
        return [_finding(
            "medium", "resolution_too_low",
            f"Islands average {mean} px across on the {res} px map (padding takes about "
            f"{_pct(share or 0.0)}): most islands get only a few texels. Pack at the resolution the "
            "texture will be baked at; rotation steps below 90° and MaxMutations will not help here.")]
    return []


def _texel_and_stretch(snap, res, analysed, degenerate, isluv, isl3d, uv_poly, area3_poly):
    has3d = analysed[isl3d[analysed] > 0]
    if not has3d.size:
        return None, None
    s_glob = float(np.sqrt(isluv[has3d].sum() / isl3d[has3d].sum()))
    s_isl = np.zeros(isl3d.size, np.float64)
    s_isl[has3d] = np.sqrt(isluv[has3d] / isl3d[has3d])
    ratio = s_isl[has3d] / s_glob
    with np.errstate(divide="ignore"):
        dev = np.abs(np.log(ratio))
    p5, p95 = np.percentile(ratio, [5, 95])
    off25 = dev > np.log(1.25)
    worst = has3d[off25][np.argsort(-dev[off25], kind="stable")]
    texel = {
        "global": _sig(s_glob),
        "px_per_unit": round(s_glob * res, 1),
        "ratio_p5": round(float(p5), 4),
        "ratio_p95": round(float(p95), 4),
        "islands_off_10pct": int(np.count_nonzero(dev > np.log(1.1))),
        "islands_off_25pct": int(np.count_nonzero(off25)),
        "example_ids_off_25pct": [int(i) for i in worst[:10]],
    }

    pisl = snap.poly_island_ids
    good = (area3_poly > 0) & (s_isl[pisl] > 0) & ~np.isin(pisl, degenerate)
    if not good.any():
        return texel, None
    rel = np.sqrt(uv_poly[good] / area3_poly[good]) / s_isl[pisl[good]]
    w = area3_poly[good] / area3_poly[good].sum()
    lo, hi = STRETCH_BAND
    p1, p99 = np.percentile(rel, [1, 99])
    stretch = {
        "poly_ratio_p1": round(float(p1), 3),
        "poly_ratio_p99": round(float(p99), 3),
        "area_share_outside_0.8_1.25": round(float(w[(rel < lo) | (rel > hi)].sum()), 4),
    }
    return texel, stretch
