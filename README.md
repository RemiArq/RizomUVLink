# RizomUVLink

An open source Python module to control a RizomUV Standalone instance from a DCC.

## Description

RizomUVLink is a Python module made to control a RizomUV Standalone instance from
any Python-capable DCC, or from any Python program.

It consists of a set of compiled Python modules, some third-party compiled
libraries and some Python code. The compiled modules exist in several versions,
one per Python version and per OS; the package picks the right one automatically
at import time, so the same folder works on every supported platform.

Features:

* Control a RizomUV instance through its API directly from Python code running in the DCC
* Create "live link" type bridges using fast in-memory mesh transfers from the DCC to RizomUV and vice versa
* Helper methods for the most common tasks, e.g. launching the most recent RizomUV version installed on the OS
* Connection-loss detection (a RizomUV crash for instance), preventing infinite loops in the DCC's plugin

The main principle: commands and data are emitted through the RizomUVLink module
and transferred (by IPC) to a designated running RizomUV Standalone instance.
Data present in the RizomUV instance can also be retrieved through RizomUVLink.

## Compatibility

One compiled module per Python version and per OS. The module runs inside the
host's Python (a DCC's embedded interpreter, or any CPython), and the package
selects the right binary for the running interpreter automatically.

| Python | Windows x64 | Linux x86_64 | macOS |
|:------:|:-----------:|:------------:|:-----:|
| 3.6    | ✔           | —            | —     |
| 3.7    | ✔           | —            | —     |
| 3.8    | ✔           | —            | —     |
| 3.9    | ✔           | ✔            | ✔     |
| 3.10   | ✔           | ✔            | ✔     |
| 3.11   | ✔           | ✔            | ✔     |
| 3.12   | ✔           | ✔            | ✔     |
| 3.13   | ✔           | ✔            | ✔     |

Platform notes:

* **Windows** (`win/*.pyd`): the ZeroMQ and libsodium DLLs ship in the same folder, nothing to install.
* **Linux** (`linux/*.so`): ZeroMQ is compiled in, no `pyzmq` or system package needed. Built on an Enterprise Linux 8 base, so any distribution with glibc 2.28 or newer works.
* **macOS** (`mac/*.so`): universal2 binaries (Apple Silicon and Intel, macOS 10.15+), so they load both in native hosts and in x86_64 hosts running under Rosetta. ZeroMQ is compiled in. Python 3.6–3.8 do not exist as arm64 builds, hence the gap.

Tell us if you need another Python version or platform combination.

## Getting started

### Dependencies

* RizomUV Standalone version 2026.0 or later, available at https://rizomuv.com
* One of the Python versions of the matrix above

### Where to find the package

The `RizomUVLink` folder ships with RizomUV itself — installing RizomUV is enough:

| OS | Location |
|---|---|
| Windows | `<RizomUV installation directory>\RizomUVLink` |
| macOS | `RizomUV.<version>.app/Contents/Resources/RizomUVLink` (since RizomUV 2026.0) |
| Linux | inside the AppImage at `usr/bin/RizomUVLink` (since RizomUV 2026.0) — run `./RizomUV.<...>.AppImage --appimage-extract` to get a copy, or `--appimage-mount` to read it in place |

Cloning this repository gives the exact same package on any OS.

### Locating the package from code

A plugin cannot ask RizomUVLink where RizomUVLink is before importing it, so
here is the bootstrap: compute the package path of the most recent RizomUV
installation, then import. On Windows every setup registers its installation
directory; on macOS the versioned bundles sit in `/Applications`.

```python
def RizomUVLinkDir():
    """Absolute path of the RizomUVLink package shipped with the most recent
       RizomUV installation, or None when none is found. On Linux there is no
       registration mechanism: keep a copy of the package (extracted from the
       AppImage, or cloned from this repository) and point sys.path at it."""
    import os
    import platform
    import re
    from pathlib import Path

    if platform.system() == "Windows":
        import winreg
        for major in range(2029, 2021, -1):
            for minor in range(10, -1, -1):
                if major == 2022 and minor < 2:
                    continue  # RizomUVLink ships since RizomUV 2022.2
                key_path = "SOFTWARE\\Rizom Lab\\RizomUV VS RS %d.%d" % (major, minor)
                try:
                    key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path)
                    exe_path = winreg.QueryValue(key, "rizomuv.exe")
                    return os.path.join(os.path.dirname(exe_path), "RizomUVLink")
                except FileNotFoundError:
                    pass
    elif platform.system() == "Darwin":
        bundles = list(Path("/Applications").glob("RizomUV*.app"))
        if bundles:
            newest = max(bundles,
                         key=lambda p: [int(n) for n in re.findall(r"\d+", p.name)])
            return str(newest / "Contents" / "Resources" / "RizomUVLink")
    return None


import sys
sys.path.insert(0, RizomUVLinkDir())
from RizomUVLink import *
```

### Minimal script

Add the package folder to Python's module search path and import; everything else
is regular RizomUV scripting (same commands and parameters as the scripting
documentation shipped with RizomUV):

```python
import sys
# or compute it: see "Locating the package from code" above
sys.path.insert(0, r"<path to the RizomUVLink folder>")

from RizomUVLink import *

link = CRizomUVLink()

# Launches the most recent RizomUV Standalone installed on the OS and connects
# to it. On Windows and macOS the executable is found automatically; on Linux
# (and to pin a specific install anywhere) pass its path explicitly:
#   link.RunRizomUV(exePath="/path/to/RizomUV")
port = link.RunRizomUV()
print("RizomUV " + link.RizomUVVersion() + " listens on TCP port " + str(port))

link.Load({"File": {"Path": "mesh.obj", "XYZUVW": True, "ImportGroups": True,
                    "UVWProps": True}})
link.Unfold({"WorkingSet": "Visible", "PrimType": "Island"})
link.Save({"File": {"Path": "mesh_unwrapped.obj", "UVWProps": True}})
link.Quit({})
```

Keep the `link` object alive as long as possible: it stays associated with its
RizomUV instance, so new meshes and commands need neither a new launch nor a new
initialization wait.

### Connecting to an instance you launched yourself

`RunRizomUV()` does launch + connect in one call, but the two halves are also
available separately: start RizomUV with `-id <port>` on its command line, then
`link.Connect(port)` from Python.

### Detecting changes on the RizomUV side (optional)

Everything in RizomUVLink works by request/response: you send a command, you
read the result when you decide. A batch script, or a bridge that round-trips
the mesh on the user's demand, needs nothing more — this whole section can be
ignored.

Change detection only serves one particular style of bridge: the kind that
leaves RizomUV open in front of the user and mirrors their edits back into the
DCC as they happen. For that case, two mechanisms, from the simplest to the
most comfortable:

* **Polling** — `GetVersion("Lib.Mesh.UVW")` returns an integer change-token for
  a data-tree path *without transferring any data*: poll it at low frequency and
  pull the data (`Get()` / `Save()`) once the token changes. The token only moves
  when the value actually changed, so idle polls are stable. See
  **examples/PollingChanges.py**.
* **Push notifications** — `Subscribe({"Paths": ["Lib.Mesh.UVW", ...]})`
  registers the paths to watch; RizomUV then publishes a notification on a
  dedicated channel (the command port + 1) whenever one of them changes, and
  `StartNotificationListener(port, callback)` runs a background thread that calls
  you back on each change — no polling loop, and no external dependency (the
  subscriber socket is built into the module). The channel is optional and
  push-only: it never interferes with the command channel, and a program that
  never calls `Subscribe` is unaffected. See **examples/ChangeNotifications.py**.

Notifications carry no mesh data — they only say "this path changed"; read the
actual data with `Get()` / `Save()` once notified.

### Good to know

* `RunRizomUV(background=True)` opens RizomUV *behind* the host application, so
  the window being worked in keeps the focus (needs RizomUV 2026.0.297 or later).
* Several RizomUV instances can run simultaneously — one `CRizomUVLink` object
  each. Beware with floating licenses: every instance takes a license token.
* Errors raised on the RizomUV side reach Python as a `CZEx` exception.

## Help

Have a look at the **examples** folder, especially at **Simple.py**. The full
command reference is the scripting documentation shipped with RizomUV.

## Authors

Remi Arquier from Rizom-Lab.

remi.arquier at rizomuv.com

https://www.rizomuv.com

## License

This project is licensed under the **MIT License** - see the LICENSE.md file for details

MIT License is a short and simple permissive license with conditions only requiring preservation of copyright and license notices. Licensed works, modifications, and larger works may be distributed under different terms and without source code. It allows a commercial use.

## Acknowledgments

* [ZeroMQ](https://zeromq.org/)
