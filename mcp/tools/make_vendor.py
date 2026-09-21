"""Build mcp/vendor/: the RizomUV MCP server's third-party packages, for one target Python.

    <any CPython with pip> mcp/tools/make_vendor.py [--python-version 3.12] [--platform win_amd64]
        [--requirements FILE] [--compile-with <TARGET python.exe>] [--out DIR] [--force]

The target is the interpreter a RizomUV install embeds (CPython 3.12 embeddable on Windows),
which has no pip. So pip runs in whatever Python runs this script, is told the target's
version and platform, and accepts wheels only. Run by makefiledist.inc.php before the Inno
Setup step, and once by hand in a dev tree. mcp/vendor/ is generated and gitignored.

Up to date when VENDOR_STAMP matches (requirements + target + rules): no network then.
"""
import argparse
import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent      # mcp/tools
MCP_DIR = HERE.parent                        # mcp
FORMAT = 1  # bump when PRUNE or the compile rule changes: forces a rebuild everywhere

# Never imported by the server: pywin32's IDE and COM extras, docs, the console-script
# shims pip writes to bin/ (they hardcode the build machine's python path), and -- below --
# every "tests" directory. NOT pruned although unused over stdio: nothing that has a
# dist-info (cryptography, uvicorn, starlette... are imported by `import mcp` itself).
PRUNE = ["bin", "pythonwin", "adodbapi", "isapi", "win32comext", "win32/Demos", "win32/test",
         "win32/include", "win32/libs", "win32/scripts", "PyWin32.chm"]


def fail(msg):
    sys.stderr.write("make_vendor: " + msg + "\n")
    sys.exit(1)


def normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()   # PEP 503


def read_pins(req):
    """{normalized name: version} of the requirements file. Every line must be an exact
    pin: a range would let two setup builds ship different bytes."""
    pins = {}
    for n, line in enumerate(req.read_text(encoding="utf-8").splitlines(), 1):
        line = line.split("#", 1)[0].split(";", 1)[0].strip()
        if not line:
            continue
        if "==" not in line:
            fail("%s:%d is not an exact pin (name==version): %s" % (req, n, line))
        name, version = (s.strip() for s in line.split("==", 1))
        pins[normalize(name)] = version
    return pins


def check_installed(tmp, pins):
    """pip resolves dependencies itself, so a new release of mcp that adds a dependency
    would slip into the vendor unpinned. Refuse that instead of shipping it."""
    installed = {}
    for d in tmp.glob("*.dist-info"):
        name, _, version = d.name[:-len(".dist-info")].rpartition("-")
        installed[normalize(name)] = version
    unpinned = sorted("%s==%s" % kv for kv in installed.items() if kv[0] not in pins)
    missing = sorted(n for n in pins if n not in installed)
    wrong = sorted("%s (pinned %s, got %s)" % (n, pins[n], installed[n])
                   for n in pins if n in installed and installed[n] != pins[n])
    if unpinned or missing or wrong:
        fail("the vendor does not match the requirements file:"
             + ("\n  not pinned (add them): " + ", ".join(unpinned) if unpinned else "")
             + ("\n  pinned but not installed: " + ", ".join(missing) if missing else "")
             + ("\n  version differs: " + ", ".join(wrong) if wrong else ""))
    return sorted(d.name[:-len(".dist-info")] for d in tmp.glob("*.dist-info"))


def target_version(python):
    out = subprocess.check_output([python, "-I", "-S", "-c",
                                   "import sys; print('%d.%d' % sys.version_info[:2])"])
    return out.decode("ascii").strip()


def compute_stamp(req, args):
    h = hashlib.sha256(req.read_bytes())
    h.update(("|%s|%s|%s|%d" % (args.python_version, args.platform, bool(args.compile_with), FORMAT)).encode())
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--python-version", default="%d.%d" % sys.version_info[:2])
    ap.add_argument("--platform", default="win_amd64")
    ap.add_argument("--requirements", default=str(MCP_DIR / "requirements-vendor-win.txt"))
    ap.add_argument("--compile-with", help="the TARGET interpreter; precompiles to unchecked-hash .pyc")
    ap.add_argument("--out", default=str(MCP_DIR / "vendor"))
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    req = Path(args.requirements)
    out = Path(args.out)
    stamp = compute_stamp(req, args)
    stamp_file = out / "VENDOR_STAMP"
    if not args.force and stamp_file.is_file() and \
            stamp_file.read_text(encoding="utf-8").split("\n", 1)[0] == stamp:
        print("mcp vendor up to date: %s" % out)
        return 0

    pins = read_pins(req)
    if args.compile_with:
        # .pyc carry the magic number of the interpreter that wrote them: compiled by the
        # wrong minor, every one of them would be silently ignored and rewritten -- or,
        # in Program Files, recompiled in memory at every start.
        have = target_version(args.compile_with)
        if have != args.python_version:
            fail("--compile-with %s is Python %s, the vendor targets %s"
                 % (args.compile_with, have, args.python_version))

    tmp = out.with_name(out.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                           "--target", str(tmp), "--only-binary=:all:", "--platform", args.platform,
                           "--python-version", args.python_version, "--implementation", "cp",
                           "--no-compile", "-r", str(req)])
    dists = check_installed(tmp, pins)

    for rel in PRUNE:
        p = tmp / rel
        if p.is_dir():
            shutil.rmtree(p)
        elif p.is_file():
            p.unlink()
    for tests in sorted((d for d in tmp.rglob("tests") if d.is_dir()), key=lambda d: len(d.parts)):
        shutil.rmtree(tests, ignore_errors=True)

    if args.compile_with:
        # The TARGET interpreter, so the .pyc carry its magic number. unchecked-hash: valid
        # whatever the installer does to file times, never rewritten (Program Files is
        # read-only for the user anyway). A failure is fatal -- typically a path beyond
        # MAX_PATH on a box without LongPathsEnabled.
        subprocess.check_call([args.compile_with, "-I", "-S", "-m", "compileall", "-q",
                               "--invalidation-mode", "unchecked-hash", str(tmp)])

    (tmp / "VENDOR_PYTHON").write_text(args.python_version + "\n", encoding="utf-8")
    (tmp / "VENDOR_STAMP").write_text(stamp + "\n" + "\n".join(dists) + "\n", encoding="utf-8")

    # Swap by renaming, never by deleting first: a vendor whose .pyd are loaded by a running
    # server cannot be removed on Windows, and a delete that stops half way leaves neither
    # the old vendor nor the new one. A rename is refused as a whole.
    old = out.with_name(out.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if out.exists():
        try:
            out.rename(old)
        except OSError as e:
            fail("cannot replace %s (%s): a running Python probably uses it. Stop it and run "
                 "again; the new build is waiting in %s." % (out, e, tmp))
    tmp.rename(out)
    shutil.rmtree(old, ignore_errors=True)
    print("mcp vendor built: %s (%d distributions)" % (out, len(dists)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
