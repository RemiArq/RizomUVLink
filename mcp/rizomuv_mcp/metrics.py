"""UV layout metrics recomputed from the raw vectors of a RizomUV scene.

The numbers are deliberately independent of RizomUV's own reporting: `Lib.Mesh.AreaUV`
exceeds 1 as soon as islands stack, and the mesh-level nodes (`BBoxUV`, `SMax`, ...) are
aggregated over the current working set and polluted by zero-3D-area islands. Everything
here starts from `Save({"Data": True})` + `Save({"IndexTable": {"PolygonIDsToIslandIDs": True}})`.

`rasterize_fast` reproduces the reference oracle `RizomUVApp/tests/rizomtest/metrics.py`
(`rasterize`) bit for bit: same pixel-centre arrays, same `searchsorted` bounds, same
edge-function expressions evaluated in float64 with the same operand order and the same
strict/loose inequalities. Only the loop changed - every (triangle, pixel) candidate is
expanded with `np.repeat` and counted with `np.bincount` - so the two cannot disagree on
a tie. Keep it that way when editing: `tests/test_metrics.py` asserts the equality.

numpy in, plain Python out: every dict returned by `measure` survives `json.dumps`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

# Pixels processed per row band of the raster. Bounds the per-band count grid (and the
# bincount scratch next to it) to a few tens of MB whatever map_resolution is asked for.
BAND_PIXELS = 1 << 22

# Coverage converges long before the pixel grid gets this fine, and an 8k or 16k raster of
# a 2.5k-island scene costs minutes; above this the raster runs at this resolution and
# says so (`raster_resolution`). The island-size statistics are analytic and always use the
# requested map resolution.
MAX_RASTER_RES = 4096

# Pair emission budget of the overlap attribution: a stack of k islands on one pixel yields
# k(k-1)/2 pairs, so a fully stacked layout would otherwise be quadratic. Past the budget
# the pair list is truncated (flagged), the island list and the pixel counts stay exact.
PAIR_BUDGET = 8_000_000

OUTSIDE_TILE_EPS = 1e-6


# -- topology ---------------------------------------------------------------------------


def fan(poly_sizes, poly_uvw_ids):
    """Fan-triangulate every polygon from its first corner.

    Returns (tris (T, 3) int64 vertex ids, tri_poly (T,) int64 source polygon), in the
    reference oracle's order. Polygons with fewer than 3 corners yield no triangle.
    """
    sizes = np.asarray(poly_sizes, np.int64)
    ids = np.asarray(poly_uvw_ids, np.int64)
    count = sizes.size
    offsets = np.zeros(count, np.int64)
    if count:
        offsets[1:] = np.cumsum(sizes)[:-1]
    ntri = np.maximum(sizes - 2, 0)
    tri_poly = np.repeat(np.arange(count, dtype=np.int64), ntri)
    starts = np.cumsum(ntri) - ntri
    local = np.arange(tri_poly.size, dtype=np.int64) - np.repeat(starts, ntri) + 1
    base = offsets[tri_poly]
    tris = np.stack([ids[base], ids[base + local], ids[base + local + 1]], axis=1)
    return tris.reshape(-1, 3), tri_poly


def border_edges(poly_sizes, poly_uvw_ids):
    """Undirected UV edges used by exactly one polygon: island borders, seams included.

    Returns (a, b, poly): the two vertex ids of each border edge and the polygon owning it.
    """
    sizes = np.asarray(poly_sizes, np.int64)
    ids = np.asarray(poly_uvw_ids, np.int64)
    if ids.size == 0:
        empty = np.zeros(0, np.int64)
        return empty, empty, empty
    offsets = np.zeros(sizes.size, np.int64)
    offsets[1:] = np.cumsum(sizes)[:-1]
    poly_of = np.repeat(np.arange(sizes.size, dtype=np.int64), sizes)
    pos = np.arange(ids.size, dtype=np.int64) - offsets[poly_of]
    nxt = offsets[poly_of] + (pos + 1) % sizes[poly_of]
    a, b = ids, ids[nxt]
    lo, hi = np.minimum(a, b), np.maximum(a, b)
    key = lo * (int(ids.max()) + 1) + hi
    _, inv, cnt = np.unique(key, return_inverse=True, return_counts=True)
    border = cnt[inv] == 1
    return a[border], b[border], poly_of[border]


def uv_islands(poly_sizes, poly_uvw_ids):
    """Island id per polygon from the UV topology alone (connected components).

    For snapshots taken without an IndexTable, or UVs read from a file. Ids are 0..k-1 in
    order of each island's lowest vertex id, which is NOT RizomUV's numbering; the
    partition is the same.
    """
    sizes = np.asarray(poly_sizes, np.int64)
    ids = np.asarray(poly_uvw_ids, np.int64)
    if sizes.size == 0:
        return np.zeros(0, np.int64)
    offsets = np.zeros(sizes.size, np.int64)
    offsets[1:] = np.cumsum(sizes)[:-1]
    # Star edges from each polygon's first corner connect it as well as its ring does.
    a = np.repeat(ids[offsets], sizes)
    b = ids
    labels = np.arange(int(ids.max()) + 1, dtype=np.int64)
    # Hook roots onto the smaller root, then pointer-jump to full compression; the forest
    # only ever points downwards, so this terminates, in O(log n) rounds on meshes.
    while True:
        la, lb = labels[a], labels[b]
        if np.array_equal(la, lb):
            break
        low = np.minimum(la, lb)
        np.minimum.at(labels, la, low)
        np.minimum.at(labels, lb, low)
        while True:
            jumped = labels[labels]
            if np.array_equal(jumped, labels):
                break
            labels = jumped
    root = labels[ids[offsets]]
    _, island = np.unique(root, return_inverse=True)
    return island.astype(np.int64)


# -- raster -----------------------------------------------------------------------------


def rasterize_fast(uv, tris, res=1024, box=(0.0, 0.0, 1.0, 1.0), chunk_pairs=2_000_000,
                   tri_island=None, return_counts=False):
    """Count triangle hits per pixel centre of `box` (uMin, vMin, uMax, vMax) at res x res.

    Returns {coverage, overlap_ratio, filled_px, overlap_px} with the reference oracle's
    exact values. With `tri_island` ((T,) island per triangle) also returns
    inter_island_overlap_px (pixels covered by two different islands - the only overlap
    that is a layout defect), self_overlap_px (an island covering a pixel twice: concave
    polygons under fan triangulation, pixel-centre ties, or a fold), overlap_pairs,
    overlap_pairs_complete and islands_in_overlap. `return_counts` adds the (res, res)
    grid, row j = v index from the bottom, column i = u index.
    """
    uv = np.asarray(uv, np.float64)
    tris = np.asarray(tris, np.int64).reshape(-1, 3)
    u_min, v_min, u_max, v_max = box
    scale_u = res / (u_max - u_min)
    scale_v = res / (v_max - v_min)
    us = u_min + (np.arange(res) + 0.5) / scale_u
    vs = v_min + (np.arange(res) + 0.5) / scale_v

    pts = uv[tris]
    ax, ay = pts[:, 0, 0], pts[:, 0, 1]
    bx, by = pts[:, 1, 0], pts[:, 1, 1]
    cx, cy = pts[:, 2, 0], pts[:, 2, 1]
    lo_u = np.maximum(np.minimum(np.minimum(ax, bx), cx), u_min)
    hi_u = np.minimum(np.maximum(np.maximum(ax, bx), cx), u_max)
    lo_v = np.maximum(np.minimum(np.minimum(ay, by), cy), v_min)
    hi_v = np.minimum(np.maximum(np.maximum(ay, by), cy), v_max)
    keep = (lo_u < hi_u) & (lo_v < hi_v)
    i0 = np.searchsorted(us, lo_u, "left")
    i1 = np.searchsorted(us, hi_u, "right")
    j0 = np.searchsorted(vs, lo_v, "left")
    j1 = np.searchsorted(vs, hi_v, "right")
    w = np.where(keep, np.maximum(i1 - i0, 0), 0)
    h = np.where(keep, np.maximum(j1 - j0, 0), 0)
    live = np.nonzero(w * h)[0]

    track = tri_island is not None
    nisl = 1
    if track:
        tri_island = np.asarray(tri_island, np.int64)
        nisl = int(tri_island.max()) + 1 if tri_island.size else 1
    full = np.zeros(res * res, np.int64) if return_counts else None
    filled = over = inter_px = self_px = 0
    inter_keys = []
    band_rows = max(1, BAND_PIXELS // res)

    for b0 in range(0, res, band_rows):
        b1 = min(res, b0 + band_rows)
        t_band = live[(j0[live] < b1) & (j1[live] > b0)]
        if t_band.size == 0:
            continue
        jb0 = np.maximum(j0[t_band], b0)
        hb = np.minimum(j1[t_band], b1) - jb0
        wb = w[t_band]
        n = wb * hb
        cum = np.cumsum(n)
        band_px = (b1 - b0) * res
        counts = np.zeros(band_px, np.int64)
        if track:
            lowest = np.full(band_px, np.iinfo(np.int64).max, np.int64)
            highest = np.full(band_px, -1, np.int64)
            hits = []
        start = 0
        while start < t_band.size:
            before = cum[start - 1] if start > 0 else 0
            end = max(int(np.searchsorted(cum, before + chunk_pairs, "right")), start + 1)
            nt = n[start:end]
            rep = np.repeat(np.arange(start, end), nt)
            k = np.arange(rep.size, dtype=np.int64) - np.repeat(np.cumsum(nt) - nt, nt)
            ti = t_band[rep]
            wr = wb[rep]
            ii = i0[ti] + k % wr
            jj = jb0[rep] + k // wr
            gu = us[ii]
            gv = vs[jj]
            Ax, Ay, Bx, By, Cx, Cy = ax[ti], ay[ti], bx[ti], by[ti], cx[ti], cy[ti]
            w0 = (Bx - Ax) * (gv - Ay) - (By - Ay) * (gu - Ax)
            w1 = (Cx - Bx) * (gv - By) - (Cy - By) * (gu - Bx)
            w2 = (Ax - Cx) * (gv - Cy) - (Ay - Cy) * (gu - Cx)
            inside = ((w0 >= 0) & (w1 >= 0) & (w2 > 0)) | ((w0 <= 0) & (w1 <= 0) & (w2 < 0))
            flat = (jj[inside] - b0) * res + ii[inside]
            counts += np.bincount(flat, minlength=band_px)
            if track:
                isl = tri_island[ti[inside]]
                np.minimum.at(lowest, flat, isl)
                np.maximum.at(highest, flat, isl)
                hits.append((flat, isl))
            start = end

        filled += int(np.count_nonzero(counts))
        over += int(np.count_nonzero(counts > 1))
        if full is not None:
            full[b0 * res:b1 * res] = counts
        if track:
            inter = highest > lowest
            n_inter = int(np.count_nonzero(inter))
            inter_px += n_inter
            # A pixel covered by one island has one distinct island; only the (rare)
            # multi-island pixels need their exact distinct count.
            distinct = np.ones(band_px, np.int64)
            if n_inter:
                flat = np.concatenate([f for f, _ in hits])
                isl = np.concatenate([i for _, i in hits])
                m = inter[flat]
                keys = np.unique(flat[m] * nisl + isl[m])  # one per (pixel, island)
                distinct[inter] = np.bincount(keys // nisl, minlength=band_px)[inter]
                inter_keys.append(keys + b0 * res * nisl)
            self_px += int(np.count_nonzero(counts > distinct))

    out = {"coverage": filled / float(res * res),
           "overlap_ratio": (over / filled) if filled else 0.0,
           "filled_px": filled, "overlap_px": over}
    if full is not None:
        out["counts"] = full.reshape(res, res)
    if track:
        out["inter_island_overlap_px"] = inter_px
        out["self_overlap_px"] = self_px
        pairs, complete, islands = _overlap_pairs(inter_keys, nisl)
        out["overlap_pairs"] = pairs
        out["overlap_pairs_complete"] = complete
        out["islands_in_overlap"] = islands
    return out


def _overlap_pairs(inter_keys, nisl):
    """Island pairs sharing at least one pixel, from sorted (pixel, island) keys."""
    if not inter_keys:
        return [], True, []
    keys = np.concatenate(inter_keys)
    pix, isl = keys // nisl, keys % nisl
    islands = [int(i) for i in np.unique(isl)]
    # keys are sorted, so each pixel's islands are contiguous and ascending
    bounds = np.flatnonzero(np.diff(pix)) + 1
    starts = np.r_[0, bounds]
    sizes = np.diff(np.r_[starts, pix.size])
    emitted = sizes * (sizes - 1) // 2
    complete = True
    if int(emitted.sum()) > PAIR_BUDGET:
        # Keep whole pixels, in order, up to the budget: the list stays a true subset. A
        # single pixel over budget on its own keeps its first k islands instead.
        upto = int(np.searchsorted(np.cumsum(emitted), PAIR_BUDGET, "right"))
        if upto:
            starts, sizes = starts[:upto], sizes[:upto]
        else:
            k = max(2, int((1 + np.sqrt(1 + 8 * PAIR_BUDGET)) // 2))
            starts, sizes = starts[:1], np.minimum(sizes[:1], k)
        complete = False
    pair_keys = []
    for g in np.unique(sizes[sizes > 1]):
        grp = starts[sizes == g]
        ia, ib = np.triu_indices(int(g), 1)
        a = isl[grp[:, None] + ia[None, :]].ravel()
        b = isl[grp[:, None] + ib[None, :]].ravel()
        pair_keys.append(np.unique(a * nisl + b))
    if not pair_keys:
        return [], complete, islands
    uniq = np.unique(np.concatenate(pair_keys))
    pairs = [(int(p // nisl), int(p % nisl)) for p in uniq]
    return pairs, complete, islands


# -- per-island statistics --------------------------------------------------------------


def island_stats_fast(uv, tris, tri_poly, poly_island_ids):
    """Per-island UV area and bounding box, for islands owning at least one triangle.

    Returns {islands (n,) ids ascending, area, signed_area, neg_area, bbox (n, 4) as
    (uMin, vMin, uMax, vMax), tri_island (T,), tri_signed_area (T,)} as numpy arrays.
    """
    uv = np.asarray(uv, np.float64)
    tris = np.asarray(tris, np.int64).reshape(-1, 3)
    tri_poly = np.asarray(tri_poly, np.int64)
    tri_island = np.asarray(poly_island_ids, np.int64)[tri_poly]
    pts = uv[tris]
    e1 = pts[:, 1] - pts[:, 0]
    e2 = pts[:, 2] - pts[:, 0]
    signed = 0.5 * (e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0])
    if tri_island.size == 0:
        empty = np.zeros(0, np.float64)
        return {"islands": np.zeros(0, np.int64), "area": empty, "signed_area": empty,
                "neg_area": empty, "bbox": np.zeros((0, 4), np.float64),
                "tri_island": tri_island, "tri_signed_area": signed}
    n = int(tri_island.max()) + 1
    area = np.bincount(tri_island, weights=np.abs(signed), minlength=n)
    sarea = np.bincount(tri_island, weights=signed, minlength=n)
    negarea = np.bincount(tri_island, weights=np.minimum(signed, 0.0), minlength=n)
    order = np.argsort(tri_island, kind="stable")
    ti = tri_island[order]
    starts = np.r_[0, np.flatnonzero(np.diff(ti)) + 1]
    isl = ti[starts]
    umin = np.minimum.reduceat(pts[order, :, 0].min(axis=1), starts)
    vmin = np.minimum.reduceat(pts[order, :, 1].min(axis=1), starts)
    umax = np.maximum.reduceat(pts[order, :, 0].max(axis=1), starts)
    vmax = np.maximum.reduceat(pts[order, :, 1].max(axis=1), starts)
    return {"islands": isl, "area": area[isl], "signed_area": sarea[isl], "neg_area": negarea[isl],
            "bbox": np.stack([umin, vmin, umax, vmax], axis=1), "tri_island": tri_island,
            "tri_signed_area": signed}


# -- snapshot ---------------------------------------------------------------------------


def _pick(out, section, key):
    """Save output value, nested ({"Data": {"PolySizes": ...}}) or dotted ("Data.PolySizes")."""
    table = out.get(section)
    if isinstance(table, dict) and key in table:
        return table[key]
    return out.get(f"{section}.{key}")


@dataclass
class UVSnapshot:
    """The UV state of a scene: polygons, their UV corners, UV positions and island ids."""

    poly_sizes: np.ndarray       # (P,) int64 corners per polygon
    poly_uvw_ids: np.ndarray     # (sum(sizes),) int64 corner -> UV vertex
    uv: np.ndarray               # (N, 2) float64
    poly_island_ids: np.ndarray  # (P,) int64

    @classmethod
    def from_save_output(cls, out: dict) -> "UVSnapshot":
        """From the merged results of Save({"Data": True}) and Save({"IndexTable": ...}).

        Without an IndexTable the islands are recomputed from the UV topology (same
        partition, different numbering than RizomUV's).
        """
        sizes = _pick(out, "Data", "PolySizes")
        ids = _pick(out, "Data", "PolyUVWIDs")
        coords = _pick(out, "Data", "CoordsUVW")
        if sizes is None or ids is None or coords is None:
            raise ValueError("Save output lacks Data.PolySizes / Data.PolyUVWIDs / Data.CoordsUVW "
                             "(was it taken with Save({'Data': True})?)")
        sizes = np.asarray(sizes, np.int64).reshape(-1)
        ids = np.asarray(ids, np.int64).reshape(-1)
        coords = np.asarray(coords, np.float64).reshape(-1)
        if coords.size % 3:
            raise ValueError(f"Data.CoordsUVW has {coords.size} values, not a multiple of 3")
        uv = np.ascontiguousarray(coords.reshape(-1, 3)[:, :2])
        if int(sizes.sum()) != ids.size:
            raise ValueError(f"Data.PolySizes sums to {int(sizes.sum())} corners but "
                             f"Data.PolyUVWIDs has {ids.size}")
        if ids.size and (int(ids.min()) < 0 or int(ids.max()) >= uv.shape[0]):
            raise ValueError("Data.PolyUVWIDs references a UV vertex outside Data.CoordsUVW")
        islands = _pick(out, "IndexTable", "PolygonIDsToIslandIDs")
        if islands is None:
            islands = uv_islands(sizes, ids)
        else:
            islands = np.asarray(islands, np.int64).reshape(-1)
            if islands.size != sizes.size:
                raise ValueError(f"IndexTable.PolygonIDsToIslandIDs has {islands.size} entries "
                                 f"for {sizes.size} polygons")
        return cls(sizes, ids, uv, islands)


# -- the measure report -----------------------------------------------------------------

# Sub-pixel shifts of the confirmation rasters: irrational fractions, so no pixel centre can
# fall exactly on an edge of a layout snapped to a power-of-two pixel grid.
_SHIFTS = (0.3183098861837907, 0.6180339887498949)


def _confirmed_overlap(uv, tris, res, tri_island):
    """Between-island overlap that survives a sub-pixel shift of the pixel grid.

    On the centred grid, islands that merely touch along an edge falling exactly on pixel
    centres are counted by both sides (Mech8 as loaded: 307 px at 1024, 0 on both shifted
    grids), and sub-pixel slivers come and go with the grid alignment (the same scene at
    2048: 1 / 0 / 121 px). Real overlap keeps about area x res^2 pixels on every grid
    (the same scene after Unfold: 1315 / 951 / 1097). The smaller of two shifted counts
    keeps the real overlap and drops both artefacts; its pairs come with it.
    """
    best = None
    for frac in _SHIFTS:
        off = frac / res
        r = rasterize_fast(uv, tris, res, box=(off, off, 1.0 + off, 1.0 + off), tri_island=tri_island)
        if best is None or r["inter_island_overlap_px"] < best["inter_island_overlap_px"]:
            best = r
        if not best["inter_island_overlap_px"]:
            break
    return {k: best[k] for k in ("inter_island_overlap_px", "overlap_pairs",
                                 "overlap_pairs_complete", "islands_in_overlap")}


def _r(x, digits):
    return None if x is None else round(float(x), digits)


def measure(snap: UVSnapshot, res: int = 1024, padding_px: float | None = None, *,
            exclude_islands=None, max_raster_res: int = MAX_RASTER_RES) -> dict:
    """Coverage, overlap, out-of-tile, island sizes and padding share of a UV layout.

    `res` is the map resolution the layout is judged at (the pack's MapResolution);
    `padding_px` the padding it was packed with, when known. `exclude_islands` drops
    islands from every statistic (diagnose passes the zero-3D-area ones); they stay
    counted in `islands` and are listed under `excluded_islands`.
    """
    t0 = time.perf_counter()
    res = int(res)
    sizes, ids, uv, pisl = snap.poly_sizes, snap.poly_uvw_ids, snap.uv, snap.poly_island_ids
    all_islands = np.unique(pisl)
    excluded = np.unique(np.asarray([] if exclude_islands is None else exclude_islands,
                                    np.int64).reshape(-1))
    excluded = excluded[np.isin(excluded, all_islands)]
    poly_keep = ~np.isin(pisl, excluded) if excluded.size else np.ones(sizes.size, bool)
    analysed = int(all_islands.size - excluded.size)

    tris, tri_poly = fan(sizes, ids)
    tri_keep = poly_keep[tri_poly]
    tris, tri_poly = tris[tri_keep], tri_poly[tri_keep]
    raster_res = min(res, int(max_raster_res))
    tri_island = pisl[tri_poly]
    ras = rasterize_fast(uv, tris, raster_res, tri_island=tri_island)
    if ras["inter_island_overlap_px"]:
        ras.update(_confirmed_overlap(uv, tris, raster_res, tri_island))
    st = island_stats_fast(uv, tris, tri_poly, pisl)

    coverage = ras["coverage"]
    area = st["area"]
    bbox = st["bbox"]
    side_px = np.sqrt(area) * res
    thin_px = np.minimum(bbox[:, 2] - bbox[:, 0], bbox[:, 3] - bbox[:, 1]) * res
    out_mask = ((bbox[:, 0] < -OUTSIDE_TILE_EPS) | (bbox[:, 1] < -OUTSIDE_TILE_EPS)
                | (bbox[:, 2] > 1 + OUTSIDE_TILE_EPS) | (bbox[:, 3] > 1 + OUTSIDE_TILE_EPS))
    outside = st["islands"][out_mask]

    a, b, owner = border_edges(sizes, ids)
    keep_edge = poly_keep[owner]
    border = float(np.sum(np.linalg.norm(uv[a[keep_edge]] - uv[b[keep_edge]], axis=1)))

    pairs = ras["overlap_pairs"]
    result = {
        "map_resolution": res,
        "polygons": int(sizes.size),
        "islands": int(all_islands.size),
        "uv_vertices": int(uv.shape[0]),
        "coverage": _r(coverage, 4),
        "uv_area": _r(area.sum(), 4),
        "overlap": {
            "pixels": ras["inter_island_overlap_px"],
            "island_pairs": len(pairs),
            "islands": len(ras["islands_in_overlap"]),
            "example_pairs": [[p, q] for p, q in pairs[:10]],
        },
        "outside_tile": {"islands": int(outside.size),
                         "example_ids": [int(i) for i in outside[:20]]},
        "island_size_px": {
            "mean": _r(np.sqrt(coverage * res * res / analysed), 1) if analysed else None,
            "median": _r(np.median(side_px), 1) if side_px.size else None,
            "p10": _r(np.percentile(side_px, 10), 1) if side_px.size else None,
        },
        "tiny_islands": {"under_4px": int(np.count_nonzero(side_px < 4)),
                         "slivers_under_2px": int(np.count_nonzero(thin_px < 2))},
        "border_length_uv": _r(border, 2),
        "padding_share_estimate": (None if padding_px is None
                                   else _r(border * float(padding_px) / (2.0 * res), 3)),
    }
    if not ras["overlap_pairs_complete"]:
        result["overlap"]["island_pairs_is_lower_bound"] = True
    if raster_res != res:
        result["raster_resolution"] = raster_res
    if excluded.size:
        result["excluded_islands"] = {"count": int(excluded.size),
                                      "ids": [int(i) for i in excluded[:50]]}
    result["seconds"] = round(time.perf_counter() - t0, 2)
    return result
