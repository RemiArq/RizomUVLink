"""Minimal Wavefront OBJ reader: 3D positions and faces, optionally the UV side.

Exists for diagnose: RizomUV has no data-tree node for 3D coordinates, so the 3D side of
the scene comes from a temporary `Save({"File": {"Path": tmp}})`, whose face order and
`vt` indices are exactly `Data.PolySizes` / `Data.PolyUVWIDs`. A 6 MB export must parse
in well under a second: the per-line work is one bytes slice, everything else is bulk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np

_CONTINUATION = re.compile(rb"\\[ \t]*\r?\n")
_AFTER_SLASH = re.compile(rb"/[^ \t]*")


@dataclass
class ObjMesh:
    positions: np.ndarray    # (V, 3) float64
    face_sizes: np.ndarray   # (F,) int64
    face_pos_ids: np.ndarray  # (sum(face_sizes),) int64, 0-based into positions
    uvs: np.ndarray          # (T, 2) float64, empty when the file has no vt
    face_uv_ids: np.ndarray | None  # like face_pos_ids, None unless every corner has a vt


def read_obj(path):
    """(positions (V, 3) float64, face_sizes (F,) int64, face_pos_ids flat int64).

    Handles `v`, `f a`, `f a/b`, `f a//c`, `f a/b/c`, negative (relative) indices, polygons
    of any size and backslash line continuations; every other statement is ignored.
    """
    mesh = read_obj_full(path, with_uv=False)
    return mesh.positions, mesh.face_sizes, mesh.face_pos_ids


def read_obj_full(path, with_uv=True) -> ObjMesh:
    """The same parse, keeping `vt` and the per-corner UV indices when `with_uv`."""
    with open(path, "rb") as f:
        data = f.read()
    if b"\\" in data:
        data = _CONTINUATION.sub(b" ", data)

    v_lines, vt_lines, f_lines = [], [], []
    f_vbase, f_tbase = [], []  # v / vt counts seen before each face: anchors negative ids
    for line in data.splitlines():
        head = line[:2]
        if head[:1] in b" \t":
            line = line.lstrip()
            head = line[:2]
        if head == b"v " or head == b"v\t":
            v_lines.append(line)
        elif head == b"f " or head == b"f\t":
            f_lines.append(line)
            f_vbase.append(len(v_lines))
            f_tbase.append(len(vt_lines))
        elif with_uv and line[:3] in (b"vt ", b"vt\t"):
            vt_lines.append(line)

    positions = _vectors(v_lines, 3, 2)
    uvs = _vectors(vt_lines, 2, 3) if with_uv else np.zeros((0, 2), np.float64)

    tokens = [ln.split()[1:] for ln in f_lines]
    face_sizes = np.fromiter((len(t) for t in tokens), np.int64, len(tokens))
    corners = b" ".join(b" ".join(t) for t in tokens)
    face_pos_ids = _indices(_AFTER_SLASH.sub(b"", corners), face_sizes,
                            np.asarray(f_vbase, np.int64), "v", positions.shape[0])
    face_uv_ids = None
    if with_uv and uvs.shape[0]:
        uv_tokens = [c.split(b"/")[1] if c.count(b"/") else b"" for c in corners.split()]
        if all(uv_tokens):
            face_uv_ids = _indices(b" ".join(uv_tokens), face_sizes,
                                   np.asarray(f_tbase, np.int64), "vt", uvs.shape[0])
    return ObjMesh(positions, face_sizes, face_pos_ids, uvs, face_uv_ids)


def _vectors(lines, width, skip):
    """First `width` numbers after the `skip`-byte keyword of each line, float64 exact."""
    if not lines:
        return np.zeros((0, width), np.float64)
    rows = [ln[skip:].split() for ln in lines]
    lens = {len(r) for r in rows}
    if len(lens) == 1 and lens.pop() == width:
        flat = [x for r in rows for x in r]
    else:
        # vertex colours (v x y z r g b), weights (v x y z w), a 3rd vt coordinate
        short = next((i for i, r in enumerate(rows) if len(r) < width), None)
        if short is not None:
            raise ValueError(f"OBJ line {lines[short][:60]!r} has fewer than {width} numbers")
        flat = [x for r in rows for x in r[:width]]
    return np.array([float(x) for x in flat], np.float64).reshape(-1, width)


def _indices(text, face_sizes, base_per_face, kind, count):
    """1-based / negative OBJ indices -> 0-based, validated against `count` elements."""
    raw = np.array(text.split(), np.int64) if text else np.zeros(0, np.int64)
    if raw.size != int(face_sizes.sum()):
        raise ValueError(f"OBJ face {kind} indices could not be read ({raw.size} for "
                         f"{int(face_sizes.sum())} corners)")
    if raw.size and (raw == 0).any():
        raise ValueError(f"OBJ face references {kind} index 0 (indices are 1-based)")
    ids = raw - 1
    neg = raw < 0
    if neg.any():
        ids[neg] = np.repeat(base_per_face, face_sizes)[neg] + raw[neg]
    if ids.size and (int(ids.min()) < 0 or int(ids.max()) >= count):
        raise ValueError(f"OBJ face references a {kind} index outside the {count} defined")
    return ids
