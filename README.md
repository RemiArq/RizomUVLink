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
| Linux | not bundled in the AppImage yet — clone this repository |

Cloning this repository gives the exact same package on any OS.

### Minimal script

Add the package folder to Python's module search path and import; everything else
is regular RizomUV scripting (same commands and parameters as the scripting
documentation shipped with RizomUV):

```python
import sys
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
