"""The link worker: the only process that imports the RizomUVLink binding (and numpy).

Why a process of its own: the binding holds the GIL for a whole command and busy-spins
while it waits, so a 10-25 s Pack would freeze every thread of the process that runs it --
in the MCP server, the JSON-RPC loop, pings and cancellation. The server talks to this
worker over its stdin/stdout, one JSON object per line, one request at a time:

    {"id": 7, "op": "execute", "args": {...}}
    {"id": 7, "ok": true, "result": ...}
    {"id": 7, "ok": false, "error": {"kind": "rizomuv", "message": "..."}}

Error kinds: rizomuv (RizomUV answered with an error; the socket is fine), busy (another
client's command runs), link_lost (silence or a broken socket: reconnect), not_connected,
bad_request, internal.
"""
import json
import logging
import os
import re
import sys
import time
import traceback
from pathlib import Path

log = logging.getLogger("rizomuv_mcp.worker")

VERSION_PATH = "Vars.Infos.Version.Full"
_VERSION_RE = re.compile(r"^\d{4}\.\d+\.\d+")
_BUSY = "RizomUV is busy"
_SILENCE = "RizomUV is not responding"

# Silence windows (ms): the longest gap allowed between two replies, heartbeats included.
# A command of any length survives them; only a dead or wedged instance does not.
COMMAND_TIMEOUT_MS = 30000
SNAPSHOT_TIMEOUT_MS = 60000     # no heartbeat while a big reply is serialized

# Island render colours of the spike (report 3 §4): blue fills, black borders on white.
_RENDER_BASE = {"WorkingSet": "Visible", "WidthHeightUnit": "px", "AASamples": 4,
                "PolygonColorMode": "Color", "PolygonColor": [0.30, 0.45, 0.75],
                "EdgeColorMode": "Color", "EdgeColor": [0.0, 0.0, 0.0], "BorderOnly": True,
                "BackgroundColor": [1.0, 1.0, 1.0]}


class OpError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind
        self.message = message


class Worker:
    def __init__(self):
        self.module = None      # the RizomUVLink module, imported on the first connect
        self.link = None
        self.pyd = None         # the compiled client: Execute with an explicit timeout
        self.port = None
        self.connected = False
        self._diag_count = 0

    # ------------------------------------------------------------------ link plumbing

    def _binding(self):
        if self.module is None:
            from . import binding
            try:
                self.module = binding.load_binding()
            except ImportError as e:
                raise OpError("internal", str(e)) from None
        return self.module

    def _reconnect(self):
        """A brand-new socket on the port: the only way out of a poisoned REQ state."""
        self.connected = False
        self.link.Connect(self.port)
        self.connected = True

    def _exec(self, command, params, timeout_ms=COMMAND_TIMEOUT_MS):
        if not self.connected or self.pyd is None:
            raise OpError("not_connected", "The link worker is not connected to RizomUV.")
        try:
            return self.pyd.Execute(command, params, int(timeout_ms))
        except self.module.CZEx as e:
            text = str(e)
            if text.startswith(_BUSY):
                raise OpError("busy", text) from None
            if text.startswith(_SILENCE):
                self.connected = False
                raise OpError("link_lost", text) from None
            raise OpError("rizomuv", text) from None
        except RuntimeError as e:
            # zmq's EFSM after a timeout, or a socket torn down under it
            self.connected = False
            raise OpError("link_lost", "The link socket failed (%s)." % e) from None

    def _get_quiet(self, path, default=None):
        try:
            return self._exec("Get", path)
        except OpError as e:
            if e.kind != "rizomuv":
                raise
            return default

    def _sacrificial_get(self, deadline):
        """The version, read until it IS the version. A client that died mid-command leaves
        the server in its heartbeat loop, and the next request on the port -- ours -- is
        answered with that dead command's output (or its error): an answer that is not a
        version string is exactly that, and the next one is ours."""
        wrong, busy_since = 0, None
        while True:
            left_ms = max(2000, int((deadline - time.monotonic()) * 1000))
            try:
                value = self._exec("Get", VERSION_PATH, min(left_ms, 120000))
            except OpError as e:
                now = time.monotonic()
                if e.kind == "busy":
                    if busy_since is None:
                        log.info("port %d: busy with another client's command, waiting", self.port)
                    busy_since = busy_since or now
                    if now - busy_since > 30:
                        raise OpError("busy", "RizomUV on port %d is busy with another RizomUVLink client's "
                                              "command and stayed so for 30 s." % self.port) from None
                    time.sleep(0.5)
                    continue
                if e.kind == "link_lost" and now < deadline:
                    time.sleep(0.5)
                    self._reconnect()
                    continue
                if e.kind != "rizomuv":
                    raise
                value = e   # a stale error reply of the dead client: as wrong as stale data
            if isinstance(value, str) and _VERSION_RE.match(value):
                return value
            wrong += 1
            log.warning("port %d: discarded an answer that is not the version (%.200r)", self.port, value)
            if wrong >= 5:
                raise OpError("link_lost", "RizomUV on port %d keeps answering something other than its "
                                           "version: another client is probably using it." % self.port)

    # ------------------------------------------------------------------ ops

    def op_ping(self):
        return {"pong": True}

    def op_connect(self, port, token=None, startup_timeout=60):
        mod = self._binding()
        if self.link is None:
            self.link = mod.CRizomUVLink()
            self.pyd = self.link.rizomuv
        deadline = time.monotonic() + float(startup_timeout)
        self.port = int(port)
        self._reconnect()
        version = self._sacrificial_get(deadline)
        token_ok = None
        if token:
            value = self._get_quiet("Vars.RizomUVLink.InstanceToken")
            # a build without the variable cannot say; any other value is a stranger
            token_ok = None if value is None else value == token
        startup_done = False
        while True:
            startup_done = bool(self._exec("Get", "Vars.Infos.StartupDone"))
            if startup_done or time.monotonic() > deadline:
                break
            time.sleep(0.2)
        return {"version": version, "headless": self._get_quiet("Vars.Infos.Headless"),
                "startup_done": startup_done, "token_ok": token_ok,
                "identifier": self._get_quiet("Vars.RizomUVLink.Identifier")}

    def op_execute(self, command, params=None, timeout_ms=COMMAND_TIMEOUT_MS):
        if not isinstance(command, str) or not command:
            raise OpError("bad_request", "execute needs a command name.")
        return {"value": self._exec(command, params, timeout_ms)}

    def op_scene(self):
        islands = self._exec("Count", "Lib.Mesh.Islands")
        # Count answers 0 on a plain list node: the polygon count is the list's length
        sizes = self._get_quiet("Lib.Mesh.PolygonSizes", [])
        polygons = len(sizes) if isinstance(sizes, list) else None
        file = self._get_quiet("Prefs.LastLoadedFile")
        return {"has_mesh": bool(islands) or bool(polygons), "polygons": polygons, "islands": islands,
                "file": file or None}

    def _snapshot(self, attached):
        from . import metrics
        try:
            out = self._exec("Save", {"Data": True}, SNAPSHOT_TIMEOUT_MS)
            out = dict(out or {})
            out.update(self._exec("Save", {"IndexTable": {"PolygonIDsToIslandIDs": True}},
                                  SNAPSHOT_TIMEOUT_MS) or {})
        except OpError as e:
            if e.kind == "rizomuv" and "No UVSet present" in e.message:
                raise OpError("rizomuv", "There is no mesh in the scene: load one first.") from None
            raise
        finally:
            if attached:
                self._rearm()
        try:
            return metrics.UVSnapshot.from_save_output(out)
        except ValueError as e:
            raise OpError("internal", "RizomUV's UV data could not be read: %s" % e) from None

    def _rearm(self):
        """Save clears the artist's "unsaved work" flag in current builds, even a Save that
        only returns data; any other command sets it again. Without this, the artist could
        quit RizomUV after our measure without being asked to save."""
        try:
            self._exec("Get", VERSION_PATH)
        except OpError as e:
            log.warning("could not re-arm the unsaved-work flag: %s", e.message)

    def op_measure(self, res=1024, padding_px=None, attached=False):
        from . import metrics
        snap = self._snapshot(attached)
        if snap.poly_sizes.size == 0:
            raise OpError("rizomuv", "There is no mesh in the scene: load one first.")
        return metrics.measure(snap, int(res), padding_px)

    def op_diagnose(self, res=1024, padding_px=None, attached=False, tmp_dir=None):
        from . import diagnose, objmesh
        snap = self._snapshot(attached)
        if snap.poly_sizes.size == 0:
            raise OpError("rizomuv", "There is no mesh in the scene: load one first.")
        self._diag_count += 1
        tmp = Path(tmp_dir) if tmp_dir else Path.cwd()
        obj = tmp / ("diag-%d-%d.obj" % (os.getpid(), self._diag_count))
        notes, mesh3d = [], None
        try:
            try:
                self._exec("Save", {"File": {"Path": obj.as_posix()}, "__DontUpdateGUIFilePath": True},
                           SNAPSHOT_TIMEOUT_MS)
            except OpError as e:
                if e.kind != "rizomuv":
                    raise
                notes.append("The 3D side could not be exported (%s)." % e.message)
            negative = self._get_quiet("Lib.Mesh.Quality.NegativeUVArea")
            if attached:
                self._rearm()
            if obj.is_file():
                try:
                    mesh3d = objmesh.read_obj(obj)
                except ValueError as e:
                    notes.append("The exported 3D mesh could not be read (%s)." % e)
            elif not notes:
                notes.append("Save wrote no file (demo licence?): the 3D side of the scene is unknown.")
        finally:
            try:
                obj.unlink()
            except OSError:
                pass
        negative = float(negative) if isinstance(negative, (int, float)) and not isinstance(negative, bool) else None
        return diagnose.diagnose(snap, int(res), padding_px, mesh3d=mesh3d, negative_uv_area=negative,
                                 notes=notes)

    def op_render(self, path, size_px=1024, mode="islands", crop=None):
        size = int(size_px)
        width = height = float(size)
        params = dict(_RENDER_BASE)
        if crop is not None:
            umin, vmin, umax, vmax = (float(c) for c in crop)
            if not (umax > umin and vmax > vmin):
                raise OpError("bad_request", "crop must be [umin, vmin, umax, vmax] with umin < umax and vmin < vmax.")
            # CroppingBox is x-range then y-range; nothing preserves the aspect, so the image does
            aspect = (umax - umin) / (vmax - vmin)
            if aspect >= 1:
                height = max(1.0, round(size / aspect))
            else:
                width = max(1.0, round(size * aspect))
            params["CroppingBox"] = [umin, umax, vmin, vmax]
        if mode == "stretch":
            # the declared ramp is centred on 1.0 while a packed layout's S is ~0.03: every
            # island would render saturated red. Centre it on the mesh's average, as the UI does.
            s = self._exec("Get", "Lib.Mesh.SAvg")
            if not isinstance(s, (int, float)) or not s > 0:
                raise OpError("rizomuv", "RizomUV reports no average texel density (Lib.Mesh.SAvg = %r): "
                                         "is there a mesh with UVs?" % (s,))
            params.update({"PolygonColorMode": "Stretches", "Stretches.Neutral": float(s),
                           "Stretches.Min": 0.0, "Stretches.Max": 2.0 * float(s),
                           "Stretches.Saturation": 0.95})
        elif mode != "islands":
            raise OpError("bad_request", "mode must be 'islands' or 'stretch'.")
        out = Path(path)
        try:
            out.unlink()
        except OSError:
            pass
        params.update({"FilePath": str(out), "Width": width, "Height": height})
        self._exec("RasterExport", params, SNAPSHOT_TIMEOUT_MS)
        if not out.is_file() or out.stat().st_size == 0:
            raise OpError("rizomuv", "RasterExport wrote nothing (demo licence?).")
        return {"path": str(out), "width": int(width), "height": int(height), "bytes": out.stat().st_size}

    def op_docs(self, path):
        self._exec("GenPythonModule", {"Path": str(path)})
        if not Path(path).is_file():
            raise OpError("rizomuv", "GenPythonModule wrote nothing to %s." % path)
        return {"version": self._exec("Get", VERSION_PATH), "module_path": str(path)}

    def op_quit_instance(self):
        # Quit answers before the process exits; after it the socket has no one behind it
        value = self._exec("Quit", {}, 10000)
        self.connected = False
        return {"quit": True, "value": value}

    def op_shutdown(self):
        return {"bye": True}

    # ------------------------------------------------------------------ dispatch

    def handle(self, request):
        rid = request.get("id") if isinstance(request, dict) else None
        try:
            if not isinstance(request, dict) or not isinstance(request.get("op"), str):
                raise OpError("bad_request", "A request is {\"id\": n, \"op\": name, \"args\": {...}}.")
            fn = getattr(self, "op_" + request["op"], None)
            if fn is None:
                raise OpError("bad_request", "Unknown op %r." % request["op"])
            args = request.get("args") or {}
            if not isinstance(args, dict):
                raise OpError("bad_request", "args must be an object.")
            try:
                result = fn(**args)
            except TypeError as e:
                if "argument" in str(e) and fn.__name__ in str(e):
                    raise OpError("bad_request", "%s: %s" % (request["op"], e)) from None
                raise
            return {"id": rid, "ok": True, "result": result}
        except OpError as e:
            return {"id": rid, "ok": False, "error": {"kind": e.kind, "message": e.message}}
        except Exception as e:   # noqa: BLE001 -- reported to the server, the worker keeps serving
            last = traceback.format_exc().strip().splitlines()[-3:]
            log.error("op %r failed:\n%s", request.get("op") if isinstance(request, dict) else None,
                      traceback.format_exc())
            return {"id": rid, "ok": False,
                    "error": {"kind": "internal", "message": "%r (%s)" % (e, " | ".join(ln.strip() for ln in last))}}


def _jsonable(value):
    """numpy scalars and anything else json cannot take, as plain Python."""
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except (TypeError, ValueError):
            pass
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        return tolist()
    return repr(value)


def main(log_level="INFO"):
    # The pipe to the server is fd 1: keep a private copy for the protocol, then point fd 1
    # and sys.stdout at stderr, so nothing the binding, numpy or a library prints can ever
    # land in the middle of a response line.
    wire = os.fdopen(os.dup(1), "w", encoding="utf-8", newline="\n")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    logging.basicConfig(level=getattr(logging, str(log_level).upper(), logging.INFO), stream=sys.stderr,
                        format="rizomuv-mcp worker %(process)d: %(levelname)s %(message)s")
    worker = Worker()
    stdin = sys.stdin.buffer
    while True:
        raw = stdin.readline()
        if not raw:
            break           # the server is gone: nothing left to answer
        raw = raw.strip()
        if not raw:
            continue
        try:
            request = json.loads(raw.decode("utf-8"))
        except ValueError as e:
            request = None
            response = {"id": None, "ok": False, "error": {"kind": "bad_request", "message": "not JSON: %s" % e}}
        else:
            response = worker.handle(request)
        wire.write(json.dumps(response, allow_nan=True, separators=(",", ":"), default=_jsonable) + "\n")
        wire.flush()
        if isinstance(request, dict) and request.get("op") == "shutdown":
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
