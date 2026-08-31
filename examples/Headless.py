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

"""Unwrap a folder of meshes without RizomUV ever appearing on screen.

`RunRizomUV(headless=True)` starts RizomUV with no window, no panel and no
dialog. Every command works exactly as it does with a window; what changes is
that nothing can stop the run waiting for a click. That is the whole point: in a
batch, a modal box is not a prompt, it is a hang.

Two things to know before using it in anger:

  * A dialog that would have opened is answered and written to the RizomUV
    command log instead. Nothing is silently swallowed, but nothing waits for
    you either -- a question you would have answered "yes" to is answered "no",
    because a script that did not ask for something must not get it.

  * Headless is not displayless. RizomUV still needs a desktop session to start
    (an X display on Linux, a logged-in session on Windows). The flag promises
    that nothing is *drawn*, not that nothing is *needed*.

Needs RizomUV 2027.0.417 or later.

Usage:
    python Headless.py <input folder> <output folder>
    python Headless.py                # runs on the example mesh shipped here

RizomUV is found automatically when it is installed. Set RIZOMUV_EXE to point at
a specific executable instead -- needed on Linux, where there is no install
location to look up, and useful anywhere to pin one build among several.
"""

import os
import sys
import glob
import tempfile
from os.path import dirname, basename, splitext

# Point Python at the RizomUVLink package: this file lives inside it, so its
# parent is the package folder. In your own script, see "Locating the package
# from code" in the README.
sys.path.insert(0, dirname(dirname(os.path.abspath(__file__))))

from RizomUVLink import CRizomUVLink, CZEx

MESH_EXTENSIONS = (".obj", ".fbx", ".usd", ".usda", ".usdc", ".gltf", ".glb")


def unwrap(link, path, outputDir):
    """Load, unfold, pack and save one mesh. Returns True when it worked.

    Each mesh is wrapped on its own: one unreadable file in a folder of two
    hundred should cost you that file, not the batch.
    """
    name = basename(path)
    # Absolute, always. RizomUV runs with its own directory as the working
    # directory, not yours, so a relative path resolves somewhere you did not
    # mean -- and forward slashes, which every platform accepts.
    inputPath = os.path.abspath(path).replace("\\", "/")
    outputPath = os.path.abspath(
        os.path.join(outputDir, splitext(name)[0] + "_unwrapped.obj")).replace("\\", "/")

    try:
        # A Load that fails does NOT raise: it returns {"Error": {"Msg", "Code"}}.
        # That is the contract a bridge codes against, and it has to be read --
        # without this check the run carries on to Unfold and Save, and the
        # failure surfaces several commands later as a confusing "no UV set".
        result = link.Load({"File": {"Path": inputPath,
                                     "XYZUVW": True,
                                     "ImportGroups": True,
                                     "UVWProps": True}})
        if isinstance(result, dict) and "Error" in result:
            print("  FAIL  %-40s %s" % (name, result["Error"].get("Msg", result["Error"])))
            return False

        link.Unfold({"WorkingSet": "Visible", "PrimType": "Island"})
        link.Pack({"WorkingSet": "Visible"})
        link.Save({"File": {"Path": outputPath, "UVWProps": True}})

        islands = link.Count("Lib.Mesh.Islands")
        print("  OK    %-40s %5d islands -> %s" % (name, islands, outputPath))
        return True

    except CZEx as ex:
        # A command that fails on the RizomUV side arrives here as an exception,
        # so the batch can count it and carry on.
        print("  FAIL  %-40s %s" % (name, ex))
        return False


def main(argv):
    inputDir = argv[1] if len(argv) > 1 else dirname(os.path.abspath(__file__))
    outputDir = argv[2] if len(argv) > 2 else tempfile.gettempdir()

    meshes = sorted(p for p in glob.glob(os.path.join(inputDir, "*"))
                    if splitext(p)[1].lower() in MESH_EXTENSIONS
                    and "_unwrapped" not in basename(p))
    if not meshes:
        print("No mesh found in " + inputDir)
        return 1

    os.makedirs(outputDir, exist_ok=True)

    link = CRizomUVLink()

    # exePath=None asks the module to find the installed RizomUV, which works on
    # Windows and macOS. On Linux there is no install location to look up, so the
    # path has to be given -- hence the environment variable, which is also how
    # you pin one build when several are installed.
    port = link.RunRizomUV(exePath=os.environ.get("RIZOMUV_EXE"), headless=True)
    print("RizomUV %s running headless on port %d" % (link.RizomUVVersion(), port))
    print("%d mesh(es) from %s\n" % (len(meshes), inputDir))

    done = 0
    try:
        # One instance for the whole batch. Launching RizomUV is by far the most
        # expensive step here, so it is paid once rather than per mesh -- and on
        # a floating license, one instance is one token.
        for path in meshes:
            if unwrap(link, path, outputDir):
                done += 1
    finally:
        # Always: a headless instance has no window to close, so a script that
        # returns without quitting leaves an invisible process behind.
        link.Quit({})

    print("\n%d/%d unwrapped" % (done, len(meshes)))
    return 0 if done == len(meshes) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
