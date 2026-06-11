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
# Detecting changes with PUSH notifications (Subscribe + listener)
# ----------------------------------------------------------------
#
# Instead of polling GetVersion in a loop (see PollingChanges.py), you can ask
# RizomUV to PUSH a notification whenever a watched data-tree path changes. You
# subscribe once, then a callback fires on every change - no busy loop.
#
#   1. Subscribe({"Paths": [...]}) registers the paths to watch and returns the
#      notification port (the command port + 1). After each task RizomUV checks
#      those paths and publishes [path, version] for the ones that changed, on a
#      dedicated ZeroMQ PUB socket - separate from the command channel.
#
#   2. StartNotificationListener(port, callback) connects to that port (a native
#      SUB socket built into the RizomUVLink module - NO external dependency such
#      as pyzmq), runs a background thread, and calls callback(path, version) on
#      each change. It returns a stop() function to end the listener.
#
# This channel is OPTIONAL and backward compatible: it is push-only and does not
# interfere with the command (REQ/REP) channel, so you can keep sending commands
# while receiving notifications. A program that never calls Subscribe is
# unaffected.
#
# Properties worth knowing:
#   * No mesh data is sent - a notification just says "this path changed". On
#     each callback, pull what you actually need with Get()/Save().
#   * No false positives at rest: a path fires only when its value really changed.
#   * A long operation (e.g. Pack) may fire several notifications, each carrying
#     a real intermediate version. Debounce on your side if you want one event
#     per operation.
#   * PUB/SUB can drop the very first messages before the subscription is fully
#     established: do one full sync right after subscribing, then rely on the
#     notifications.
#   * Run the listener / callback from a single background thread (the one
#     StartNotificationListener creates). Do your heavy work outside the callback
#     if it could be slow.
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


# Paths we want to be notified about. UVW = the UV coordinates,
# SelectedPolyEdgeIDs = the current edge selection.
WATCHED_PATHS = [
    "Lib.Mesh.UVW",
    "Lib.Mesh.SelectedPolyEdgeIDs",
]


# create a rizomuvlink object instance and run / connect RizomUV standalone
link = CRizomUVLink()
print("RizomUVLink " + link.Version() + " instance has been created")

port = link.RunRizomUV()
print("RizomUV " + link.RizomUVVersion() + " is now listening on TCP port: " + str(port))

stop = None
try:
    import time

    # Load a mesh so the watched paths exist
    meshInputPath = dirname(__file__) + "/ExampleMesh.obj"
    link.Load({"File.Path": meshInputPath, "File.XYZUVW": True, "__Focus": True})

    # 1) Subscribe to the paths -> RizomUV returns the notification port.
    notifyPort = link.Subscribe({"Paths": WATCHED_PATHS})
    print("\nSubscribed. Notifications published on port: " + str(notifyPort))

    # 2) Define what to do on each change. This callback runs on the listener's
    #    background thread. Here we just print; a real bridge would pull the data
    #    (e.g. link.Save(...) to round-trip the UVs back to the host application).
    def on_change(path, version):
        print("    [notify] " + path + " changed (version " + str(version) + ")"
              + "  -> a bridge would now pull this data (Get/Save)")

    # 3) Start the background listener. Returns a stop() function.
    stop = link.StartNotificationListener(notifyPort, on_change)

    # Give the subscription a moment to be established (PUB/SUB slow-joiner).
    time.sleep(0.5)

    # 4) Trigger some edits from the script to demonstrate the notifications.
    #    In a real bridge these changes would instead come from the user editing
    #    in the RizomUV GUI - on_change fires either way.
    print("\n-> Pack (changes the UVs)")
    link.Pack({"Translate": True})
    time.sleep(1.0)                       # let the notification(s) arrive

    print("-> Select all edges (changes the selection)")
    link.Select({"PrimType": "Edge", "All": True, "Select": True, "ResetBefore": True})
    time.sleep(1.0)

    # 5) Keep listening while RizomUV is used interactively. Edit the UVs or the
    #    selection in the GUI and watch on_change fire. This demo waits ~5s; a
    #    real bridge would keep the listener alive as long as the instance lives.
    print("\nListening for ~5s (edit UVs/selection in RizomUV to see it react)...")
    time.sleep(5.0)

    # 6) Stop the listener and (optionally) unsubscribe.
    stop()
    stop = None
    link.Subscribe({})                    # empty list -> stop notifications

    # Close RizomUV standalone instance associated to link
    link.Quit({})

except CZEx as ex:
    if stop is not None:
        stop()
    print(str(ex))

print("Done")
