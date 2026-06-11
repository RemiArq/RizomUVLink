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

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#
# Detecting changes by POLLING GetVersion
# ---------------------------------------
#
# Every value in RizomUV's data tree carries an integer change-version. The
# GetVersion command returns that counter for a given path. It is an opaque
# token: compare it across calls to know whether the value changed, WITHOUT
# transferring the value itself (no mesh data crosses the link).
#
# Typical live-link / bridge use: poll a few interesting paths at a LOW
# frequency; when a counter differs from the previous poll, that path changed
# in RizomUV (e.g. the user edited the UVs or the selection in the GUI), so the
# bridge knows it is time to pull the data back with Get()/Save().
#
# Properties worth knowing:
#   * The node is synced from the live state before the counter is read, so a
#     change made in the GUI is detected even though the bridge never read the
#     value. No false positives at rest: an idle poll returns the same token.
#   * Each call costs a small server-side recompute of that node (no data is
#     sent) - so poll at a low frequency (a few times per second is plenty).
#   * The token is opaque: compare for INEQUALITY only. Do not assume it grows
#     or has any meaning beyond "different => changed".
#   * For a whole subtree (a table whose own counter does not track a
#     descendant's value change) use the recursive form:
#         link.GetVersion({"Path": "Lib.Mesh", "Recursive": True})
#
# If you would rather NOT run a polling loop at all, use the push channel:
# Subscribe({"Paths": [...]}) then StartNotificationListener(port, callback).
# RizomUV then PUSHES a notification when a subscribed path changes, so no busy
# loop is needed. Polling (this file) is the simplest approach and needs nothing
# beyond GetVersion.
#
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

import sys
import os

from os.path import dirname


def RizomUVWinRegisterInstallPath():
    """ Returns the path to the most recent version
        of the RizomUV installation directory on the system using
        the Windows registry.

        Look for versions from 2029.10 to 2022.2 included
    """
    import winreg

    for i in range(9, 1, -1):
        for j in range(10, -1, -1):
            if i == 2 and j < 2:
                continue
            path = "SOFTWARE\\Rizom Lab\\RizomUV VS RS 202" + str(i) + "." + str(j)
            try:
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path)
                exePath = winreg.QueryValue(key, "rizomuv.exe")
                return os.path.dirname(exePath)
            except FileNotFoundError:
                pass

    return None


# add the RizomUVLink installation path to the Python module search paths
sys.path.append(RizomUVWinRegisterInstallPath() + "/RizomUVLink")

# import all RizomUVLink module items
from RizomUVLink import *


# Paths we want to watch. UVW = the UV coordinates, SelectedPolyEdgeIDs = the
# current edge selection. Add or remove paths to fit your bridge.
WATCHED_PATHS = [
    "Lib.Mesh.UVW",
    "Lib.Mesh.SelectedPolyEdgeIDs",
]


def read_versions(link, paths):
    """ Return {path: version} for the given paths (one GetVersion call each). """
    return {p: link.GetVersion(p) for p in paths}


def poll_changes(link, last_versions):
    """ Re-read the watched versions and return (changed_paths, new_versions).

        'changed_paths' lists the paths whose version differs from last_versions.
        Feed 'new_versions' back in on the next call. """
    changed = []
    current = read_versions(link, last_versions.keys())
    for path, version in current.items():
        if version != last_versions[path]:
            changed.append(path)
    return changed, current


# create a rizomuvlink object instance and run / connect RizomUV standalone
link = CRizomUVLink()
print("RizomUVLink " + link.Version() + " instance has been created")

port = link.RunRizomUV()
print("RizomUV " + link.RizomUVVersion() + " is now listening on TCP port: " + str(port))

try:
    import time

    # Load a mesh so the watched paths exist
    meshInputPath = dirname(__file__) + "/ExampleMesh.obj"
    link.Load({"File.Path": meshInputPath, "File.XYZUVW": True, "__Focus": True})

    # 1) Establish the baseline: remember the current version of each watched path.
    versions = read_versions(link, WATCHED_PATHS)
    print("\nBaseline versions:")
    for p, v in versions.items():
        print("    " + p + " = " + str(v))

    # 2) An idle poll detects nothing (no false positives at rest).
    changed, versions = poll_changes(link, versions)
    print("\nIdle poll -> changed: " + str(changed))   # -> []

    # 3) Edit the UVs (here we Pack from the script; in a real bridge the change
    #    would instead come from the user editing in the GUI). The next poll
    #    detects that Lib.Mesh.UVW moved.
    link.Pack({"Translate": True})
    changed, versions = poll_changes(link, versions)
    print("After Pack   -> changed: " + str(changed))   # -> ['Lib.Mesh.UVW']

    # 4) Change the selection -> SelectedPolyEdgeIDs moves.
    link.Select({"PrimType": "Edge", "All": True, "Select": True, "ResetBefore": True})
    changed, versions = poll_changes(link, versions)
    print("After Select -> changed: " + str(changed))   # -> ['Lib.Mesh.SelectedPolyEdgeIDs']

    # 5) A continuous bridge loop. While RizomUV is used interactively, poll at a
    #    low frequency and react to each change by pulling the data back. This
    #    demo stops after a few seconds; a real bridge would loop as long as the
    #    standalone instance lives.
    print("\nWatching for changes for ~5s (edit UVs/selection in RizomUV to see it react)...")
    deadline = time.time() + 5.0
    while time.time() < deadline:
        time.sleep(0.3)                       # low frequency: ~3 polls / second
        changed, versions = poll_changes(link, versions)
        for path in changed:
            print("    changed: " + path + "  -> a bridge would now pull this data (Get/Save)")

    # Close RizomUV standalone instance associated to link
    link.Quit({})

except CZEx as ex:
    print(str(ex))

print("Done")
