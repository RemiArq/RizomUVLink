"""Entry point of the RizomUV MCP server.

    <python> -I -S -X utf8 <this file> [server arguments]

Started by rizomuv-mcp.exe (Windows), or by hand, and by the server itself for its link
worker. Works under the embeddable CPython a RizomUV install ships (python312._pth:
isolated, no site, and the script directory is NOT put on sys.path) and under a full
CPython of the same minor version in a dev tree: -S keeps the latter's site-packages out,
so both see exactly the stdlib plus the vendor.

Never prints to stdout, and never rebinds sys.stdout: stdout is the MCP JSON-RPC channel,
and the SDK's stdio_server claims fd 1 itself (it diverts it to stderr while serving).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))   # <RizomUVLink>/mcp
PKG_DIR = os.path.dirname(HERE)                     # <RizomUVLink>: the binding package dir


def _fail(msg):
    sys.stderr.write("rizomuv-mcp boot: " + msg + "\n")
    sys.stderr.flush()
    sys.exit(3)


def _vendor_dir():
    return os.environ.get("RIZOMUV_MCP_VENDOR") or os.path.join(HERE, "vendor")


def main():
    vendor = _vendor_dir()
    if not os.path.isdir(os.path.join(vendor, "mcp")):
        _fail("no vendored dependencies in %s. In a dev tree run mcp/tools/make_vendor.py once; "
              "in an install, repair RizomUV." % vendor)

    # The vendor dir holds cp3XY binaries (pydantic_core, numpy, pywin32...): refuse a
    # mismatching interpreter with a sentence rather than an ImportError deep in pydantic.
    stamp = os.path.join(vendor, "VENDOR_PYTHON")
    if os.path.isfile(stamp):
        with open(stamp, encoding="utf-8") as f:
            wanted = f.read().strip()
        have = "%d.%d" % sys.version_info[:2]
        if wanted and wanted != have:
            _fail("the vendored dependencies are for Python %s, this is Python %s (%s)"
                  % (wanted, have, sys.executable))

    # site.addsitedir, not a bare sys.path entry: it also runs the vendor's .pth files,
    # and pywin32.pth is what puts win32/, win32/lib and the pywin32_system32 DLLs in
    # reach (the MCP SDK's stdio transport needs win32api/win32job on Windows).
    import site
    site.addsitedir(vendor)

    # The server package sits next to this file, the binding one level up. Both go AFTER
    # the vendor: PKG_DIR contains this very "mcp" folder, which must never be taken for
    # the SDK package of the same name -- it has no __init__.py and must keep none.
    for p in (HERE, PKG_DIR):
        if p not in sys.path:
            sys.path.append(p)

    # The server starts its link worker through this same file, with this same
    # interpreter and flags: that is the only way the worker gets the same sys.path.
    os.environ["RIZOMUV_MCP_BOOT_SCRIPT"] = os.path.abspath(__file__)

    import runpy
    runpy.run_module("rizomuv_mcp", run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
