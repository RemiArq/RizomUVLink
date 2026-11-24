"""boot.py under the interpreters that run it, the vendor it relies on, and how the link
worker finds the binding."""
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

WINDOWS = sys.platform == "win32"

_PROBE = textwrap.dedent(r"""
    import json, os, sys
    import mcp, numpy, pywintypes, RizomUVLink
    link = RizomUVLink.CRizomUVLink()
    report = {"mcp": mcp.__file__, "numpy": numpy.__file__, "pywintypes": pywintypes.__file__,
              "binding": RizomUVLink.__file__, "link_version": link.Version(), "argv": sys.argv[1:],
              "boot_script": os.environ.get("RIZOMUV_MCP_BOOT_SCRIPT"), "path": sys.path,
              "flags": [sys.flags.isolated, sys.flags.no_site, sys.flags.utf8_mode]}
    with open(sys.argv[2], "w", encoding="utf-8") as f:
        json.dump(report, f)
    print("probe stdout")       # must reach the console, never before runpy: see the test
""")


@pytest.fixture
def fake_install(tmp_path, pkg_dir, mcp_dir):
    """<tmp>/RizomUVLink laid out like an install: the binding, and mcp/ with boot.py and
    a throwaway rizomuv_mcp whose __main__ reports what it could import."""
    pkg = tmp_path / "RizomUVLink"
    (pkg / "win").mkdir(parents=True)
    for name in ("RizomUVLink.py", "RizomUVLinkBase.py"):
        shutil.copy2(pkg_dir / name, pkg)
    shutil.copy2(pkg_dir / "win" / "__init__.py", pkg / "win")
    shutil.copy2(pkg_dir / "win" / ("rizomuvlink_python%d%d.pyd" % sys.version_info[:2]), pkg / "win")
    for dll in (pkg_dir / "win").glob("*.dll"):
        shutil.copy2(dll, pkg / "win")
    (pkg / "mcp" / "rizomuv_mcp").mkdir(parents=True)
    shutil.copy2(mcp_dir / "boot.py", pkg / "mcp")
    (pkg / "mcp" / "rizomuv_mcp" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "mcp" / "rizomuv_mcp" / "__main__.py").write_text(_PROBE, encoding="utf-8")
    return pkg


@pytest.fixture
def embedded_python(snap_exe):
    python = snap_exe.parent / "python.exe"
    if not python.is_file():
        pytest.skip("no embedded python next to %s" % snap_exe)
    return python


def _run_boot(python, boot, *args, vendor=None, cwd=None):
    env = dict(os.environ)
    env.pop("RIZOMUV_MCP_BOOT_SCRIPT", None)
    if vendor is not None:
        env["RIZOMUV_MCP_VENDOR"] = str(vendor)
    return subprocess.run([str(python), "-I", "-S", "-X", "utf8", str(boot), *args], env=env, cwd=cwd,
                          stdin=subprocess.DEVNULL, capture_output=True, timeout=120)


@pytest.mark.skipif(not WINDOWS, reason="the vendor and the embedded python are Windows builds")
@pytest.mark.parametrize("which", ["dev-cpython", "embedded"])
def test_boot_imports_the_sdk_numpy_and_the_binding(which, fake_install, vendor_dir, request, tmp_path):
    python = sys.executable if which == "dev-cpython" else request.getfixturevalue("embedded_python")
    report = tmp_path / "report.json"
    boot = fake_install / "mcp" / "boot.py"
    run = _run_boot(python, boot, "--report", str(report), "a b", vendor=vendor_dir, cwd=tmp_path)
    assert run.returncode == 0, run.stderr.decode(errors="replace")
    r = json.loads(report.read_text(encoding="utf-8"))
    vendor = os.path.normcase(str(vendor_dir))
    for key in ("mcp", "numpy", "pywintypes"):
        assert os.path.normcase(r[key]).startswith(vendor), (key, r[key])
    assert Path(r["binding"]) == fake_install / "RizomUVLink.py"
    assert r["link_version"]
    assert r["argv"] == ["--report", str(report), "a b"], "arguments pass through verbatim"
    assert Path(r["boot_script"]) == boot
    assert r["flags"] == [1, 1, 1]
    order = [os.path.normcase(p) for p in r["path"]]
    assert order.index(vendor) < order.index(os.path.normcase(str(boot.parent))) \
        < order.index(os.path.normcase(str(fake_install))), "vendor, then mcp/, then the binding"
    assert run.stdout.replace(b"\r", b"") == b"probe stdout\n", "boot itself writes nothing to stdout"


def _fake_vendor(tmp_path, python_version):
    vendor = tmp_path / "vendor"
    (vendor / "mcp").mkdir(parents=True)
    (vendor / "VENDOR_PYTHON").write_text(python_version + "\n", encoding="utf-8")
    return vendor


def test_boot_refuses_a_vendor_built_for_another_python(fake_install, tmp_path):
    run = _run_boot(sys.executable, fake_install / "mcp" / "boot.py", vendor=_fake_vendor(tmp_path, "3.99"))
    assert run.returncode == 3
    assert run.stdout == b""
    assert b"for Python 3.99, this is Python %d.%d" % sys.version_info[:2] in run.stderr


def test_boot_without_a_vendor_says_how_to_make_one(fake_install, tmp_path):
    run = _run_boot(sys.executable, fake_install / "mcp" / "boot.py", vendor=tmp_path / "none")
    assert run.returncode == 3 and run.stdout == b""
    assert b"make_vendor.py" in run.stderr


# ------------------------------------------------------------------ the vendor

def _load_make_vendor(mcp_dir):
    import importlib.util
    spec = importlib.util.spec_from_file_location("make_vendor", mcp_dir / "tools" / "make_vendor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_built_vendor_matches_the_pins(vendor_dir, mcp_dir):
    mv = _load_make_vendor(mcp_dir)
    pins = mv.read_pins(mcp_dir / "requirements-vendor-win.txt")
    assert len(pins) == 30 and pins["mcp"] == "2.2.0"
    assert mv.check_installed(vendor_dir, pins)   # exits when anything is off
    assert (vendor_dir / "VENDOR_PYTHON").read_text().strip() == "3.12"
    stamp = (vendor_dir / "VENDOR_STAMP").read_text().splitlines()
    assert len(stamp) == 31
    assert not (vendor_dir / "bin").exists() and not (vendor_dir / "pythonwin").exists()
    assert not list(vendor_dir.glob("*/tests")), "pruned"
    assert list((vendor_dir / "mcp").glob("__pycache__/*.cpython-312.pyc")), "precompiled"


def test_make_vendor_refuses_an_unpinned_distribution(mcp_dir, tmp_path):
    mv = _load_make_vendor(mcp_dir)
    for name in ("mcp-2.2.0.dist-info", "surprise-1.0.dist-info", "numpy-2.5.2.dist-info"):
        (tmp_path / name).mkdir()
    with pytest.raises(SystemExit):
        mv.check_installed(tmp_path, {"mcp": "2.2.0", "numpy": "2.5.3", "pywin32": "312"})
    req = tmp_path / "req.txt"
    req.write_text("mcp>=2.2\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        mv.read_pins(req)


# ------------------------------------------------------------------ locating the binding

_BINDING_CHILD = textwrap.dedent(r"""
    import json, sys
    sys.path.append(sys.argv[1])
    for extra in sys.argv[2:]:
        sys.path.insert(0, extra)
    from rizomuv_mcp import binding
    try:
        mod = binding.load_binding()
        print(json.dumps({"file": mod.__file__, "has": hasattr(mod, "CRizomUVLink") and hasattr(mod, "CZEx")}))
    except ImportError as e:
        print(json.dumps({"error": str(e)}))
""")


def _locate(server_dir, *extra, cwd, env=None):
    run = subprocess.run([sys.executable, "-c", _BINDING_CHILD, str(server_dir), *map(str, extra)],
                         capture_output=True, text=True, cwd=cwd, env=env, timeout=60)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not WINDOWS, reason="win/ binding")
def test_the_binding_is_found_next_to_the_server(mcp_dir, pkg_dir, repo_root):
    # cwd = the superproject, whose RizomUVLink/ folder is a namespace match, not a module
    r = _locate(mcp_dir, cwd=repo_root)
    assert Path(r["file"]) == pkg_dir / "RizomUVLink.py" and r["has"]


@pytest.mark.skipif(not WINDOWS, reason="win/ binding")
def test_a_binding_without_a_module_for_this_python_is_passed_over(mcp_dir, pkg_dir, tmp_path):
    shadow = tmp_path / "shadow"
    (shadow / "win").mkdir(parents=True)
    shutil.copy2(pkg_dir / "RizomUVLink.py", shadow)
    r = _locate(mcp_dir, shadow, cwd=tmp_path)
    assert Path(r["file"]) == pkg_dir / "RizomUVLink.py", "the importable but unusable one is skipped"


def test_not_found_lists_every_place_tried(mcp_dir, tmp_path):
    server = tmp_path / "somewhere" / "mcp"
    (server / "rizomuv_mcp").mkdir(parents=True)
    for name in ("__init__.py", "binding.py", "launch.py", "paths.py", "discovery.py", "tcptable.py"):
        src = mcp_dir / "rizomuv_mcp" / name
        (server / "rizomuv_mcp" / name).write_text(src.read_text(encoding="utf-8") if src.is_file() else "",
                                                   encoding="utf-8")
    env = dict(os.environ, RIZOMUV_LINK_DIR=str(tmp_path / "bogus"))
    env.pop("RIZOMUV_MCP_APP_DIR", None)
    r = _locate(server, cwd=tmp_path, env=env)
    msg = r["error"]
    assert msg.startswith("RizomUVLink was not found.") and "RIZOMUV_LINK_DIR" in msg
    assert "tried: RIZOMUV_LINK_DIR: %s (no RizomUVLink.py)" % (tmp_path / "bogus") in msg
    assert "tried: the package this server ships in: %s" % (tmp_path / "somewhere") in msg
