"""The one RizomUV a server session drives, and the link worker process that talks to it.

Which instance: the artist's open RizomUV (found through the discovery files the app
publishes) or, when there is none, a private headless one this server starts and owns.
An explicit --port attaches to exactly that instance. The server never quits an instance
it did not start: Quit has no save prompt.

How: every RizomUVLink call goes through the worker (worker.py), a child process, because
the binding holds the GIL for a whole command. While a call is pending this module watches
the instance pid and the worker once a second, and reports progress every 2 s, so a dead
RizomUV is noticed in about a second rather than after the link's 30 s silence window.

Everything async here runs on the server's event loop; the few blocking probes (port
tests, TCP table, job termination) go through asyncio.to_thread.
"""
import asyncio
import atexit
import json
import logging
import os
import subprocess
import sys
import time

from mcp.server.mcpserver.exceptions import ToolError

from . import discovery, launch, paths, tcptable

log = logging.getLogger(__name__)

NO_INSTANCE = ("No open RizomUV to attach to. Open RizomUV 2027.0 (a build that publishes itself), start "
               "one with -id <port> and pass --port, or use connect(target='headless').")
LAUNCH_LABEL = "Starting a headless RizomUV (first start can take a minute)"
DIALOG_HINT = " (if a dialog is open in RizomUV, it is waiting for an answer there)"
ATTACH_TIMEOUT = 60.0
BUSY_RETRY_SECONDS = 30.0
CLOSE_QUIT_SECONDS = 1.0
CLOSE_EXIT_SECONDS = 0.5

# One response line can carry a whole data dump (Save Data of a big mesh is several MB).
_LINE_LIMIT = 1 << 28


class WorkerError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind
        self.message = message


class InstanceStopped(Exception):
    """The instance a pending call was waiting on is gone."""


class _Busy(Exception):
    pass


class _Skip(Exception):
    """A discovered candidate that cannot be attached; the reason goes into the notes."""


def _error_response(rid, kind, message):
    return {"id": rid, "ok": False, "error": {"kind": kind, "message": message}}


# ------------------------------------------------------------------ the worker process

def worker_command(log_level="INFO"):
    """The command line of the link worker: the same interpreter, started the way this
    server was (boot.py sets the vendor on sys.path), or as a module when installed."""
    boot = paths.boot_script()
    if boot is not None:
        base = [sys.executable, "-I", "-S", "-X", "utf8", str(boot)]
    else:
        base = [sys.executable, "-m", "rizomuv_mcp"]
    return base + ["--link-worker", "--log-level", str(log_level)]


async def spawn_worker(log_level="INFO"):
    """Start a link worker. Its stdin/stdout are the protocol pipes; its stderr is ours,
    which an MCP client shows as the server log -- never our stdout, the JSON-RPC wire."""
    kwargs = {}
    if sys.platform == "win32":
        # python.exe is a console program: without this a server started with no console
        # would flash one on the artist's desktop
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = await asyncio.create_subprocess_exec(
        *worker_command(log_level), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=None, limit=_LINE_LIMIT, **kwargs)
    return WorkerProcess(proc)


class WorkerProcess:
    """The server's end of one link worker. Requests are answered in order; a response
    whose waiter gave up (a cancelled tool call) is dropped when it arrives."""

    def __init__(self, proc):
        self.proc = proc
        self._popen = proc._transport.get_extra_info("subprocess") if hasattr(proc, "_transport") else None
        self._next_id = 0
        self._pending = {}
        self._dead = None
        self._reader = asyncio.ensure_future(self._read())

    @property
    def pid(self):
        return self.proc.pid

    def alive(self):
        return self._dead is None and self.proc.returncode is None

    async def _read(self):
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    log.error("link worker %d wrote a line that is not JSON: %.200r", self.pid, line)
                    continue
                fut = self._pending.pop(msg.get("id"), None) if isinstance(msg, dict) else None
                if fut is not None and not fut.done():
                    fut.set_result(msg)
        except Exception as e:   # noqa: BLE001 -- a broken pipe is as final as EOF
            log.debug("link worker %d: reader stopped: %r", self.pid, e)
        finally:
            code = None
            try:
                code = await asyncio.wait_for(self.proc.wait(), 5.0)
            except (asyncio.TimeoutError, Exception):   # noqa: BLE001
                pass
            self._dead = "exit code %s" % code
            pending, self._pending = self._pending, {}
            for rid, fut in pending.items():
                if not fut.done():
                    fut.set_result(_error_response(rid, "worker_dead",
                                                   "The link worker process stopped (%s)." % self._dead))

    async def submit(self, op, args=None):
        """Send one request; the future resolves to its response dict (never raises)."""
        fut = asyncio.get_running_loop().create_future()
        if not self.alive():
            fut.set_result(_error_response(None, "worker_dead", "The link worker process is not running."))
            return fut
        self._next_id += 1
        rid = self._next_id
        self._pending[rid] = fut
        line = json.dumps({"id": rid, "op": op, "args": args or {}}, separators=(",", ":"), allow_nan=True)
        try:
            self.proc.stdin.write(line.encode("utf-8") + b"\n")
            await self.proc.stdin.drain()
        except (OSError, RuntimeError) as e:   # the pipe closed under us
            self._pending.pop(rid, None)
            if not fut.done():
                fut.set_result(_error_response(rid, "worker_dead", "The link worker is gone (%s)." % e))
        return fut

    async def request(self, op, args=None, timeout=None):
        """submit() and wait for the response dict. asyncio.TimeoutError after timeout; the
        request itself keeps running in the worker."""
        fut = await self.submit(op, args)
        return await asyncio.wait_for(asyncio.shield(fut), timeout)

    def kill(self):
        if self.proc.returncode is None:
            try:
                self.proc.kill()
            except (OSError, ProcessLookupError):
                pass

    async def wait_closed(self, timeout=1.0):
        try:
            await asyncio.wait_for(self.proc.wait(), timeout)
        except asyncio.TimeoutError:
            self.kill()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout)
            except asyncio.TimeoutError:
                pass

    async def close(self, timeout=0.5):
        """Ask the worker to exit, kill it if it does not."""
        if self.alive():
            try:
                await self.request("shutdown", timeout=timeout)
            except (asyncio.TimeoutError, Exception):   # noqa: BLE001
                pass
        await self.wait_closed(timeout)

    def kill_now(self):
        """From atexit, with no event loop left: the Popen underneath."""
        popen = self._popen
        try:
            if popen is not None and popen.poll() is None:
                popen.kill()
        except OSError:
            pass


# ------------------------------------------------------------------ the session

class Session:
    """Instance selection, the worker, calls and the instance's lifetime."""

    def __init__(self, config):
        self.config = config
        self.target = config.instance
        self.port_request = config.port
        self._lock = asyncio.Lock()
        self._gen = 0
        self._closed = False
        self.worker = None
        self.inflight = 0
        self.last_pack = None          # {"map_resolution", "padding_px"} of the last pack tool call
        self.scene_cache = None
        self.notes = []                # why discovered candidates were skipped, last resolution
        self._clear()
        atexit.register(self._atexit)

    def _clear(self):
        self.mode = None               # "attached" | "headless"
        self.owned = False             # started by this server: quit when the session ends
        self.inst = None               # launch.LaunchedInstance of an owned instance
        self.port = None
        self.pid = None
        self.version = None
        self.headless = None
        self.record = None             # discovery.InstanceRecord, when the instance published one
        self.exe = None
        self.instance_lock = None
        self.connected_at = None

    @property
    def connected(self):
        return self.mode is not None

    @property
    def attached(self):
        return self.mode == "attached"

    # ------------------------------------------------------------------ watching

    def _watch(self):
        """(alive(), exit_code()) of the current instance, bound to it, not to self."""
        inst, pid = self.inst, self.pid
        if inst is not None:
            return inst.alive, inst.exit_code
        if pid:
            return (lambda: discovery.pid_alive(pid)), (lambda: None)
        return (lambda: True), (lambda: None)

    def _stopped_message(self, code):
        text = "The RizomUV instance stopped (exit code %s). " % ("unknown" if code is None else code)
        if self.inst is not None and code not in (None, 0):
            text += "%s. " % launch.explain_exit_code(code).capitalize()
        text += "Unsaved work in it is lost. The next call will attach to or start a new one."
        if self.inst is not None:
            text += " Its output is in %s." % self.inst.log_path
        return text

    async def _await(self, worker, fut, *, label, progress, alive, attached):
        """The response to a submitted request, watching the instance meanwhile. Raises
        InstanceStopped when the instance dies first; a dead worker resolves fut itself."""
        t0 = time.monotonic()
        next_report = t0 + 1.0
        while not fut.done():
            await asyncio.wait({fut}, timeout=1.0)
            if fut.done():
                break
            if not alive():
                raise InstanceStopped()
            now = time.monotonic()
            if progress is not None and now >= next_report:
                elapsed = now - t0
                text = "%s: %.0f s" % (label, elapsed)
                if attached and elapsed >= 60:
                    text += DIALOG_HINT
                await progress(elapsed, text)
                next_report = now + 2.0
        return fut.result()

    # ------------------------------------------------------------------ connecting

    async def _ensure_worker(self):
        if self.worker is None or not self.worker.alive():
            if self.worker is not None:
                self.worker.kill()
            self.worker = await spawn_worker(self.config.log_level)
            log.debug("link worker started, pid %d", self.worker.pid)
        return self.worker

    async def _connect_worker(self, worker, port, *, token, timeout, alive, progress, label):
        fut = await worker.submit("connect", {"port": port, "token": token, "startup_timeout": timeout})
        resp = await self._await(worker, fut, label=label, progress=progress, alive=alive, attached=False)
        if not resp["ok"]:
            raise WorkerError(resp["error"]["kind"], resp["error"]["message"])
        return resp["result"]

    def _drop_worker(self):
        """A worker interrupted mid-request would answer the next caller late: replace it."""
        if self.worker is not None:
            self.worker.kill()
            self.worker = None

    async def ensure(self, progress=None):
        """Connected to a live instance on return, or ToolError."""
        async with self._lock:
            if self._closed:
                raise ToolError("The server is shutting down.")
            if self.connected:
                alive, code = self._watch()
                if alive():
                    if self.worker is None or not self.worker.alive():
                        await self._reconnect_locked(progress)
                    return
                msg = self._stopped_message(code())
                await self._reset_locked()
                raise ToolError(msg)
            await self._resolve_locked(progress)

    async def _resolve_locked(self, progress):
        self.notes = []
        if self.port_request is not None:
            await self._attach_locked(self.port_request, self._record_for_port(self.port_request),
                                      explicit=True, progress=progress)
            return
        if self.target in ("auto", "attach"):
            for record in await asyncio.to_thread(self._candidates):
                try:
                    await self._attach_locked(record.port, record, explicit=False, progress=progress)
                    return
                except _Skip as e:
                    self.notes.append(str(e))
            if self.target == "attach":
                raise ToolError(NO_INSTANCE + "".join("\n- " + n for n in self.notes))
        await self._launch_locked(progress)

    @staticmethod
    def _candidates():
        """The artist's open sessions (and anything we launched ourselves), newest first."""
        mine = os.getpid()
        return [r for r in discovery.list_instances() if r.artist_session or r.owner_pid == mine]

    @staticmethod
    def _record_for_port(port):
        return next((r for r in discovery.list_instances() if r.port == port), None)

    @staticmethod
    def _listener_pid(port):
        pids = tcptable.listening_pids(port)
        return next(iter(pids)) if pids and len(pids) == 1 else None

    async def _attach_locked(self, port, record, *, explicit, progress):
        fail = ToolError if explicit else _Skip
        where = "the RizomUV on port %d" % port + (" (pid %d)" % record.pid if record else "")
        if not await asyncio.to_thread(launch.port_listening, port):
            if explicit:
                raise ToolError("Nothing listens on link port %d. Start RizomUV with -id %d, or check the port."
                                % (port, port))
            raise _Skip("%s does not answer on its port." % where)
        lock = discovery.InstanceLock(discovery.lock_path(record) if record else discovery.port_lock_path(port))
        try:
            got = lock.acquire()
        except OSError as e:
            raise fail("Cannot create the client lock %s: %s" % (lock.path, e)) from None
        if not got:
            raise fail("%s is already driven by another assistant (its client lock %s is held)." % (where, lock.path))
        try:
            pid = record.pid if record else await asyncio.to_thread(self._listener_pid, port)
            worker = await self._ensure_worker()
            exclude = {os.getpid(), worker.pid} | ({pid} if pid else set())
            foreign = await asyncio.to_thread(tcptable.foreign_clients, port, exclude)
            if foreign:
                raise fail("%s already has another RizomUVLink client (%s). Two clients on one port receive "
                           "each other's answers: close the other one first."
                           % (where, ", ".join("pid %d, %s" % f for f in foreign)))
            alive = (lambda: discovery.pid_alive(pid)) if pid else (lambda: True)
            try:
                info = await self._connect_worker(worker, port, token=record.token if record else None,
                                                  timeout=ATTACH_TIMEOUT, alive=alive, progress=progress,
                                                  label="Connecting to %s" % where)
            except InstanceStopped:
                self._drop_worker()
                raise fail("%s stopped while connecting." % where) from None
            except WorkerError as e:
                raise fail("Could not connect to %s: %s" % (where, e.message)) from None
            if info["token_ok"] is False:
                raise fail("%s is not the instance its discovery file describes (token mismatch)." % where)
            if not info["startup_done"]:
                raise fail("%s did not finish starting within %.0f s." % (where, ATTACH_TIMEOUT))
        except asyncio.CancelledError:
            lock.release()
            self._drop_worker()     # it may still be inside connect: it would answer the next caller late
            raise
        except BaseException:
            lock.release()
            raise
        # An explicit port is always attached, whoever started it: the server only quits what
        # it provably launched (a discovered headless record naming this very process).
        owned = bool(not explicit and record is not None and record.headless and record.owner_pid == os.getpid())
        self._set_connected("headless" if owned else "attached", owned=owned, port=port, pid=pid, info=info,
                            record=record, lock=lock, exe=record.exe if record else None)

    async def _launch_locked(self, progress):
        try:
            exe, source = await asyncio.to_thread(launch.find_rizomuv_exe, self.config.exe)
            if progress is not None:
                await progress(0.0, LAUNCH_LABEL)
            inst = await asyncio.to_thread(launch.launch_headless, exe)
        except launch.LaunchError as e:
            raise ToolError(str(e)) from None
        ok = False
        t0 = time.monotonic()
        deadline = t0 + self.config.launch_timeout
        try:
            next_report = t0 + 2.0
            while not await asyncio.to_thread(inst.check_ready):
                now = time.monotonic()
                if now > deadline:
                    raise ToolError("RizomUV did not open its link port %d within %.0f s and was stopped. Its "
                                    "output is in %s." % (inst.port, self.config.launch_timeout, inst.log_path))
                if progress is not None and now >= next_report:
                    await progress(now - t0, "%s: %.0f s" % (LAUNCH_LABEL, now - t0))
                    next_report = now + 2.0
                await asyncio.sleep(0.25)
            worker = await self._ensure_worker()
            try:
                info = await self._connect_worker(worker, inst.port, token=None,
                                                  timeout=max(10.0, deadline - time.monotonic()),
                                                  alive=inst.alive, progress=progress, label=LAUNCH_LABEL)
            except InstanceStopped:
                raise ToolError(inst.describe_exit()) from None
            except WorkerError as e:
                raise ToolError("The headless RizomUV (pid %d) opened port %d but the link failed: %s Its "
                                "output is in %s." % (inst.pid, inst.port, e.message, inst.log_path)) from None
            if not info["startup_done"]:
                raise ToolError("The headless RizomUV (pid %d) did not finish starting within %.0f s. Its output "
                                "is in %s." % (inst.pid, self.config.launch_timeout, inst.log_path))
            ok = True
        except launch.LaunchError as e:
            raise ToolError(str(e)) from None
        finally:
            if not ok:
                self._drop_worker()
                await asyncio.to_thread(inst.terminate, 1.0)
        log.info("headless RizomUV %s ready in %.1f s (pid %d, port %d)", info["version"],
                 time.monotonic() - t0, inst.pid, inst.port)
        self._set_connected("headless", owned=True, inst=inst, port=inst.port, pid=inst.pid, info=info,
                            exe="%s (%s)" % (exe, source))

    def _set_connected(self, mode, *, owned, port, pid, info, inst=None, record=None, lock=None, exe=None):
        self._gen += 1
        self.mode, self.owned, self.inst = mode, owned, inst
        self.port, self.pid, self.record, self.instance_lock = port, pid, record, lock
        self.version, self.headless = info["version"], info["headless"]
        self.exe = exe
        self.connected_at = time.time()
        self.scene_cache = None
        log.info("%s RizomUV %s on port %d (pid %s)", "owned headless" if owned else mode, self.version, port, pid)

    async def _reconnect_locked(self, progress):
        """The same instance, a fresh socket (and a fresh worker if it died): the worker's
        connect drains whatever a dead client left behind."""
        worker = await self._ensure_worker()
        alive, code = self._watch()
        try:
            info = await self._connect_worker(worker, self.port, token=self.record.token if self.record else None,
                                              timeout=ATTACH_TIMEOUT, alive=alive, progress=progress,
                                              label="Reconnecting to RizomUV")
        except InstanceStopped:
            msg = self._stopped_message(code())
            await self._reset_locked()
            raise ToolError(msg) from None
        except WorkerError as e:
            self._drop_worker()
            raise ToolError("Could not reconnect to RizomUV on port %d: %s" % (self.port, e.message)) from None
        self.version = info["version"]

    async def _reset_locked(self):
        """Forget the current instance. An owned one is killed if it still runs (callers
        that want it to quit first do so before)."""
        if self.instance_lock is not None:
            self.instance_lock.release()
        self._drop_worker()
        if self.inst is not None:
            await asyncio.to_thread(self.inst.terminate, 1.0)
        self._clear()
        self.scene_cache = None
        self.last_pack = None

    async def _disconnect_locked(self):
        """Leave the current instance on purpose: an owned one is quit first."""
        if self.connected and self.owned:
            await self._quit_owned(timeout=5.0)
        elif self.worker is not None and self.worker.alive():
            await self.worker.close(0.3)
        await self._reset_locked()

    async def _quit_owned(self, timeout):
        worker = self.worker
        alive, _ = self._watch()
        if worker is not None and worker.alive() and alive():
            try:
                await worker.request("quit_instance", timeout=timeout)
            except (asyncio.TimeoutError, Exception) as e:   # noqa: BLE001 -- terminate() follows
                log.debug("quit_instance: %r", e)
        if self.inst is not None:
            end = time.monotonic() + CLOSE_EXIT_SECONDS
            while self.inst.alive() and time.monotonic() < end:
                await asyncio.sleep(0.05)

    # ------------------------------------------------------------------ calls

    async def call(self, op, args=None, *, label, progress=None):
        """One worker op on the connected instance (connecting first if needed). Every
        failure is a ToolError whose sentence says what happened and what state is left."""
        await self.ensure(progress)
        gen, worker, attached = self._gen, self.worker, self.attached
        alive, code = self._watch()
        self.inflight += 1
        try:
            fut = await worker.submit(op, args)
            resp = await self._await(worker, fut, label=label, progress=progress, alive=alive, attached=attached)
        except InstanceStopped:
            msg = self._stopped_message(code())
            async with self._lock:
                if gen == self._gen:
                    await self._reset_locked()
            raise ToolError(msg) from None
        finally:
            self.inflight -= 1
        if resp["ok"]:
            return resp["result"]
        kind, message = resp["error"]["kind"], resp["error"]["message"]
        if kind == "rizomuv":
            raise ToolError(message)
        if kind == "busy":
            raise _Busy(message)
        if kind in ("link_lost", "worker_dead", "not_connected"):
            await self._recover(gen, kind, message, label)
        if kind == "bad_request":
            raise ToolError("%s: %s" % (label, message))
        raise ToolError("%s failed in the link worker: %s" % (label, message))

    async def _recover(self, gen, kind, message, label):
        """After a lost link or a dead worker: reconnect when the instance lives, else
        reset. Always raises: the failed call's outcome is unknown."""
        what = {"link_lost": "The link to RizomUV was lost during %s (%s)" % (label, message),
                "worker_dead": "The link worker stopped during %s (%s)" % (label, message),
                "not_connected": "The link worker was not connected during %s" % label}[kind]
        async with self._lock:
            if gen != self._gen:
                raise ToolError("%s. The session has moved to another RizomUV since." % what)
            alive, code = self._watch()
            if not alive():
                msg = self._stopped_message(code())
                await self._reset_locked()
                raise ToolError(msg)
            if kind != "link_lost":
                self._drop_worker()
            try:
                await self._reconnect_locked(None)
            except ToolError as e:
                await self._reset_locked()
                raise ToolError("%s, and reconnecting failed: %s" % (what, e)) from None
        raise ToolError("%s. The link is re-established; whether the command ran is unknown, so check the scene "
                        "(session_info, measure) before retrying." % what)

    async def execute(self, command, params=None, *, label=None, progress=None, timeout_ms=30000):
        """A RizomUV command's raw result. A "busy" answer (another client's command runs)
        is retried for 30 s."""
        deadline = time.monotonic() + BUSY_RETRY_SECONDS
        delay = 0.5
        while True:
            try:
                result = await self.call("execute", {"command": command, "params": params, "timeout_ms": timeout_ms},
                                         label=label or command, progress=progress)
                return result["value"]
            except _Busy as e:
                if time.monotonic() > deadline:
                    raise ToolError("RizomUV is busy with another client's command and stayed busy for %.0f s "
                                    "(%s)" % (BUSY_RETRY_SECONDS, e)) from None
                await asyncio.sleep(delay)
                delay = min(delay * 2, 4.0)

    async def op(self, op, args=None, *, label, progress=None):
        """A worker op other than execute, with the same busy retry."""
        deadline = time.monotonic() + BUSY_RETRY_SECONDS
        while True:
            try:
                return await self.call(op, args, label=label, progress=progress)
            except _Busy as e:
                if time.monotonic() > deadline:
                    raise ToolError("RizomUV is busy with another client's command and stayed busy for %.0f s "
                                    "(%s)" % (BUSY_RETRY_SECONDS, e)) from None
                await asyncio.sleep(1.0)

    async def scene(self, progress=None):
        self.scene_cache = await self.op("scene", {}, label="Reading the scene", progress=progress)
        return self.scene_cache

    # ------------------------------------------------------------------ switching, info

    def _compatible(self, target, port):
        if not self.connected or not self._watch()[0]():
            return False
        if port is not None:
            return port == self.port
        if target == "headless":
            return self.owned
        if target == "attach":
            return self.attached
        return True

    async def switch(self, target, port, progress=None):
        """The connect tool: keep the current instance when it already matches, else leave it
        (quitting it if we own it) and resolve again."""
        if port is not None and target == "headless":
            raise ToolError("port attaches to an existing RizomUV and target='headless' starts a new one: "
                            "pass one or the other.")
        if port is None and target == "auto":
            port = self.config.port
        async with self._lock:
            if not self._compatible(target, port):
                if self.connected:
                    await self._disconnect_locked()
                self.target, self.port_request = target, port
        await self.ensure(progress)

    async def info(self):
        """What session_info reports. Never starts RizomUV, and never waits behind a
        running command (the scene summary is then the last one read)."""
        out = {"connected": self.connected, "mode": self.mode}
        if self.connected:
            alive, code = self._watch()
            out.update({"port": self.port, "pid": self.pid, "version": self.version, "headless": self.headless,
                        "owned": self.owned, "instance_alive": alive()})
            if self.inst is not None:
                out["log"] = str(self.inst.log_path)
            if not out["instance_alive"]:
                out["note"] = self._stopped_message(code())
            elif self.inflight or self._lock.locked():
                out["scene"] = self.scene_cache
                out["scene_note"] = "A command is running; this is the last scene summary read."
            else:
                try:
                    out["scene"] = await self.scene()
                except ToolError as e:
                    out["scene"] = None
                    out["scene_error"] = str(e)
            if self.attached:
                out["policy"] = ("This is the artist's RizomUV, their live scene: load refuses to replace it "
                                 "without replace_scene, save writes new files, run_command allows reads, "
                                 "undoable edits and undo only, and it is never quit from here.")
            elif self.owned:
                out["policy"] = ("A private headless RizomUV started by this server; it holds a licence seat and "
                                 "is quit when the session ends.")
        elif self._lock.locked():
            out["note"] = "Connecting to or starting a RizomUV right now."
        out["last_pack"] = self.last_pack
        out["candidates"] = await asyncio.to_thread(self._describe_candidates)
        try:
            exe, source = await asyncio.to_thread(launch.find_rizomuv_exe, self.config.exe)
            out["headless_launch_exe"] = {"path": str(exe), "source": source}
        except launch.LaunchError as e:
            out["headless_launch_exe"] = {"error": str(e)}
        out["config"] = {"instance": self.config.instance, "port": self.config.port, "exe": self.config.exe,
                         "launch_timeout": self.config.launch_timeout}
        if self.target != self.config.instance or self.port_request != self.config.port:
            out["config"]["switched_to"] = {"instance": self.target, "port": self.port_request}
        if self.notes:
            out["notes"] = list(self.notes)
        return out

    def _describe_candidates(self):
        found = []
        for r in discovery.list_instances():
            current = self.connected and r.port == self.port
            if current:
                locked = False
            else:
                probe = discovery.InstanceLock(discovery.lock_path(r))
                try:
                    locked = not probe.acquire()
                except OSError:
                    locked = None
                probe.release()
            found.append({"pid": r.pid, "port": r.port, "version": r.version, "headless": r.headless,
                          "artist_session": r.artist_session, "started_by_this_server": r.owner_pid == os.getpid(),
                          "locked_by_other": locked, "current": current})
        return found

    # ------------------------------------------------------------------ the end

    async def close(self):
        """Lifespan exit, bounded to about 1.5 s: an MCP client kills the server 2 s after it
        closed stdin. Owned instance: Quit (<= 1 s), then the job. Attached: let go, never Quit.
        Deliberately not under the lock: a launch in progress holds it for up to a minute,
        and its own cleanup stops what it started."""
        if self._closed:
            return
        self._closed = True
        try:
            if self.connected and self.owned:
                await self._quit_owned(timeout=CLOSE_QUIT_SECONDS)
                if self.inst is not None:
                    await asyncio.to_thread(self.inst.terminate, CLOSE_EXIT_SECONDS)
            worker, self.worker = self.worker, None
            if worker is not None:
                if self.connected and not self.owned:
                    await worker.close(0.3)
                else:
                    worker.kill()
                    await worker.wait_closed(0.3)
        finally:
            if self.instance_lock is not None:
                self.instance_lock.release()
            self._clear()
            atexit.unregister(self._atexit)

    def _atexit(self):
        """The backstop when the loop never ran close(): kill what we started. (The job
        object kills an owned instance anyway once this process is gone.)"""
        try:
            if self.worker is not None:
                self.worker.kill_now()
            if self.inst is not None:
                self.inst.terminate(0.5)
        except Exception:   # noqa: BLE001 -- nothing to report to at exit
            pass
