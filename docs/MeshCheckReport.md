# The Mesh Check report

The Mesh Check report is a JSON file in which RizomUV lists what is wrong with a 3D mesh
before UV work: every case of every check, named by the **file's own** vertex and face
IDs. It is written for two kinds of readers:

- a **plugin** in the application the mesh came from (Maya, Blender, 3ds Max, Houdini,
  Cinema 4D...), which finds each problem on its own mesh and fixes it;
- an **AI assistant**, which reads the report to explain the problems to a user, decide
  what to fix and in what order, or write that plugin. The sections *How to read a
  report* and *Interpreting each check* are written for it.

A JSON Schema of the format is [meshcheck.schema.json](meshcheck.schema.json). RizomUV
never changes the 3D it exports: everything in a report is still in the file.

## Producing a report

From the RizomUV interface: **Window > Mesh Checker**, then **Export JSON...**

From Python, through RizomUVLink (a RizomUV with no window, `headless=True`, works the
same):

```python
from RizomUVLink import CRizomUVLink

link = CRizomUVLink()
link.RunRizomUV(headless=True)
link.Load({"File": {"Path": "C:/models/crate.fbx", "XYZUVW": True}})
link.MeshCheck({"File": "C:/models/crate.meshcheck.json",   # written by the RizomUV process
                "MaxNonPlanarDeg": 2.0,                       # optional, default 2
                "PoleMinEdges": 6})                           # optional, default 6
link.Quit({})
```

`MeshCheck` also returns, without `File`, the counts the panel shows. A full example is
[examples/MeshCheck.py](../examples/MeshCheck.py).

## The contract

1. **IDs are the reference.** Vertex and face IDs are 0-based and global, in the order
   the file stores them: the OBJ `v` and `f` lines (OBJ itself counts from 1: add 1); the
   meshes of an FBX, USD or glTF file one after the other.
2. **Local IDs say where it is in one mesh.** Every face reference also comes local to
   its object (`faceLocal`, `facesLocal`, `overlapsLocal`), every vertex reference too
   (`vertexLocal`, `verticesLocal`, `neighboursLocal`). `object` is an index into
   `objects`. For a vertex, local = global − `objects[object].vertexBase`. A DCC
   addresses its meshes this way.
3. **Positions are a check, not the reference.** They are in the file's axis system
   (`source.upAxis`) and unit (`source.unit`), as the file stores them. Use them to find
   an element again if the mesh was edited since the report, or to show it.
4. **A face's `vertices` is its corner list exactly as the file has it**, repeats
   included.
5. **The report describes the file as it was loaded.** Any edit to the mesh renumbers
   things: produce a new report after fixing, rather than reusing an old one.

## Top level

| Field | Meaning |
|---|---|
| `format` | always `"rizomuv.meshcheck"` |
| `formatVersion` | `1`. A later version only adds fields; a reader ignores the ones it does not know |
| `formatDoc`, `schema` | where this document and the JSON Schema are |
| `generator` | the RizomUV version that wrote it |
| `date` | when, ISO 8601, UTC |
| `source` | `file` (the path loaded), `upAxis` (`x`, `y` or `z`), `unit` (as RizomUV names it: `mm`, `cm`, `m`, `in`...), `vertexCount`, `faceCount` |
| `conventions` | the contract above, in words |
| `parameters` | the thresholds of the measures: `maxNonPlanarDeg`, `poleMinEdges` |
| `objects` | the objects that have faces: `name`, `faceCount`, `vertexBase` (the global ID of its first vertex), `vertexCount` (the span of global IDs its faces use) |
| `checks` | all the checks below, always all of them, always in that order |

Each check: `id`, `category`, `label`, `kind` (`Edge`, `Vertex` or `Polygon`: what an
instance points at), `uvEffect` (two or three words), `description`, `suggestedFix`,
`count` (always the number of instances) and `instances`.

`category` is **`file`** for a problem in the topology as the file holds it — RizomUV
works around it when it unfolds, the file keeps it — and **`measure`** for a property
measured against a threshold, which is a matter of quality, not an error.

## Winding, orientation and normals

Two checks — `FlippedNormals` and `InconsistentNormals` — are about the orientation of
the faces. They carry the names every DCC uses, which speak of *normals*; what they
check is the *winding*. This section explains the difference, because a plugin or an
assistant that confuses the two will apply the wrong fix.

### Winding

A face lists its vertices in an order: that order is its **winding**. Looking at a face
from the side where its vertices turn counter-clockwise, you look at its **front**. So
the winding decides which side of the face is the front. Reverse the list, and the front
and the back swap.

### The normal that follows from the winding

The **normal** of a face, in this sense, is the direction its front points to. It is
not stored anywhere: it follows from the winding and the positions. Reverse the winding,
and this normal flips. That is why every DCC talks about "flipped normals" when a face
is turned the wrong way, and fixes it by reversing the face: the normal is how the
problem shows on screen (a black or back-facing polygon), the winding is where it is.

### The normals of the file are something else

A file can also store **normals** of its own (OBJ `vn`, FBX and USD normals): one
direction per corner, used for shading. They are data, set by the modeller or a tool,
and can point anywhere. **The two checks do not report them.** They read only the
winding and how faces share their edges: topology, not directions.

The file's normals play one part: when RizomUV has to decide which way a surface faces
(below), it lets them vote — a face whose winding agrees with its file normals counts
for its side. Without file normals, the majority of the faces decides.

### Consistent orientation, and flipped normals

Two neighbouring faces are **consistently oriented** when they run along their shared
edge in **opposite** directions: their fronts are then on the same side of the surface.
On a cube whose faces all face outward, every edge is run once each way.

If one face of that cube is written the other way round, the edges around it are run
twice the same way: its front is inside, the others' outside. The surface can still be
made consistent — reverse that one face. That is **`FlippedNormals`**: a face wound
against its neighbours, fixed by reversing it. RizomUV reverses it in its own copy and
reports it; your file keeps it.

### Surfaces that cannot be oriented

Some surfaces cannot be made consistent at all. A **Moebius strip** is a ribbon closed
with a half twist: start on a face, front up, and walk from face to face keeping the
same front side; after one turn you arrive on the **other** side of the face you started
from. Whatever faces you reverse, at least one edge stays run twice the same way —
reversing only moves the conflict to the next edge. Such a surface is
**non-orientable**: it has no front and back.

That is **`InconsistentNormals`**, labelled *Non-orientable edges*: the edges left in
conflict after RizomUV oriented the surface as best it could. A literal Moebius strip is
rare; the usual causes are a weld that closed a tube or a ring with a half turn, or a
merge of vertices that glued the front of one part to the back of another. The fix is to
cut the surface (or rebuild it); RizomUV unfolds those edges as cuts.

### Telling the two apart

| | `FlippedNormals` — *Flipped normals* | `InconsistentNormals` — *Non-orientable edges* |
|---|---|---|
| What is wrong | one face, or a few, turned the wrong way | the shape itself has no front and back |
| Can reversing faces fix it? | yes | no, never |
| What the instance points at | faces | edges |
| What RizomUV does | turns them, no seam added | cuts there: a seam nobody drew |
| Fix in the DCC | Conform (Maya), Unify (3ds Max), Recalculate Outside (Blender), or Reverse / Flip | cut or rebuild the surface; Conform and the like cannot succeed |

On a non-orientable surface RizomUV may also have reversed some faces while trying:
they show in FlippedNormals too. There, the edge is the problem, not those faces.

### What the two checks do not see

- **A whole object inside out**, every face consistently wound but facing inward: no
  neighbour disagrees, so nothing is reported. Most DCCs call that "flipped normals" too.
- **A face whose winding disagrees with its own file normals** while agreeing with its
  neighbours: nothing in the topology is wrong, so nothing is reported.

Both are disagreements between the winding and the file's normals, a check of
directions these two checks do not make.

### The same problems in the DCCs

| DCC | How it names the problem | What fixes it |
|---|---|---|
| Blender | *Non Contiguous* (Select All by Trait): the edges between two faces with opposite normals — the border of flipped faces, and non-orientable edges | Recalculate Outside, Flip |
| Maya | no named check; "normals not facing the same way" | Conform (majority side), Reverse |
| 3ds Max | "inconsistent normals", some outward and some inward; xView *Face Orientation* | Unify, Flip |
| Houdini | "inconsistently wound polygons" (PolyDoctor) | *Correct Winding of Polygons to Majority of their Manifold Patch* |

No DCC names the non-orientable case: their tools simply fail on it.

## Vocabulary

| Word | Meaning |
|---|---|
| **vertex, edge, face** | a point, a segment between two vertices, a polygon. RizomUV says *polygon* in its interface, the report says *face* |
| **winding** | the order in which a face lists its vertices; it decides which side is the front |
| **normal** | the direction the front of a face points to, as the winding implies. Not the normals stored in the file |
| **consistently oriented** | two neighbouring faces run along their shared edge in opposite directions |
| **non-orientable** | a surface no winding can make consistent, like a Moebius strip |
| **manifold** | every edge has at most two faces, and the faces around each vertex form one fan |
| **fan** | the faces around a vertex that follow one another, two neighbours sharing an edge |
| **border** | an edge with only one face |
| **valence** | the number of edges a vertex has |
| **island, seam** | in the UVs, a piece unfolded on its own, and the cut edges that bound it |

## Interpreting each check

Severity, for a user who wants clean UVs: **high** changes the seams or islands behind
their back; **medium** degrades or duplicates the result; **low** is information or a
modelling taste.

### NonManifoldEdges — high, `file`, kind `Edge`

An edge shared by more than two faces: a fin, a T-shaped junction, two shells glued on
one edge. RizomUV cannot unfold it as one surface, so it unfolds it as if it were cut
there: **a seam the user never drew**.
Instance: `vertices` [a, b] and `verticesLocal`, `positions` of both ends, `faces` (every
face on the edge, three or more), `facesLocal`.
Fix: keep two of the faces on that edge; detach the others (they become their own
shell) or delete them if they are leftovers.

### InconsistentNormals — *Non-orientable edges* — high, `file`, kind `Edge`

An edge where the two faces can **never** be oriented consistently: the surface has no
front and back (a non-orientable surface, such as a Moebius strip). RizomUV first
orients every surface as best it can, reversing the faces that disagree (those become
FlippedNormals); the edges left are the ones no reversing can fix. Reversing faces only
moves the conflict to another edge. RizomUV unfolds the edge as if it were cut there:
a seam the user never drew. Instance: same fields as NonManifoldEdges.
Fix: cut the surface along the edge (a real seam, or detach), or rebuild that part.
**Not** reversing faces, and not *Conform* / *Recalculate*, which cannot succeed here.
Expect the faces of such a surface to show also in FlippedNormals (RizomUV reversed some
of them while trying) and, the surface being twisted, in NonPlanarFaces.

### NonManifoldVertices — high, `file`, kind `Vertex`

A vertex where faces do not form one continuous surface: two surfaces touching at that
single vertex (a bow-tie), faces sharing it along borders that do not follow each other,
or a face written twice on the same vertices. RizomUV unfolds each part as a separate
island. Instance: `vertex`, `vertexLocal`, `position`, `faces` (all the faces using it)
and **`fans`**: those faces grouped by the edges they share around the vertex. Two or
more fans means two or more surfaces meet there.
Fix: split the vertex into one per fan (each fan keeps its own copy), or connect the
surfaces with a real edge. For a duplicate face, delete one copy: it also shows in
OverlappingFaces.

### DegenerateFaces — high, `file`, kind `Polygon`

A face with one or two vertices, a vertex used twice, or no possible triangulation.
RizomUV unfolds it apart, as a tiny island of its own. Instance: `face`, `faceLocal`,
`vertices` (the raw corner list, repeats included: the repeat is the problem),
`verticesLocal`, `reason` (`one vertex`, `two vertices`, `repeated vertex or no
triangulation`).
Fix: delete the face, or remove the repeated vertex from its corner list.
Its vertices are not counted again in ZeroAreaFaces or the measures.

### ZeroAreaFaces — medium, `file`, kind `Polygon`

A face with a triangle of no area, or with a zero angle (a sliver, a needle). RizomUV
moves its vertices by a tiny amount in its own copy so its solvers can run: the UVs
around it can stretch. Instance: `face`, `faceLocal`, `vertices`.
Fix: collapse or delete the face, or move its vertices apart.

### FlippedNormals — *Flipped normals* — medium, `file`, kind `Polygon`

A face oriented against the faces around it, on a surface that can be oriented (see
*Winding, orientation and normals* above): its winding runs the same way as a
neighbour's along their shared edge, so its normal points to the other side. Which side is right is decided by the majority of the connected faces,
or by the file's own normals when it has some. RizomUV unfolds it the right way round;
the DCC shows it as a back face. Instance: `face`, `faceLocal`, `vertices` (the corner
list in the file's order, the order to reverse).
Fix: reverse the face's winding (Maya *Reverse*, Blender *Flip*), or conform the normals
of its object (Maya *Conform*, Blender *Recalculate Outside*).

### IsolatedVertices — low, `file`, kind `Vertex`

A vertex used by no face. No effect on the UVs. Instance: `vertex`, `position`, and
`object` / `vertexLocal` when it falls inside an object's block of vertices (it belongs
to no face, so this is inferred: see the caveats).
Fix: delete the vertex.

### NonPlanarFaces — medium, `measure`, kind `Polygon`

A quad or n-gon with a vertex off its mean plane by more than
`parameters.maxNonPlanarDeg`. `deviationDeg` is the largest angle under which a vertex
leaves the plane, seen from the face's centre: a few degrees is a gentle fold, tens of
degrees a bad one. The application the mesh goes to may triangulate it differently from
RizomUV, so the distortion seen in RizomUV is not the one rendered. Instance: `face`,
`faceLocal`, `vertices`, `deviationDeg`.
Fix: triangulate the face, or flatten it.

### OverlappingFaces — high, `measure`, kind `Polygon`

A face lying on another one, corner on corner, facing the same way or the opposite one:
almost always a **doubled mesh** (an object duplicated in place) or a duplicated face.
The copy has vertices of its own, so no topology check sees it; each copy unfolds as an
island of its own, stacked on the other. Instance: `face`, `faceLocal`, `vertices`,
`overlaps` (the faces it lies on), `overlapsLocal`. **Each pair appears twice**, once
from each side: count pairs, not instances.
Fix: delete one copy of each pair — for a doubled mesh, the whole duplicated object
(the faces of one object all overlap faces of another) — then merge the vertices left
on the same positions.

### ComplexPoles — low, `measure`, kind `Vertex`

A vertex with `parameters.poleMinEdges` edges or more; `valence` is its edge count. The
UVs pinch around it; a seam through it relaxes them. Instance: `vertex`, `vertexLocal`,
`position`, `valence`, `faces`, `facesLocal`.
Fix: optional. Reroute edges to lower the valence, or plan a seam through the pole.

### Valence2Vertices — low, `measure`, kind `Vertex`

A vertex with only two edges: in the middle of a border (`onBorder: true`, a collinear
vertex) or inside the surface (a doublet). Often left over from a boolean or a bevel. No
effect on the UVs. Instance: `vertex`, `vertexLocal`, `position`, `neighbours` [a, b],
`neighboursLocal`, `onBorder`, `faces`, `facesLocal`.
Fix: dissolve the vertex (its two edges merge into one).

## How to read a report

Rules an assistant should follow:

1. **Check `format` and `formatVersion`** before anything; ignore unknown fields.
2. **Count problems, not instances, where they pair up**: OverlappingFaces lists each
   pair twice. Report "3 doubled faces", not "6".
3. **Expect one problem to show in several checks.** A Moebius strip is
   InconsistentNormals (non-orientable edges), and its faces also show in FlippedNormals
   (flipped normals) and NonPlanarFaces; a
   face written twice is NonManifoldVertices (its corners) and OverlappingFaces. Explain
   the root cause once.
4. **Group by object** (`objects[instance.object].name`): users think in objects. A whole
   object in OverlappingFaces is a doubled object.
5. **Separate `file` from `measure`.** `file` problems are errors to fix before UV work;
   `measure` results are quality hints that depend on the thresholds in `parameters`
   and are not always worth fixing.
6. **Never say RizomUV fixed something.** It works around the problem for the UVs; the
   file and any export still have it.
7. **Fix in an order that keeps IDs valid.** Work from one report on the unchanged mesh;
   apply the fixes that do not renumber (reversing faces, moving vertices) first; delete
   last, from the highest ID down. Then produce a new report to confirm.
8. **Prefer local IDs inside a DCC** (`faceLocal`, `vertexLocal` on the mesh named by
   `object`); use global IDs for OBJ, whose numbering is global to the file.
9. **Read FlippedNormals and InconsistentNormals as winding, not as the file's
   normals.** They report how faces are wound against their neighbours; the normals the
   file stores are not what they check (see *Winding, orientation and normals*). Do not
   tell a user to edit or unlock normals for them, and do not apply Conform to
   InconsistentNormals: only a cut fixes it.

A short explanation for a user, built from a report, looks like:

> *crate.fbx* has 3 problems that change the seams: 1 non-manifold edge on **Lid** (a
> fin of 3 faces), and **Handle_copy** lies exactly on **Handle** (a doubled object).
> Delete *Handle_copy*, keep two faces on the Lid edge, then check again. The 482 complex
> poles are not errors.

## Caveats

- **Local vertex IDs rest on contiguity.** `vertexBase` is the lowest vertex ID the
  object's faces use. FBX and USD store each mesh's points as one block, so local =
  global − base holds — unless the mesh's first points are used by no face, which
  shifts it. OBJ numbers vertices for the whole file: use the global ID there.
- **Isolated vertices** are given the object whose block of vertices starts last at or
  before them; true for files that store each object's points together.
- **glTF**: the loader welds equal positions and drops unused points before the checks
  run, so a glTF report has no isolated vertices and no bow-ties.
- **Overlap tolerance** is a millionth of the model's bounding-box diagonal: faces
  meant to touch exactly are found, faces merely close are not.
