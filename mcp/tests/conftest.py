"""Shared setup of the RizomUV MCP server tests.

    <python 3.12> -m pytest RizomUVLink/RizomUVLink/mcp/tests

The server's dependencies come from mcp/vendor (build it once with mcp/tools/make_vendor.py),
exactly as the installed server gets them. Tests that start RizomUV (marker "live") run
whenever an executable is found, and must stop every instance they start.
"""
import os
import site
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

MCP_DIR = Path(__file__).resolve().parents[1]      # <RizomUVLink>/mcp
PKG_DIR = MCP_DIR.parent                           # <RizomUVLink>: the binding package
REPO_ROOT = PKG_DIR.parents[1]                     # the superproject, when checked out in it
VENDOR_DIR = Path(os.environ.get("RIZOMUV_MCP_VENDOR") or MCP_DIR / "vendor")

# The dev build of the superproject, unless RIZOMUV_MCP_TEST_EXE names another one -- a copy
# of RizomUVApp/bin, typically, while that exe is being rebuilt: a running instance locks it.
DEV_EXE = REPO_ROOT / "RizomUVApp" / "bin" / "rizomuv.exe"

# The same order as boot.py: the vendor (with its .pth files: pywin32), then the server
# package's directory, then the binding's -- after the vendor, so that the "mcp" folder
# in PKG_DIR is never taken for the SDK.
if VENDOR_DIR.is_dir():
    site.addsitedir(str(VENDOR_DIR))
for _p in (MCP_DIR, PKG_DIR):
    if str(_p) not in sys.path:
        sys.path.append(str(_p))


def pytest_configure(config):
    config.addinivalue_line("markers", "live: starts or drives a real RizomUV (skipped when none is found)")
    config.addinivalue_line("markers", "slow: takes more than a few seconds")


@pytest.fixture(scope="session")
def repo_root():
    if not (REPO_ROOT / "RizomUVApp").is_dir():
        pytest.skip("not checked out inside the RizomUV repository")
    return REPO_ROOT


@pytest.fixture(scope="session")
def pkg_dir():
    return PKG_DIR


@pytest.fixture(scope="session")
def mcp_dir():
    return MCP_DIR


@pytest.fixture(scope="session")
def vendor_dir():
    if not (VENDOR_DIR / "mcp").is_dir():
        pytest.skip("no vendor in %s: run mcp/tools/make_vendor.py" % VENDOR_DIR)
    return VENDOR_DIR


@pytest.fixture(scope="session")
def snap_exe():
    env = os.environ.get("RIZOMUV_MCP_TEST_EXE")
    for candidate in ([Path(env)] if env else []) + [DEV_EXE]:
        if candidate.is_file():
            return candidate
    pytest.skip("no RizomUV executable to launch (set RIZOMUV_MCP_TEST_EXE)")


@pytest.fixture(scope="session")
def example_mesh():
    path = PKG_DIR / "examples" / "ExampleMesh.obj"
    if not path.is_file():
        pytest.skip("missing %s" % path)
    return path


@pytest.fixture(scope="session")
def mech8():
    path = REPO_ROOT / "ObjFiles" / "Handmades" / "Mech8_2055isl_85kTri.obj"
    if not path.is_file():
        pytest.skip("missing %s" % path)
    return path


@pytest.fixture
def isolated_dirs(tmp_path, monkeypatch):
    """A private state dir and discovery dir, so a test never reads the artist's
    instances nor writes into the real %LOCALAPPDATA%. Both exist on return."""
    state = tmp_path / "state"
    instances = tmp_path / "instances"
    state.mkdir()
    instances.mkdir()
    monkeypatch.setenv("RIZOMUV_MCP_STATE_DIR", str(state))
    monkeypatch.setenv("RIZOMUV_INSTANCES_DIR", str(instances))
    return SimpleNamespace(root=tmp_path, state=state, instances=instances)
