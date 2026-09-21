"""Which RizomUV commands `run_command` passes through, and on which instance.

Two kinds of instance, two levels of trust:
- attached: the RizomUV the artist has open. Their live scene, their undo history, their
  preferences, their current file. Only reads, undoable scene edits and undo itself go
  through; everything with a dedicated tool is sent to that tool, which carries the
  guardrails (no Load over their scene, saves to new files, renders to a private folder).
- headless: a private instance this server started. Loading, saving and session state are
  fair game there, but the lifecycle still belongs to the server, and preferences still
  persist beyond the session.

Every command of the generated client module must be classified here
(tests/test_policy.py enumerates them); anything else is denied as not classified, so a
command added to RizomUV is closed until someone decides what it may do.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any, Callable

CATEGORIES = {
    "read": "reads the data tree or the UI state; changes nothing",
    "scene": "changes the scene; one undo step per call",
    "undo": "walks the undo history",
    "load": "replaces the whole scene; not undoable",
    "save_export": "writes files",
    "set": "writes any data-tree value, preferences included; not undoable by default",
    "textures": "changes the viewport texture of the session",
    "prefs_ui": "changes preferences, controls, UI layout or UI state",
    "internal": "RizomUV plumbing, tests and generators",
    "quit": "ends the RizomUV process",
}

_HEADLESS_ONLY = "only allowed on a headless instance started by this server"
_WRITES_FILE = ("with a path it writes a file; call it without Path (or a path string) and read "
                "the returned JSON")


@dataclass(frozen=True)
class Rule:
    category: str
    attached: bool
    headless: bool
    denied: str = ""  # why, when the instance kind denies it
    guard: Callable[[Any], str | None] | None = None  # params -> denial reason, or None


def _path_of(params):
    if isinstance(params, str):
        return params
    if isinstance(params, dict):
        path = params.get("Path")
        return path if isinstance(path, str) else None
    return None


def _get_guard(params):
    path = _path_of(params)
    if path is None:
        return None
    path = path.strip().rstrip(".")
    if path == "":
        return ("Get on the root serializes the whole data tree, Lib included, which crashes RizomUV. "
                "Read a precise path; ItemNames lists a container's children.")
    if path == "Lib":
        return ("Get('Lib') crashes RizomUV. Read its leaves instead: ItemNames('Lib.Mesh') lists them; "
                "measure and diagnose give the UV statistics.")
    if path in ("Lib.Mesh", "Lib.Scene"):
        return (f"Get('{path}') cannot be serialized over the link. Read its leaves instead: "
                f"ItemNames('{path}') lists them; measure and diagnose give the UV statistics.")
    return None


def _snapshot_guard(params):
    if isinstance(params, str) and params.strip():
        return _WRITES_FILE + "."
    if isinstance(params, dict) and params.get("Path"):
        return _WRITES_FILE + "."
    return None


def _ui_event_log_guard(params):
    reason = _snapshot_guard(params)
    if reason:
        return reason
    if isinstance(params, dict) and ("Clear" in params or "Enable" in params):
        return ("Clear and Enable change the UI event recording of the session; read the log "
                "without them.")
    return None


def _set_guard(params):
    if not isinstance(params, dict) or not isinstance(params.get("Path"), str):
        return 'Set takes {"Path": "<dotted.path>", "Value": <value>} (optionally "UndoAble": true).'
    path = params["Path"].strip()
    if path == "Prefs" or path.startswith("Prefs."):
        return ("Prefs.* are the application preferences: they persist beyond this session and are "
                "not set from here.")
    if path == "Vars.RizomUVLink" or path.startswith("Vars.RizomUVLink."):
        return "Vars.RizomUVLink.* is the link's own state (instance identity); it is not set from here."
    return None


_RULES: dict[str, Rule] = {}


def _add(names, category, attached, headless, denied="", guard=None):
    for name in names.split():
        _RULES[name] = Rule(category, attached, headless, denied, guard)


_add("Get GetAsString", "read", True, True, guard=_get_guard)
_add("GetVersion Count ItemNames Eval", "read", True, True)
_add("SnapshotWindowTree SnapshotFeatureTree", "read", True, True, guard=_snapshot_guard)
_add("UiEventLog", "read", True, True, guard=_ui_event_log_guard)

# Undoable lib tasks (CTask default): each link call is its own undo group.
_add("Unfold Optimize Pack Cut Weld Select Hide Constrain Deform IslandProperties PolygonProperties "
     "IslandGroups IslandCopy Uvset Hotspot Tag PaintMap ResetTo3d SymmetrySet", "scene", True, True)
_add("Undo Redo", "undo", True, True)

_add("Load", "load", False, True,
     "Load over the link replaces the artist's scene without asking to save it. Use the load tool, "
     "which refuses to replace a scene unless replace_scene is true.")
_add("Save", "save_export", False, True,
     "Save over the link overwrites files without asking and can retarget the artist's current file. "
     "Use the save tool: it writes a new file and leaves the artist's file path alone.")
_add("RasterExport", "save_export", False, True,
     "Use the render_layout tool, which renders the layout to a private folder and returns the image.")
_add("PsExport", "save_export", False, True, f"PsExport writes files; {_HEADLESS_ONLY}.")
_add("Set", "set", False, True,
     f"Set writes the artist's session state or preferences without undo; {_HEADLESS_ONLY}.",
     guard=_set_guard)
_add("GenerateCheckerboardTexture LoadGridTexture LoadUserTexture", "textures", False, True,
     f"Changes the viewport texture of the artist's session; {_HEADLESS_ONLY}.")
_add("ResetVars", "prefs_ui", False, True,
     f"ResetVars resets the artist's user interface state; {_HEADLESS_ONLY}.")

_PREFS = ("Preferences and controls persist beyond this session (and a headless instance shares "
          "them with nobody who asked for the change); they are not driven from here.")
_add("SavePreferences SaveControls LoadPrefs LoadControls ResetPrefs ResetControls MigrateUserData",
     "prefs_ui", False, False, _PREFS)
_add("UiLayout", "prefs_ui", False, False, "UiLayout rearranges RizomUV's windows; not driven from here.")
_add("ToolbarLayout", "prefs_ui", False, False,
     "ToolbarLayout imports or exports toolbar layout files; not driven from here.")
_add("TriggerFeature", "prefs_ui", False, False,
     "TriggerFeature drives a GUI control and can open a modal dialog that blocks the link until "
     "someone answers it; not driven from here.")

_add("Quit Exit", "quit", False, False,
     "The server owns the RizomUV lifecycle: a headless instance it started is quit when the session "
     "ends, and the artist's RizomUV is never quit from here (Quit has no save prompt).")
_add("Test", "internal", False, False, "Test runs RizomUV's internal code tests.")
_add("InitLib", "internal", False, False, "InitLib re-initialises the library, undo history included.")
_add("LibTaskUpdate LibTaskEnd Loop", "internal", False, False, "Internal plumbing of RizomUV.")
_add("GenPythonModule", "internal", False, False,
     "GenPythonModule writes the client module to a file; read the rizomuv://command/<name> "
     "resources instead.")
_add("GenerateHelp", "internal", False, False,
     "GenerateHelp writes the HTML help to disk; read the rizomuv://command/<name> resources instead.")
_add("GenerateScriptingHelp", "internal", False, False,
     "GenerateScriptingHelp returns nothing over the link; read the rizomuv://command/<name> "
     "resources instead.")
_add("Subscribe", "internal", False, False,
     "Subscribe replaces the change-notification watch list for every client and binds a second "
     "port; not driven from here.")


def classified() -> list[str]:
    """Every command name the table knows, sorted."""
    return sorted(_RULES)


def rule_info(command: str) -> dict | None:
    """{category, allowed_attached, allowed_headless, note} for a listing, None if unknown.

    `allowed_*` is True for commands allowed with conditions on their parameters.
    """
    rule = _RULES.get(command)
    if rule is None:
        return None
    return {"category": rule.category, "allowed_attached": rule.attached,
            "allowed_headless": rule.headless, "note": rule.denied or CATEGORIES[rule.category]}


def check(command: str, params: Any, attached: bool) -> tuple[bool, str]:
    """(allowed, reason). The reason says why a command is refused and what to use instead;
    for an allowed command it describes what the command does to the instance."""
    rule = _RULES.get(command)
    if rule is None:
        hint = difflib.get_close_matches(command, list(_RULES), n=1, cutoff=0.8)
        folded = {name.lower(): name for name in _RULES}.get(str(command).lower())
        suggestion = folded or (hint[0] if hint else None)
        text = (f"'{command}' is not a RizomUV command this server has classified, so it is not "
                "passed through.")
        if suggestion:
            text += f" Did you mean '{suggestion}'?"
        return False, text
    if not (rule.attached if attached else rule.headless):
        return False, rule.denied
    if rule.guard is not None:
        reason = rule.guard(params)
        if reason:
            return False, reason
    return True, CATEGORIES[rule.category]
