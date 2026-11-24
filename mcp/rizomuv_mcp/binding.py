"""Locate and import the RizomUVLink binding. Only the link worker process calls this:
the binding holds the GIL for a whole command, so the server process never imports it.

The binding is not a pip dependency: it ships inside every RizomUV install
(<install>/RizomUVLink), compiled per Python minor, so a candidate directory is only
accepted when it has the compiled module for THIS interpreter.
"""
import importlib
import importlib.util
import os
import sys
from pathlib import Path

from . import launch, paths

_PLATFORM = {"win32": ("win", ".pyd"), "darwin": ("mac", ".so")}.get(sys.platform, ("linux", ".so"))
_DLL_DIRS = []   # os.add_dll_directory cookies: the directory stays searchable while referenced


def platform_module(directory):
    """<directory>/<win|mac|linux>/rizomuvlink_python<maj><min>.<pyd|so> for this Python."""
    sub, ext = _PLATFORM
    return Path(directory) / sub / ("rizomuvlink_python%d%d%s" % (sys.version_info[0], sys.version_info[1], ext))


def _problem(directory):
    if not (directory / "RizomUVLink.py").is_file():
        return "no RizomUVLink.py"
    module = platform_module(directory)
    if not module.is_file():
        return "no %s/%s (this is Python %d.%d)" % (module.parent.name, module.name, *sys.version_info[:2])
    return None


def candidates():
    """(directory, source) in search order, after "already importable"."""
    env = os.environ.get("RIZOMUV_LINK_DIR")
    if env:
        yield Path(env), "RIZOMUV_LINK_DIR"
    yield paths.package_dir(), "the package this server ships in"
    app_dir = os.environ.get("RIZOMUV_MCP_APP_DIR")
    if app_dir:
        yield Path(app_dir) / "RizomUVLink", "RIZOMUV_MCP_APP_DIR"
    for _, source, install in launch.registry_installs():
        yield install / "RizomUVLink", source
    for bundle in launch.mac_bundles():
        yield bundle / "Contents" / "Resources" / "RizomUVLink", "/Applications"


def _same(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _put_on_path(directory):
    """At the END of sys.path, so its generic top-level names (win, mac, linux, and its
    "mcp" folder) never shadow anything. Unless an earlier entry holds another
    RizomUVLink.py: then just before that entry, or the import would take the wrong one."""
    entry = str(directory)
    if not any(_same(p or ".", entry) for p in sys.path):
        sys.path.append(entry)
    spec = importlib.util.find_spec("RizomUVLink")
    if spec is None or not spec.origin or _same(Path(spec.origin).parent, directory):
        return
    shadow = Path(spec.origin).parent
    sys.path[:] = [p for p in sys.path if not _same(p or ".", entry)]
    index = next((i for i, p in enumerate(sys.path) if _same(p or ".", shadow)), 0)
    sys.path.insert(index, entry)


def _forget_partial_import():
    # the binding's own module names only: this very package may live under the same
    # directory (<RizomUVLink>/mcp/rizomuv_mcp) and must stay imported
    sub = _PLATFORM[0]
    for name in list(sys.modules):
        if name in ("RizomUVLink", "RizomUVLinkBase", sub) or name.startswith(sub + "."):
            del sys.modules[name]


def _import_from(directory):
    _put_on_path(directory)
    if sys.platform == "win32":
        # CPython 3.8+ already searches a .pyd's own directory for its DLLs (libzmq,
        # libsodium sit next to it); this keeps them reachable should that ever change
        _DLL_DIRS.append(os.add_dll_directory(str(directory / _PLATFORM[0])))
    try:
        module = importlib.import_module("RizomUVLink")
    except Exception as e:
        _forget_partial_import()
        raise ImportError("%s: %s" % (type(e).__name__, e)) from e
    if not _same(Path(module.__file__).parent, directory):
        found = module.__file__
        _forget_partial_import()
        raise ImportError("imported %s instead" % found)
    return module


def load_binding():
    """The RizomUVLink module (CRizomUVLink, CZEx). ImportError, listing every place
    tried and why each was refused, when no usable one exists."""
    module = sys.modules.get("RizomUVLink")
    if module is not None and hasattr(module, "CRizomUVLink"):
        return module
    tried, seen = [], set()
    ordered = []
    # 1. already importable: boot.py put the package this server ships in on sys.path.
    # (A namespace match -- a bare "RizomUVLink" folder such as the superproject's -- has
    # no origin and is not a candidate.)
    spec = importlib.util.find_spec("RizomUVLink")
    if spec is not None and spec.origin and spec.origin.endswith(".py"):
        ordered.append((Path(spec.origin).parent, "sys.path"))
    # 2. the other places a RizomUVLink package can be
    ordered.extend(candidates())
    for directory, source in ordered:
        key = os.path.normcase(os.path.abspath(directory))
        if key in seen:
            continue
        seen.add(key)
        problem = _problem(directory)
        if problem is None:
            try:
                return _import_from(directory)
            except ImportError as e:
                problem = str(e)
        tried.append("%s: %s (%s)" % (source, directory, problem))
    raise ImportError("RizomUVLink was not found. Install RizomUV 2027.0 or later, or set RIZOMUV_LINK_DIR "
                      "to <install>/RizomUVLink.\n  tried: " + "\n  tried: ".join(tried))
