# MIT License
#
# Copyright (c) [2026] [Rizom-Lab]
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""Check a mesh before UV work, and list its problems object by object.

RizomUV reads the mesh, writes its Mesh Check report as JSON, and this script reads
the report back: what a plugin in a DCC does before fixing the mesh on its side. The
format is described in ../docs/MeshCheckReport.md, with a JSON Schema next to it.

    python MeshCheck.py path/to/mesh.fbx

The report is written next to the mesh, as <mesh>.meshcheck.json. Needs a RizomUV with
the Mesh Checker (2027.0 builds from November 2026 on).
"""

import json
import os
import sys
from collections import defaultdict

# the package folder, one level up from examples/, as the other examples do
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from RizomUVLink import CRizomUVLink


def check(mesh_path):
    report_path = os.path.splitext(mesh_path)[0] + ".meshcheck.json"

    link = CRizomUVLink()
    # the most recent RizomUV installed, or the one RIZOMUV_EXE names
    link.RunRizomUV(exePath=os.environ.get("RIZOMUV_EXE"), headless=True)
    try:
        link.Load({"File": {"Path": mesh_path, "XYZUVW": True}})
        # The path is written by the RizomUV process: on the same machine, any path works
        link.MeshCheck({"File": report_path})
    finally:
        link.Quit({})

    with open(report_path, encoding="utf-8") as f:
        return json.load(f), report_path


def summarize(report):
    assert report["format"] == "rizomuv.meshcheck"
    objects = report["objects"]
    print("%s: %d vertices, %d faces, up axis %s, unit %s" % (
        report["source"]["file"], report["source"]["vertexCount"], report["source"]["faceCount"],
        report["source"]["upAxis"], report["source"]["unit"]))

    for category, title in (("file", "Problems in the file"), ("measure", "Measures")):
        print("\n" + title)
        for c in report["checks"]:
            if c["category"] != category or c["count"] == 0:
                continue
            # an overlap is listed from both of its faces: count pairs
            count = c["count"] // 2 if c["id"] == "OverlappingFaces" else c["count"]
            by_object = defaultdict(list)
            for inst in c["instances"]:
                name = objects[inst["object"]]["name"] if "object" in inst else "(no object)"
                local = inst.get("faceLocal", inst.get("vertexLocal", inst.get("verticesLocal")))
                by_object[name].append(local)
            print("  %-22s %4d  (%s)" % (c["label"], count, c["uvEffect"]))
            for name, locals_ in sorted(by_object.items()):
                shown = ", ".join(str(l) for l in locals_[:8]) + (" ..." if len(locals_) > 8 else "")
                print("      %-20s %s" % (name, shown))
            print("      fix: " + c["suggestedFix"])


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    report, path = check(os.path.abspath(sys.argv[1]))
    summarize(report)
    print("\nReport: " + path)
