# The Mesh Check report

The Mesh Check report is a JSON file. In it, RizomUV lists the problems of a 3D mesh,
before you start the UVs. It gives every case of every check, with the vertex and face
numbers of **your file**. Two kinds of readers use it:

- a **plugin** in the 3D software the mesh comes from (Maya, Blender, 3ds Max, Houdini,
  Cinema 4D...). The plugin finds each problem in its mesh and fixes it.
- an **AI assistant**. It reads the report to explain the problems to a user, to choose
  what to fix first, or to write that plugin. The sections *How to read a report* and
  *Each check, one by one* are written for it.

The JSON Schema of the format is [meshcheck.schema.json](meshcheck.schema.json).

RizomUV never changes the 3D mesh when it exports. So every problem in a report is
still in the file.

## How to make a report

In RizomUV: **Window > Mesh Checker**, then **Export JSON...**

In Python, with RizomUVLink. It works the same with a RizomUV that has no window
(`headless=True`):

```python
from RizomUVLink import CRizomUVLink

link = CRizomUVLink()
link.RunRizomUV(headless=True)
link.Load({"File": {"Path": "C:/models/crate.fbx", "XYZUVW": True}})
link.MeshCheck({"File": "C:/models/crate.meshcheck.json",   # the RizomUV process writes it
                "MaxNonPlanarDeg": 2.0,                       # optional, default 2
                "PoleMinEdges": 6})                           # optional, default 6
link.Quit({})
```

Without `File`, `MeshCheck` returns the numbers that the panel shows. A full example is
in [examples/MeshCheck.py](../examples/MeshCheck.py).

## The rules of the format

1. **The IDs are the reference.** Vertex and face IDs start at 0. They are global: they
   count in the order of the file. In an OBJ file, it is the order of the `v` and `f`
   lines (OBJ itself starts at 1, so add 1). In an FBX, USD or glTF file, the meshes come
   one after the other.
2. **Local IDs give the place inside one object.** Each face also has an ID inside its
   object (`faceLocal`, `facesLocal`, `overlapsLocal`), and each vertex too
   (`vertexLocal`, `verticesLocal`, `neighboursLocal`). `object` is an index in the
   `objects` list. For a vertex: local = global − `objects[object].vertexBase`. A 3D
   software finds its meshes this way.
3. **Positions are only a help.** They use the axes (`source.upAxis`) and the unit
   (`source.unit`) of the file. Use them to find an element again if the mesh changed
   after the report, or to show it.
4. **The `vertices` of a face is its list of corners, exactly as in the file**, with the
   vertices that are used two times.
5. **The report shows the file as it was loaded.** When you edit the mesh, the numbers
   change. After a fix, make a new report. Do not use the old one again.

## The first level of the file

| Field | Meaning |
|---|---|
| `format` | always `"rizomuv.meshcheck"` |
| `formatVersion` | `1`. A later version only adds fields. A reader skips the fields it does not know |
| `formatDoc`, `schema` | where this document and the JSON Schema are |
| `generator` | the RizomUV version that wrote the file |
| `date` | when it was written, ISO 8601, UTC |
| `source` | `file` (the path that was loaded), `upAxis` (`x`, `y` or `z`), `unit` (the RizomUV name: `mm`, `cm`, `m`, `in`...), `vertexCount`, `faceCount` |
| `conventions` | the rules above, in words |
| `parameters` | the limits of the measures: `maxNonPlanarDeg`, `poleMinEdges` |
| `objects` | the objects that have faces: `name`, `faceCount`, `vertexBase` (the global ID of its first vertex), `vertexCount` (how many global IDs its faces use, from the first to the last) |
| `checks` | all the checks below. Always all of them, always in this order |

Each check has: `id`, `category`, `label`, `kind` (`Edge`, `Vertex` or `Polygon`: what
each case points to), `uvEffect` (two or three words), `description`, `suggestedFix`,
`count` (always the number of cases) and `instances` (the cases).

`category` can be:

- **`file`**: a problem in the topology of the file. RizomUV works around it for the
  unfold, but the file still has it.
- **`measure`**: a value that is compared to a limit. It is about quality. It is not
  always an error.

## Winding, orientation and normals

Two checks are about the direction of the faces: `FlippedNormals` and
`InconsistentNormals`. Their names use the word *normals*, because every 3D software
uses it. But what they check is the *winding*. This section explains the difference.
A plugin or an assistant that mixes the two will use the wrong fix.

### Winding

A face lists its vertices in an order. This order is its **winding**.

Look at a face from the side where its vertices turn counter-clockwise: you see its
**front**. So the winding decides which side of the face is the front. If you reverse
the order of the list, the front and the back change places.

### The normal that comes from the winding

Here, the **normal** of a face is the direction its front points to. It is not saved
anywhere. It comes from the winding and from the positions of the vertices. If you
reverse the winding, this normal goes to the other side.

This is why every 3D software says "flipped normals" when a face points the wrong way,
and why the fix is to reverse the face. On the screen you see the normal (a black face,
or a back face). But the cause is the winding.

### The normals saved in the file are a different thing

A file can also save **normals** of its own (OBJ `vn`, FBX and USD normals). There is
one direction for each corner, and it is used for shading. These normals are data. The
artist or a tool sets them, and they can point in any direction.

**The two checks do not look at them.** They only read the winding and how the faces
share their edges. This is topology, not directions.

The normals of the file are used for one thing only. When RizomUV must decide which
side of a surface is the outside, the file normals vote: a face whose winding agrees
with its file normals votes for its side. When the file has no normals, the side of
most of the faces wins.

### Faces that agree, and flipped normals

Two faces side by side **agree** when they go along their shared edge in **opposite**
directions. Then their fronts are on the same side of the surface. On a cube with all
its faces to the outside, each edge is used one time in each direction.

Now, write one face of this cube in the other order. The edges around it are now used
two times in the same direction. Its front is inside, and the fronts of the other faces
are outside. It is easy to fix: reverse this one face.

This is **`FlippedNormals`**: a face that points the other way, compared to the faces
around it. To fix it, reverse the face. RizomUV reverses it in its own copy of the mesh
and reports it. Your file still has it.

### Surfaces that cannot be oriented

On some surfaces, the faces can never all agree. A **Moebius strip** is an example. It
is a ribbon, closed with a half twist. Start on a face, front up. Go from face to face
and keep the same front side. After one turn, you are on the **other** side of the face
where you started.

On such a surface, you can reverse any faces you want: at least one edge is still used
two times in the same direction. When you reverse a face, the problem only goes to the
next edge. This surface is **non-orientable**: it has no front and no back.

This is **`InconsistentNormals`**, with the label *Non-orientable edges*. RizomUV first
turns the faces the best way it can. The edges that still have the problem after that
are the ones in this check.

A real Moebius strip is rare. Most of the time, the cause is a weld that closed a tube
or a ring with a half turn, or a merge of vertices that joined the front of one part to
the back of another part. To fix it, cut the surface, or build it again. RizomUV unfolds
these edges as cuts.

### How to know which one it is

| | `FlippedNormals` — *Flipped normals* | `InconsistentNormals` — *Non-orientable edges* |
|---|---|---|
| What is wrong | one face, or some faces, point the wrong way | the shape has no front and no back |
| Does it help to reverse faces? | yes | no, never |
| What each case points to | faces | edges |
| What RizomUV does | turns the faces, no new seam | cuts the edge: you get a seam you did not ask for |
| Fix in the 3D software | Conform (Maya), Unify (3ds Max), Recalculate Outside (Blender), or Reverse / Flip | cut the surface, or build it again. Conform and similar tools cannot fix it |

On a non-orientable surface, RizomUV can also reverse some faces while it tries. These
faces are then also in FlippedNormals. In this case, the problem is the edge, not these
faces.

### What the two checks do not find

- **A whole object turned inside out.** All its faces agree, but all point to the
  inside. No face disagrees with the faces around it, so nothing is reported. Most 3D
  software calls this "flipped normals" too.
- **A face whose winding does not agree with its own file normals**, but agrees with the
  faces around it. The topology is correct, so nothing is reported.

In both cases, the winding does not agree with the normals of the file. These two checks
do not compare directions, so they cannot find it.

### The same problems in 3D software

| Software | How it names the problem | What fixes it |
|---|---|---|
| Blender | *Non Contiguous* (Select All by Trait): the edges between two faces with opposite normals. These are the borders of flipped faces, and the non-orientable edges | Recalculate Outside, Flip |
| Maya | no check with a name; "normals not facing the same way" | Conform (the side of most faces), Reverse |
| 3ds Max | "inconsistent normals", some to the outside and some to the inside; xView *Face Orientation* | Unify, Flip |
| Houdini | "inconsistently wound polygons" (PolyDoctor) | *Correct Winding of Polygons to Majority of their Manifold Patch* |

No 3D software has a name for the non-orientable case. Their tools only fail on it.

## Words

| Word | Meaning |
|---|---|
| **vertex, edge, face** | a point; a line between two vertices; a polygon. The RizomUV interface says *polygon*, the report says *face* |
| **winding** | the order of the vertices of a face. It decides which side is the front |
| **normal** | the direction of the front of a face, as the winding gives it. It is not the normals saved in the file |
| **faces that agree** | two faces side by side that go along their shared edge in opposite directions |
| **non-orientable** | a surface where the faces can never all agree, like a Moebius strip |
| **manifold** | each edge has two faces or less, and the faces around each vertex make one fan |
| **fan** | the faces around a vertex that follow each other: each face shares an edge with the next one |
| **border** | an edge with only one face |
| **valence** | the number of edges of a vertex |
| **island, seam** | in the UVs: a part that is unfolded alone, and the cut edges around it |

## Each check, one by one

Severity, for a user who wants clean UVs:

- **high**: it changes the seams or the islands, and the user did not ask for it;
- **medium**: it makes the result worse, or doubles it;
- **low**: it is information, or a modeling choice.

### NonManifoldEdges — high, `file`, kind `Edge`

An edge that has more than two faces: a fin, a T-shaped joint, or two parts that touch
along one edge. RizomUV cannot unfold it as one surface, so it cuts the edge for the
unfold. The user gets **a seam that they did not ask for**.

Case: `vertices` [a, b] and `verticesLocal`, `positions` of both ends, `faces` (all the
faces of the edge, three or more), `facesLocal`.

Fix: keep two faces on the edge. Detach the other faces (they become a separate part),
or delete them if they are not needed.

### InconsistentNormals — *Non-orientable edges* — high, `file`, kind `Edge`

An edge where the two faces can **never** agree. The surface has no front and no back
(it is non-orientable, like a Moebius strip).

RizomUV first turns the faces of each surface the best way it can. The faces it turns
are in FlippedNormals. The edges that still have the problem are in this check: no
reversing can fix them. Reversing faces only moves the problem to another edge. RizomUV
cuts the edge for the unfold, so the user gets a seam that they did not ask for.

Case: the same fields as NonManifoldEdges.

Fix: cut the surface along the edge (a real seam, or detach), or build this part again.
**Do not** reverse faces, and do not use *Conform* or *Recalculate*: they cannot work
here.

The faces of such a surface often also appear in FlippedNormals (RizomUV turned some of
them while it tried). Because the surface is twisted, they can also appear in
NonPlanarFaces.

### NonManifoldVertices — high, `file`, kind `Vertex`

A vertex where the faces do not make one continuous surface. It can be:

- two surfaces that touch only at this vertex (a bow-tie);
- faces that share the vertex, but their borders do not follow each other;
- a face that is written two times on the same vertices.

RizomUV unfolds each part as a separate island.

Case: `vertex`, `vertexLocal`, `position`, `faces` (all the faces that use it) and
**`fans`**: these faces, in groups. In a group, each face shares an edge with the next
one around the vertex. Two groups or more means that two surfaces or more meet there.

Fix: split the vertex, one copy for each group of faces. Or join the surfaces with a
real edge. For a face written two times, delete one copy: it is also in
OverlappingFaces.

### DegenerateFaces — high, `file`, kind `Polygon`

A face that is not a real polygon: it has one or two vertices, or it uses the same
vertex two times, or it cannot be cut into triangles. RizomUV unfolds it alone, as a
very small island.

Case: `face`, `faceLocal`, `vertices` (the list of corners as in the file, with the
vertex used two times: this is the problem), `verticesLocal`, `reason` (`one vertex`,
`two vertices`, `repeated vertex or no triangulation`).

Fix: delete the face, or remove the vertex that is used two times from its list.

Its vertices are not counted again in ZeroAreaFaces or in the measures.

### ZeroAreaFaces — medium, `file`, kind `Polygon`

A face that is flat like a line: one of its triangles has no area, or an angle of zero
(a very thin face). RizomUV moves its vertices a very small distance in its own copy, so
that the unfold can work. The UVs around it can stretch.

Case: `face`, `faceLocal`, `vertices`.

Fix: delete the face, or move its vertices apart.

### FlippedNormals — *Flipped normals* — medium, `file`, kind `Polygon`

A face that points the other way, compared to the faces around it, on a surface that
can be oriented (see *Winding, orientation and normals* above). Along an edge, its
vertices go in the same direction as the vertices of the face next to it. So its normal
points to the other side.

The right side is the side of most of the connected faces. When the file has normals,
they decide. RizomUV unfolds the face the right way. The 3D software shows it as a back
face.

Case: `face`, `faceLocal`, `vertices` (the list of corners, in the order of the file:
this is the order to reverse).

Fix: reverse the winding of the face (*Reverse* in Maya, *Flip* in Blender). Or fix the
normals of the whole object (*Conform* in Maya, *Recalculate Outside* in Blender).

### IsolatedVertices — low, `file`, kind `Vertex`

A vertex that no face uses. No effect on the UVs.

Case: `vertex`, `position`, and `object` / `vertexLocal` when the vertex is inside the
range of vertices of an object. The vertex has no face, so this object is a guess: see
*Limits*.

Fix: delete the vertex.

### NonPlanarFaces — medium, `measure`, kind `Polygon`

A face with 4 vertices or more that is not flat: one vertex is out of the plane of the
face by more than `parameters.maxNonPlanarDeg`.

`deviationDeg` is the biggest angle between a vertex and this plane, seen from the
center of the face. A few degrees is a small fold. Tens of degrees is a big fold.

The software that uses the mesh later can cut this face into triangles in a different
way than RizomUV. Then the stretch you see in RizomUV is not the stretch in the final
render.

Case: `face`, `faceLocal`, `vertices`, `deviationDeg`.

Fix: cut the face into triangles, or make it flat.

### OverlappingFaces — high, `measure`, kind `Polygon`

A face exactly on top of another face, corner on corner. The two faces can point the
same way or opposite ways. Almost always, it is a **copied mesh** (an object copied at
the same place) or a copied face.

The copy has its own vertices, so no topology check finds it. Each copy is unfolded as
its own island, on top of the other.

Case: `face`, `faceLocal`, `vertices`, `overlaps` (the faces under or over it),
`overlapsLocal`. **Each pair is in the list two times**, one time from each side: count
the pairs, not the cases.

Fix: delete one copy of each pair. For a copied mesh, delete the whole copied object (all
the faces of one object are on top of the faces of another object). Then merge the
vertices that are at the same place.

### ComplexPoles — low, `measure`, kind `Vertex`

A vertex with `parameters.poleMinEdges` edges or more. `valence` is its number of edges.
The UVs are pinched around it. A seam through it makes them better.

Case: `vertex`, `vertexLocal`, `position`, `valence`, `faces`, `facesLocal`.

Fix: not always needed. Change the edges so that the vertex has fewer of them, or put a
seam through it.

### Valence2Vertices — low, `measure`, kind `Vertex`

A vertex with only two edges. It can be on a border (`onBorder: true`: the vertex is on
a straight line between its two neighbours) or inside the surface. It is often left
after a boolean or a bevel. No effect on the UVs.

Case: `vertex`, `vertexLocal`, `position`, `neighbours` [a, b], `neighboursLocal`,
`onBorder`, `faces`, `facesLocal`.

Fix: dissolve the vertex (its two edges become one edge).

## How to read a report

Rules for an assistant:

1. **Check `format` and `formatVersion` first.** Skip the fields you do not know.
2. **Count problems, not cases, when the cases come in pairs.** OverlappingFaces lists
   each pair two times. Say "3 copied faces", not "6".
3. **One problem can appear in several checks.** A Moebius strip is in
   InconsistentNormals (non-orientable edges), and its faces are also in FlippedNormals
   and NonPlanarFaces. A face written two times is in NonManifoldVertices (its corners)
   and in OverlappingFaces. Explain the real cause only one time.
4. **Group by object** (`objects[instance.object].name`): users think in objects. When
   a whole object is in OverlappingFaces, it is a copied object.
5. **Keep `file` and `measure` apart.** `file` problems are errors to fix before the
   UVs. `measure` results are quality hints. They depend on the limits in `parameters`,
   and they are not always worth a fix.
6. **Never say that RizomUV fixed something.** It works around the problem for the UVs.
   The file and the export still have it.
7. **Fix in an order that keeps the IDs correct.** Use one report, made on the mesh
   before any change. First, do the fixes that do not change the numbers (reverse faces,
   move vertices). Do the deletes last, from the highest ID to the lowest. Then make a
   new report to check.
8. **In a 3D software, use the local IDs** (`faceLocal`, `vertexLocal`, on the mesh
   given by `object`). For OBJ, use the global IDs: OBJ counts for the whole file.
9. **FlippedNormals and InconsistentNormals are about winding, not about the normals of
   the file.** They report faces whose vertex order does not agree with the faces around them (see
   *Winding, orientation and normals*). Do not tell a user to edit or unlock normals for
   them. Do not use Conform for InconsistentNormals: only a cut fixes it.

A short explanation for a user, made from a report, can look like this:

> *crate.fbx* has 3 problems that change the seams: 1 non-manifold edge on **Lid** (3
> faces on one edge), and **Handle_copy** is exactly on top of **Handle** (a copied
> object). Delete *Handle_copy*, keep two faces on the Lid edge, then check again. The
> 482 complex poles are not errors.

## Limits

- **Local vertex IDs need the vertices of each object to be together.** `vertexBase` is
  the lowest vertex ID that the faces of the object use. FBX and USD save the points of
  each mesh as one block, so local = global − base is correct. It is wrong only when the
  first points of a mesh are used by no face. OBJ counts the vertices for the whole
  file: use the global ID there.
- **Isolated vertices** get the object whose block of vertices starts last, at or before
  them. This is correct for files that save the points of each object together.
- **glTF**: before the checks, the loader welds the points at the same position and
  removes the points that no face uses. So a glTF report has no isolated vertices and no
  bow-ties.
- **Overlap tolerance**: one millionth of the diagonal of the bounding box of the model.
  Faces that touch exactly are found. Faces that are only close are not.
