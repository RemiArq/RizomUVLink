"""Where the MCP server keeps its files, and where it finds the pieces it ships with."""
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent             # <RizomUVLink>/mcp/rizomuv_mcp


def _local_appdata():
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) if base else Path.home() / "AppData" / "Local"


def state_dir(sub=None):
    """The server's own files: userdir/ (RIZOMUV_USER_DIR of the headless instances it
    owns, so they never rotate the artist's command log), logs/, renders/, docs/, tmp/.
    Created on demand. RIZOMUV_MCP_STATE_DIR replaces the root (tests)."""
    override = os.environ.get("RIZOMUV_MCP_STATE_DIR")
    if override:
        root = Path(override)
    elif sys.platform == "win32":
        root = _local_appdata() / "rizomuv" / "mcp"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support" / "RizomUV" / "mcp"
    else:
        root = Path.home() / ".rizomuv" / "mcp"
    path = root / sub if sub else root
    path.mkdir(parents=True, exist_ok=True)
    return path


def instances_dir():
    """Where every listening RizomUV publishes <pid>.json. The application computes the
    same path (CUserDataStore::InstancesDir) and the two must agree to the byte: same
    override variable, same per-platform rule, nothing derived from the version or from
    RIZOMUV_LOCAL_DIR. Not created here: a reader has no business creating it."""
    override = os.environ.get("RIZOMUV_INSTANCES_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        return _local_appdata() / "rizomuv" / "instances"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "RizomUV" / "instances"
    return Path.home() / ".rizomuv" / "instances"


def mcp_dir():
    """<RizomUVLink>/mcp: boot.py, vendor/, and this package."""
    return _HERE.parent


def package_dir():
    """<RizomUVLink>: the binding package (RizomUVLink.py, win/, mac/, linux/). In an
    install it sits in the install directory, next to rizomuv.exe."""
    return _HERE.parent.parent


def boot_script():
    """The boot.py that started this process (it says so in RIZOMUV_MCP_BOOT_SCRIPT), else
    the one shipped next to the package, else None (a pip-installed server)."""
    env = os.environ.get("RIZOMUV_MCP_BOOT_SCRIPT")
    if env and os.path.isfile(env):
        return Path(env)
    shipped = mcp_dir() / "boot.py"
    return shipped if shipped.is_file() else None
