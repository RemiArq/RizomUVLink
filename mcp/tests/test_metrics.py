"""metrics.py: the vectorized oracle must count exactly like the reference, and measure()
must say what a hand calculation says."""

import importlib.util
import json
import sys

import numpy as np
import pytest

from rizomuv_mcp import metrics, objmesh


@pytest.fixture(scope="module")
def ref(repo_root):
    """RizomUVApp/tests/rizomtest/metrics.py, the oracle the app's own suite trusts."""
    path = repo_root / "RizomUVApp" / "tests" / "rizomtest" / "metrics.py"
    if not path.is_file():
        pytest.skip("reference oracle rizomtest/metrics.py not in this checkout")
    name = "rizomtest_metrics_reference"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # its dataclasses resolve their module through sys.modules
    spec.loader.exec_module(mod)
    return mod


def snap_from_polys(polys, islands=None):
    """One UV vertex per corner: polygons touch only through coincident coordinates."""
    sizes = np.array([len(p) for p in polys], np.int64)
    uv = np.array([pt for p in polys for pt in p], np.float64).reshape(-1, 2)
    islands = np.arange(len(polys)) if islands is None else np.asarray(islands)
    return metrics.UVSnapshot(sizes, np.arange(len(uv), dtype=np.int64), uv, islands.astype(np.int64))


def ref_mesh(ref, snap):
    coords = np.c_[snap.uv, np.zeros(len(snap.uv))]
    return ref.UVMesh(snap.poly_sizes, snap.poly_uvw_ids, coords, snap.poly_island_ids)


def _random_polys(seed, count, lo, hi, grid=None):
    rng = np.random.default_rng(seed)
    polys = []
    for k in range(count):
        n = 3 if k % 3 else 4
        pts = rng.uniform(lo, hi, size=(n, 2))
        if grid:
            pts = np.round(pts * grid) / grid  # dyadic: edges run through pixel centres
        polys.append([tuple(p) for p in pts])
    return polys


CASES = {
    "overlapping_islands": ([[(0.1, 0.1), (0.6, 0.1), (0.6, 0.6), (0.1, 0.6)],
                             [(0.4, 0.4), (0.9, 0.4), (0.9, 0.9), (0.4, 0.9)],
                             [(0.2, 0.2), (0.5, 0.25), (0.3, 0.5)]], [0, 1, 2]),
    # an arrow: the fan from vertex 0 folds over itself, exactly what concave UV quads do
    "concave_quads": ([[(0.2, 0.2), (0.8, 0.5), (0.2, 0.8), (0.45, 0.5)],
                       [(0.55, 0.1), (0.9, 0.05), (0.7, 0.25), (0.95, 0.4)]], [0, 1]),
    "tile_border": ([[(-0.3, 0.2), (1.4, 0.5), (0.5, 1.3)],
                     [(0.8, -0.2), (1.2, -0.2), (1.2, 0.3), (0.8, 0.3)],
                     [(1.5, 1.5), (2.0, 1.5), (2.0, 2.0)]], [0, 1, 2]),
    "degenerate": ([[(0.1, 0.1), (0.5, 0.5), (0.9, 0.9)],
                    [(0.3, 0.3), (0.3, 0.3), (0.6, 0.2)],
                    [(0.5, 0.5), (0.5, 0.5), (0.5, 0.5)],
                    [(0.1, 0.2), (0.3, 0.4)],
                    [(0.2, 0.6), (0.7, 0.65), (0.4, 0.95)]], [0, 0, 1, 1, 2]),
    "pixel_ties": (_random_polys(7, 60, 0.0, 1.0, grid=64), [k % 5 for k in range(60)]),
    "random_soup": (_random_polys(11, 150, -0.2, 1.2), [k % 9 for k in range(150)]),
    "empty": ([], []),
}
RESOLUTIONS = [1, 7, 32, 64, 100, 256]


def case(name):
    polys, islands = CASES[name]
    return snap_from_polys(polys, islands)


@pytest.mark.parametrize("res", RESOLUTIONS)
@pytest.mark.parametrize("name", list(CASES))
def test_rasterize_matches_reference(ref, name, res):
    snap = case(name)
    expected = ref.rasterize(ref_mesh(ref, snap), res)
    tris, tri_poly = metrics.fan(snap.poly_sizes, snap.poly_uvw_ids)
    got = metrics.rasterize_fast(snap.uv, tris, res)
    assert (got["filled_px"], got["overlap_px"]) == (expected.filled_px, expected.overlap_px)
    assert got["coverage"] == expected.coverage
    # the island attribution and the row bands must not change a single count
    tracked = metrics.rasterize_fast(snap.uv, tris, res, tri_island=snap.poly_island_ids[tri_poly])
    assert (tracked["filled_px"], tracked["overlap_px"]) == (expected.filled_px, expected.overlap_px)


@pytest.mark.parametrize("name", ["random_soup", "pixel_ties", "tile_border"])
def test_rasterize_with_a_box_matches_reference(ref, name):
    snap = case(name)
    box = (0.25, 0.1, 0.75, 0.9)
    expected = ref.rasterize(ref_mesh(ref, snap), 64, box)
    tris, _ = metrics.fan(snap.poly_sizes, snap.poly_uvw_ids)
    got = metrics.rasterize_fast(snap.uv, tris, 64, box=box)
    assert (got["filled_px"], got["overlap_px"]) == (expected.filled_px, expected.overlap_px)


def test_fan_matches_reference(ref):
    polys = [[(0, 0), (1, 0), (1, 1)], [(0, 0), (1, 0), (1, 1), (0, 1)],
             [(0, 0), (1, 0), (2, 1), (1, 2), (0, 1)], [(0, 0), (1, 1)], [(0, 0), (1, 0), (1, 1), (0, 1)]]
    snap = snap_from_polys(polys)
    tris, tri_poly = metrics.fan(snap.poly_sizes, snap.poly_uvw_ids)
    assert np.array_equal(tris, ref_mesh(ref, snap).triangles())
    assert tri_poly.tolist() == [0, 1, 1, 2, 2, 2, 4, 4]


def _brute_attribution(snap, res, box=(0.0, 0.0, 1.0, 1.0)):
    """Per-island masks, one island at a time: an independent count of the attribution."""
    tris, tri_poly = metrics.fan(snap.poly_sizes, snap.poly_uvw_ids)
    tri_island = snap.poly_island_ids[tri_poly]
    masks, total = {}, np.zeros((res, res), np.int64)
    for isl in np.unique(tri_island):
        counts = metrics.rasterize_fast(snap.uv, tris[tri_island == isl], res, box=box, return_counts=True)["counts"]
        masks[int(isl)] = counts > 0
        total += counts
    distinct = sum(m.astype(np.int64) for m in masks.values()) if masks else np.zeros((res, res), np.int64)
    pairs = sorted((a, b) for a in masks for b in masks if a < b and (masks[a] & masks[b]).any())
    return {"inter_island_overlap_px": int(np.count_nonzero(distinct > 1)),
            "self_overlap_px": int(np.count_nonzero(total > np.maximum(distinct, 1))),
            "overlap_pairs": pairs,
            "islands_in_overlap": sorted({i for p in pairs for i in p})}


@pytest.mark.parametrize("res", [16, 64, 100])
@pytest.mark.parametrize("name", ["overlapping_islands", "pixel_ties", "random_soup", "tile_border"])
@pytest.mark.parametrize("band", [None, 3])
def test_island_attribution_matches_brute_force(monkeypatch, name, res, band):
    snap = case(name)
    expected = _brute_attribution(snap, res)
    if band:
        monkeypatch.setattr(metrics, "BAND_PIXELS", res * band)
    tris, tri_poly = metrics.fan(snap.poly_sizes, snap.poly_uvw_ids)
    got = metrics.rasterize_fast(snap.uv, tris, res, tri_island=snap.poly_island_ids[tri_poly])
    for key, value in expected.items():
        assert got[key] == value, key
    assert got["overlap_pairs_complete"] is True


def test_pair_budget_truncates_pairs_only(monkeypatch):
    square = [(0.2, 0.2), (0.6, 0.2), (0.6, 0.6), (0.2, 0.6)]
    snap = snap_from_polys([square] * 5)  # five islands stacked: 10 pairs on every pixel
    tris, tri_poly = metrics.fan(snap.poly_sizes, snap.poly_uvw_ids)
    monkeypatch.setattr(metrics, "PAIR_BUDGET", 3)
    got = metrics.rasterize_fast(snap.uv, tris, 16, tri_island=snap.poly_island_ids[tri_poly])
    assert got["overlap_pairs_complete"] is False
    assert 0 < len(got["overlap_pairs"]) < 10
    assert got["islands_in_overlap"] == [0, 1, 2, 3, 4]
    m = metrics.measure(snap, 16)
    assert m["overlap"]["island_pairs_is_lower_bound"] is True and m["overlap"]["islands"] == 5


def test_island_stats_match_reference(ref):
    snap = case("random_soup")
    tris, tri_poly = metrics.fan(snap.poly_sizes, snap.poly_uvw_ids)
    st = metrics.island_stats_fast(snap.uv, tris, tri_poly, snap.poly_island_ids)
    expected = ref_mesh(ref, snap).island_stats()
    assert st["islands"].tolist() == sorted(expected)
    for k, isl in enumerate(st["islands"]):
        assert st["area"][k] == pytest.approx(expected[int(isl)].area, rel=1e-12)
        assert tuple(st["bbox"][k]) == expected[int(isl)].bbox


def _union_find_islands(sizes, ids):
    parent = list(range(int(max(ids)) + 1))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    offset = 0
    for s in sizes:
        for k in range(1, s):
            parent[find(ids[offset + k])] = find(ids[offset])
        offset += s
    offset, roots = 0, []
    for s in sizes:
        roots.append(find(ids[offset]))
        offset += s
    return roots


def _same_partition(a, b):
    pairs = set(zip(list(a), list(b)))
    return len(pairs) == len(set(a)) == len(set(b))


def test_uv_islands_is_the_topology_partition():
    rng = np.random.default_rng(3)
    sizes = rng.integers(3, 6, size=400)
    ids = rng.integers(0, 900, size=int(sizes.sum()))
    got = metrics.uv_islands(sizes, ids)
    assert _same_partition(got.tolist(), _union_find_islands(sizes.tolist(), ids.tolist()))
    assert metrics.uv_islands([], []).size == 0


def test_border_edges_of_a_quad_grid():
    # 2 x 2 quads on a 3 x 3 vertex grid of step 0.25: 8 border edges, length 2
    vid = lambda i, j: j * 3 + i  # noqa: E731
    quads = [[vid(i, j), vid(i + 1, j), vid(i + 1, j + 1), vid(i, j + 1)] for j in range(2) for i in range(2)]
    uv = np.array([(0.25 * i, 0.25 * j) for j in range(3) for i in range(3)])
    a, b, poly = metrics.border_edges([4] * 4, np.array(quads).ravel())
    assert a.size == 8
    assert np.linalg.norm(uv[a] - uv[b], axis=1).sum() == pytest.approx(2.0)
    assert metrics.border_edges([], [])[0].size == 0


def test_from_save_output_nested_dotted_and_without_index_table():
    nested = {"Data": {"PolySizes": [3, 3], "PolyUVWIDs": [0, 1, 2, 1, 3, 2],
                       "CoordsUVW": [0, 0, 0, 1, 0, 0, 0, 1, 0, 1, 1, 0]},
              "IndexTable": {"PolygonIDsToIslandIDs": [4, 4]}}
    snap = metrics.UVSnapshot.from_save_output(nested)
    assert snap.uv.shape == (4, 2) and snap.poly_island_ids.tolist() == [4, 4]
    dotted = {"Data.PolySizes": [3, 3], "Data.PolyUVWIDs": [0, 1, 2, 1, 3, 2],
              "Data.CoordsUVW": nested["Data"]["CoordsUVW"]}
    snap = metrics.UVSnapshot.from_save_output(dotted)
    assert snap.poly_island_ids.tolist() == [0, 0]  # recomputed from the shared edge
    for broken in ({"Data": {"PolySizes": [3], "PolyUVWIDs": [0, 1], "CoordsUVW": [0] * 9}},
                   {"Data": {"PolySizes": [3], "PolyUVWIDs": [0, 1, 5], "CoordsUVW": [0] * 9}},
                   {"Data": {"PolySizes": [3], "PolyUVWIDs": [0, 1, 2], "CoordsUVW": [0] * 8}},
                   {"Data": {"PolySizes": [3], "PolyUVWIDs": [0, 1, 2], "CoordsUVW": [0] * 9},
                    "IndexTable": {"PolygonIDsToIslandIDs": [0, 1]}},
                   {"IndexTable": {}}):
        with pytest.raises(ValueError):
            metrics.UVSnapshot.from_save_output(broken)


def _assert_plain(value):
    json.dumps(value, allow_nan=False)
    if isinstance(value, dict):
        for v in value.values():
            _assert_plain(v)
    elif isinstance(value, list):
        for v in value:
            _assert_plain(v)
    else:
        assert value is None or type(value) in (int, float, str, bool), type(value)


def square(u0, v0, size):
    return [(u0, v0), (u0 + size, v0), (u0 + size, v0 + size), (u0, v0 + size)]


def test_measure_hand_checked():
    # dyadic squares at res 16: 2 x 2 px and 4 x 4 px, no edge on a pixel centre
    snap = snap_from_polys([square(0.125, 0.125, 0.125), square(0.5, 0.5, 0.25)])
    m = metrics.measure(snap, 16, padding_px=1)
    _assert_plain(m)
    assert m["map_resolution"] == 16 and m["polygons"] == 2 and m["islands"] == 2 and m["uv_vertices"] == 8
    assert m["coverage"] == round(20 / 256, 4)
    assert m["uv_area"] == round(0.125 ** 2 + 0.25 ** 2, 4)
    assert m["overlap"] == {"pixels": 0, "island_pairs": 0, "islands": 0, "example_pairs": []}
    assert m["outside_tile"] == {"islands": 0, "example_ids": []}
    assert m["island_size_px"] == {"mean": round(10 ** 0.5, 1), "median": 3.0, "p10": 2.2}
    assert m["tiny_islands"] == {"under_4px": 1, "slivers_under_2px": 0}
    assert m["border_length_uv"] == 1.5
    assert m["padding_share_estimate"] == round(1.5 * 1 / (2 * 16), 3)
    assert "raster_resolution" not in m and "excluded_islands" not in m
    assert metrics.measure(snap, 16)["padding_share_estimate"] is None


def test_measure_outside_tile_slivers_and_exclusion():
    sliver = [(0.0625, 0.875), (0.5, 0.875), (0.5, 0.90625), (0.0625, 0.90625)]  # 0.5 px thick at 16
    snap = snap_from_polys([square(0.125, 0.125, 0.125), sliver, square(1.25, 0.25, 0.25)])
    m = metrics.measure(snap, 16)
    assert m["outside_tile"] == {"islands": 1, "example_ids": [2]}
    assert m["tiny_islands"]["slivers_under_2px"] == 1
    m = metrics.measure(snap, 16, exclude_islands=np.array([2]))
    assert m["outside_tile"]["islands"] == 0
    assert m["islands"] == 3 and m["excluded_islands"] == {"count": 1, "ids": [2]}


def test_measure_counts_overlap_between_islands_only():
    snap = snap_from_polys([square(0.125, 0.125, 0.375), square(0.3125, 0.3125, 0.375)])
    m = metrics.measure(snap, 16)
    assert m["overlap"]["pixels"] > 0
    assert m["overlap"]["island_pairs"] == 1 and m["overlap"]["example_pairs"] == [[0, 1]]
    # the same polygons in one island: a fold, not an overlap between islands
    assert metrics.measure(snap_from_polys(CASES["concave_quads"][0][:1] * 2, [0, 0]), 64)["overlap"]["pixels"] == 0


def test_touching_islands_are_not_overlap():
    """A mirrored island sharing an edge that runs through pixel centres: the centred grid
    counts that column twice, the shifted confirmation grids do not."""
    e = 8.5 / 16
    ccw = [(0.25, 0.25), (e, 0.25), (e, 0.5), (0.25, 0.5)]
    cw = [(e, 0.25), (e, 0.5), (0.75, 0.5), (0.75, 0.25)]
    snap = snap_from_polys([ccw, cw])
    tris, tri_poly = metrics.fan(snap.poly_sizes, snap.poly_uvw_ids)
    centred = metrics.rasterize_fast(snap.uv, tris, 16, tri_island=snap.poly_island_ids[tri_poly])
    assert centred["inter_island_overlap_px"] > 0  # the construction does produce the tie
    assert metrics.measure(snap, 16)["overlap"]["pixels"] == 0


def test_measure_empty_and_capped():
    empty = metrics.measure(snap_from_polys([]), 1024, padding_px=2)
    _assert_plain(empty)
    assert empty["coverage"] == 0.0 and empty["islands"] == 0 and empty["island_size_px"]["mean"] is None
    big = metrics.measure(snap_from_polys([square(0.1, 0.1, 0.5)]), 16384)
    assert big["raster_resolution"] == metrics.MAX_RASTER_RES and big["map_resolution"] == 16384
    assert big["coverage"] == pytest.approx(0.25, abs=1e-3)
    assert big["island_size_px"]["median"] == pytest.approx(0.5 * 16384, rel=1e-6)


def test_mech8_as_loaded(ref, mech8):
    """Real data: the OBJ's own UVs, islands from the UV topology."""
    mesh = objmesh.read_obj_full(mech8)
    snap = metrics.UVSnapshot(mesh.face_sizes, mesh.face_uv_ids, mesh.uvs,
                              metrics.uv_islands(mesh.face_sizes, mesh.face_uv_ids))
    tris, _ = metrics.fan(snap.poly_sizes, snap.poly_uvw_ids)
    expected = ref.rasterize(ref_mesh(ref, snap), 128)
    got = metrics.rasterize_fast(snap.uv, tris, 128)
    assert (got["filled_px"], got["overlap_px"]) == (expected.filled_px, expected.overlap_px)
    m = metrics.measure(snap, 1024)
    _assert_plain(m)
    # RizomUV reports the same scene, loaded, as 2573 islands and (Save Data) coverage 0.8572
    assert m["islands"] == 2573 and m["polygons"] == 41804
    assert m["coverage"] == pytest.approx(0.8572, abs=5e-4)
    assert m["overlap"]["pixels"] == 0  # the centred grid's 307 px are islands touching exactly
    assert m["seconds"] < 10
