"""diagnose.py rules on synthetic scenes and on Mech8; objmesh.py, its 3D input."""

import gzip
import json
import os
import shutil
import time
from pathlib import Path

import numpy as np
import pytest

from rizomuv_mcp import diagnose, metrics, objmesh


# -- synthetic scenes -------------------------------------------------------------------


def grid_island(n, uv0, s, p0, length, warp=None):
    """An n x n grid of quads: UV square of side s at uv0, 3D square of side `length` at p0."""
    uv, pos = [], []
    for j in range(n + 1):
        for i in range(n + 1):
            x, y = i / n, j / n
            uv.append((uv0[0] + s * x, uv0[1] + s * y))
            wx = warp(x) if warp else x
            pos.append((p0[0] + length * wx, p0[1] + length * y, p0[2]))
    quads = [[j * (n + 1) + i, j * (n + 1) + i + 1, (j + 1) * (n + 1) + i + 1, (j + 1) * (n + 1) + i]
             for j in range(n) for i in range(n)]
    return uv, pos, quads


def scene(*islands):
    """Concatenate islands; UV vertex ids equal 3D vertex ids, as in RizomUV's OBJ export."""
    uv, pos, sizes, ids, isl = [], [], [], [], []
    for k, (iuv, ipos, quads) in enumerate(islands):
        base = len(uv)
        uv += iuv
        pos += ipos
        for q in quads:
            sizes.append(len(q))
            ids += [base + v for v in q]
            isl.append(k)
    snap = metrics.UVSnapshot(np.array(sizes, np.int64), np.array(ids, np.int64), np.array(uv, np.float64),
                              np.array(isl, np.int64))
    mesh3d = (np.array(pos, np.float64), np.array(sizes, np.int64), np.array(ids, np.int64))
    return snap, mesh3d


def four_clean_islands(scale=0.2):
    return [grid_island(2, (0.05 + 0.5 * (k % 2), 0.05 + 0.5 * (k // 2)), scale, (3.0 * k, 0, 0), 1.0)
            for k in range(4)]


def codes(result):
    return [f["code"] for f in result["findings"]]


def check_plain(result):
    json.dumps(result, allow_nan=False)
    order = [diagnose._ORDER[f["severity"]] for f in result["findings"]]
    assert order == sorted(order)
    assert result["findings"], "a diagnose always says something"


def test_clean_layout():
    snap, mesh3d = scene(*four_clean_islands())
    r = diagnose.diagnose(snap, 1024, 2, mesh3d, 0.0)
    check_plain(r)
    assert codes(r) == ["clean"]
    assert r["findings"][0]["severity"] == "info"
    assert r["degenerate_islands"] == {"count": 0, "ids": []}
    assert r["texel_density"]["ratio_p5"] == pytest.approx(1.0) and r["texel_density"]["islands_off_10pct"] == 0
    assert r["texel_density"]["px_per_unit"] == pytest.approx(0.2 * 1024, rel=1e-3)
    assert r["stretch"]["area_share_outside_0.8_1.25"] == 0.0
    assert r["flipped_uv_area"] == 0.0


def test_overlap_is_high_and_first():
    islands = four_clean_islands()
    islands[1] = grid_island(2, (0.15, 0.15), 0.2, (3.0, 0, 0), 1.0)  # onto island 0
    snap, mesh3d = scene(*islands)
    r = diagnose.diagnose(snap, 1024, 2, mesh3d)
    check_plain(r)
    assert r["findings"][0]["code"] == "overlap" and r["findings"][0]["severity"] == "high"
    assert "0–1" in r["findings"][0]["message"]
    assert r["metrics"]["overlap"]["example_pairs"] == [[0, 1]]


def test_outside_tile():
    islands = four_clean_islands()
    islands[3] = grid_island(2, (1.2, 0.3), 0.2, (9.0, 0, 0), 1.0)
    snap, mesh3d = scene(*islands)
    r = diagnose.diagnose(snap, 1024, 2, mesh3d)
    assert "outside_tile" in codes(r)
    assert r["metrics"]["outside_tile"]["example_ids"] == [3]


def test_degenerate_islands_are_named_and_excluded():
    islands = four_clean_islands()
    # zero 3D area, left at its "3D XY" far outside the tile, as Pack does on Mech8
    islands.append(grid_island(1, (1.6, 8.9), 0.05, (5.0, 5.0, 5.0), 0.0))
    snap, mesh3d = scene(*islands)
    r = diagnose.diagnose(snap, 1024, 2, mesh3d)
    check_plain(r)
    assert r["degenerate_islands"] == {"count": 1, "ids": [4]}
    assert "degenerate_islands" in codes(r) and "outside_tile" not in codes(r)
    msg = next(f["message"] for f in r["findings"] if f["code"] == "degenerate_islands")
    assert "ids 4" in msg and "outside the tile" in msg
    assert r["metrics"]["excluded_islands"] == {"count": 1, "ids": [4]}
    assert r["metrics"]["islands"] == 5
    assert r["texel_density"]["islands_off_25pct"] == 0


def test_uneven_texel_density():
    islands = four_clean_islands()
    islands[2] = grid_island(2, (0.05, 0.55), 0.4, (6.0, 0, 0), 1.0)  # twice the texels per unit
    snap, mesh3d = scene(*islands)
    r = diagnose.diagnose(snap, 1024, 2, mesh3d)
    check_plain(r)
    td = r["texel_density"]
    assert td["islands_off_25pct"] >= 1 and 2 in td["example_ids_off_25pct"]
    finding = next(f for f in r["findings"] if f["code"] == "texel_density_uneven")
    assert finding["severity"] == "medium"


def test_stretch():
    islands = four_clean_islands()
    islands[0] = grid_island(6, (0.05, 0.05), 0.2, (0, 0, 0), 1.0, warp=lambda x: x * x)
    snap, mesh3d = scene(*islands)
    r = diagnose.diagnose(snap, 1024, 2, mesh3d)
    check_plain(r)
    assert r["stretch"]["area_share_outside_0.8_1.25"] > diagnose.STRETCH_SHARE_LIMIT
    assert "stretch" in codes(r)


def test_flips_visible_or_negligible():
    snap, mesh3d = scene(*four_clean_islands())
    r = diagnose.diagnose(snap, 1024, 2, mesh3d, negative_uv_area=-0.01)
    assert r["findings"][0]["code"] == "flipped_uvs" and r["flipped_uv_area"] == -0.01
    r = diagnose.diagnose(snap, 1024, 2, mesh3d, negative_uv_area=-1.4e-6)  # Mech8 as loaded
    assert "flipped_uvs" not in codes(r) and any("flipped" in n for n in r["notes"])
    assert codes(r) == ["clean"]


def many_small_islands(count, size):
    per_row = int(np.ceil(np.sqrt(count)))
    step = 1.0 / per_row
    return [grid_island(1, ((k % per_row) * step + 0.1 * step, (k // per_row) * step + 0.1 * step), size,
                        (2.0 * k, 0, 0), size * 30) for k in range(count)]


def test_resolution_too_low_with_padding():
    snap, mesh3d = scene(*many_small_islands(400, 0.02))
    r = diagnose.diagnose(snap, 256, 2, mesh3d)
    check_plain(r)
    finding = next(f for f in r["findings"] if f["code"] == "resolution_too_low")
    assert r["metrics"]["padding_share_estimate"] > diagnose.PADDING_SHARE_LIMIT
    assert finding["message"].startswith("Padding takes about ")
    assert "Doubling map_resolution" in finding["message"] and "MaxMutations" in finding["message"]


def test_resolution_too_low_without_padding_asks_for_it():
    snap, mesh3d = scene(*many_small_islands(400, 0.012))
    r = diagnose.diagnose(snap, 256, None, mesh3d)
    finding = next(f for f in r["findings"] if f["code"] == "resolution_too_low")
    assert "Pass padding_px" in finding["message"]
    tiny = next(f for f in r["findings"] if f["code"] == "tiny_islands")
    assert tiny["severity"] == "medium"


def test_without_or_with_a_mismatched_3d_mesh():
    snap, mesh3d = scene(*four_clean_islands())
    r = diagnose.diagnose(snap, 1024, 2, None, notes=["Save wrote no file (demo licence?)."])
    check_plain(r)
    assert r["degenerate_islands"] is None and r["texel_density"] is None and r["stretch"] is None
    assert r["notes"][0] == "Save wrote no file (demo licence?)." and any("3D" in n for n in r["notes"])
    positions, sizes, ids = mesh3d
    r = diagnose.diagnose(snap, 1024, 2, (positions, sizes[:-1], ids[:-4]))
    assert r["texel_density"] is None and any("does not match" in n for n in r["notes"])


def test_findings_sorted_by_severity():
    islands = four_clean_islands()
    islands.append(grid_island(1, (1.6, 8.9), 0.05, (5.0, 5.0, 5.0), 0.0))
    islands[1] = grid_island(2, (0.15, 0.15), 0.2, (3.0, 0, 0), 1.0)
    snap, mesh3d = scene(*islands)
    r = diagnose.diagnose(snap, 1024, 2, mesh3d, negative_uv_area=-0.01)
    check_plain(r)
    assert [f["severity"] for f in r["findings"]][:2] == ["high", "high"]


# -- real data --------------------------------------------------------------------------


def test_mech8_as_loaded(mech8):
    mesh = objmesh.read_obj_full(mech8)
    islands = metrics.uv_islands(mesh.face_sizes, mesh.face_uv_ids)
    snap = metrics.UVSnapshot(mesh.face_sizes, mesh.face_uv_ids, mesh.uvs, islands)
    r = diagnose.diagnose(snap, 1024, None, (mesh.positions, mesh.face_sizes, mesh.face_pos_ids))
    check_plain(r)
    assert r["degenerate_islands"]["count"] == 2
    assert r["metrics"]["overlap"]["pixels"] == 0 and r["metrics"]["outside_tile"]["islands"] == 0
    assert set(codes(r)) == {"degenerate_islands", "tiny_islands"}
    assert r["texel_density"]["islands_off_25pct"] == 0
    assert r["stretch"]["area_share_outside_0.8_1.25"] < diagnose.STRETCH_SHARE_LIMIT


def _fixtures_dir():
    """Mech8 states captured from a real RizomUV (calibration run), when available."""
    path = os.environ.get("RIZOMUV_MCP_FIXTURES")
    if not path or not (Path(path) / "mech8_meta.json").is_file():
        pytest.skip("set RIZOMUV_MCP_FIXTURES to the directory of the captured Mech8 states")
    return Path(path)


def test_mech8_packed_by_rizomuv_1024_2px(tmp_path):
    """The calibration numbers: coverage 0.6231, no overlap, degenerate 1183 and 1184,
    islands average 15.9 px, padding takes ~15 % of the map."""
    fixtures = _fixtures_dir()
    meta = json.loads((fixtures / "mech8_meta.json").read_text())
    with gzip.open(fixtures / "mech8_pack1024_pad2.snapshot.json.gz", "rt", encoding="utf-8") as f:
        snap = metrics.UVSnapshot.from_save_output(json.load(f))
    obj = tmp_path / "packed.obj"
    with gzip.open(fixtures / "mech8_pack1024_pad2.obj.gz", "rb") as src, open(obj, "wb") as dst:
        shutil.copyfileobj(src, dst)
    r = diagnose.diagnose(snap, 1024, 2, objmesh.read_obj(obj), meta["pack1024_pad2"]["negative_uv_area"])
    check_plain(r)
    m = r["metrics"]
    assert m["coverage"] == 0.6231 and m["uv_area"] == 0.6233 and m["islands"] == 2573
    assert m["overlap"]["pixels"] == 0 and m["outside_tile"]["islands"] == 0
    assert r["degenerate_islands"] == {"count": 2, "ids": [1183, 1184]}
    assert m["island_size_px"] == {"mean": 15.9, "median": 7.2, "p10": 1.9}
    assert m["padding_share_estimate"] == pytest.approx(0.149, abs=0.002)
    assert r["texel_density"]["px_per_unit"] == 34.3
    assert codes(r) == ["degenerate_islands", "resolution_too_low", "tiny_islands"]


# -- objmesh ----------------------------------------------------------------------------


OBJ = """# every face form the reader must take
v 0 0 0
v 1 0 0 1.0
v 1 1 0 0.5 0.5 0.5
  v 0 1 0
vt 0 0
vt 1 0 0
vt 1 1
vn 0 0 1
g grp
usemtl m
s off
f 1 2 3
f 1/1 2/2 3/3
f 1//1 3//1 4//1
f -4/-3/-1 -3/-2/-1 -2/-1/-1 -1/-1/-1
f 1 2 \\
  3 4
l 1 2
o next
v 2 2 2
f -1 1 2
"""


def test_read_obj_forms(tmp_path):
    path = tmp_path / "forms.obj"
    path.write_text(OBJ)
    positions, sizes, ids = objmesh.read_obj(path)
    assert positions.tolist() == [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [2, 2, 2]]
    assert sizes.tolist() == [3, 3, 3, 4, 4, 3]
    assert ids.tolist() == [0, 1, 2, 0, 1, 2, 0, 2, 3, 0, 1, 2, 3, 0, 1, 2, 3, 4, 0, 1]
    assert positions.dtype == np.float64 and ids.dtype == np.int64 and sizes.dtype == np.int64
    crlf = tmp_path / "forms_crlf.obj"
    crlf.write_bytes(OBJ.replace("\n", "\r\n").encode())
    again = objmesh.read_obj(crlf)
    assert all(np.array_equal(a, b) for a, b in zip(again, (positions, sizes, ids)))
    full = objmesh.read_obj_full(path)
    assert full.face_uv_ids is None  # "f 1 2 3" has no vt
    assert full.uvs.tolist() == [[0, 0], [1, 0], [1, 1]]


def test_read_obj_uv_indices(tmp_path):
    path = tmp_path / "uv.obj"
    path.write_text("v 0 0 0\nv 1 0 0\nv 1 1 0\nvt 0 0\nvt 1 0\nvt 1 1\nvt 0 1\nf 1/2 2/3 3/-1\n")
    full = objmesh.read_obj_full(path)
    assert full.face_uv_ids.tolist() == [1, 2, 3]


@pytest.mark.parametrize("text", ["v 0 0 0\nv 1 0 0\nv 1 1 0\nf 0 1 2\n",
                                  "v 0 0 0\nv 1 0 0\nv 1 1 0\nf 1 2 9\n",
                                  "v 0 0 0\nv 1 0 0\nv 1 1 0\nf -1 -2 -4\n",
                                  "v 0 0\nf 1 1 1\n"])
def test_read_obj_rejects_bad_files(tmp_path, text):
    path = tmp_path / "bad.obj"
    path.write_text(text)
    with pytest.raises(ValueError):
        objmesh.read_obj(path)


def test_read_obj_is_fast_and_exact_on_mech8(mech8):
    t = time.perf_counter()
    positions, sizes, ids = objmesh.read_obj(mech8)
    elapsed = time.perf_counter() - t
    assert elapsed < 1.5, f"read_obj took {elapsed:.2f} s on a 6 MB OBJ"
    v, s, f = [], [], []
    with open(mech8) as fh:
        for line in fh:
            if line.startswith("v "):
                v.append([float(x) for x in line.split()[1:4]])
            elif line.startswith("f "):
                toks = line.split()[1:]
                s.append(len(toks))
                f.extend(int(t.split("/")[0]) - 1 for t in toks)
    assert np.array_equal(positions, np.array(v)) and np.array_equal(sizes, s) and np.array_equal(ids, f)
