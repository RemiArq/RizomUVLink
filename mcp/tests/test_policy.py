"""policy.py: what run_command passes through, per instance kind."""

import json

import pytest

from rizomuv_mcp import docs, policy

ATTACHED, HEADLESS = True, False


@pytest.fixture(scope="module")
def shipped_commands():
    path = docs.shipped_module_path()
    if path is None:
        pytest.skip("RizomUVLinkBase.py is not next to the package")
    return sorted(docs.parse_module_file(path))


def test_every_shipped_command_is_classified(shipped_commands):
    assert len(shipped_commands) == 60
    missing = [c for c in shipped_commands if policy.rule_info(c) is None]
    assert not missing, f"commands without a policy: {missing}"
    stale = sorted(set(policy.classified()) - set(shipped_commands))
    assert not stale, f"policy entries for commands the module does not have: {stale}"


def test_rule_info_is_plain(shipped_commands):
    for name in shipped_commands:
        info = policy.rule_info(name)
        json.dumps(info)
        assert info["category"] in policy.CATEGORIES
        assert isinstance(info["allowed_attached"], bool) and isinstance(info["allowed_headless"], bool)
        if info["allowed_attached"]:
            assert info["allowed_headless"], f"{name}: allowed on the artist's instance but not headless?"


def test_every_denial_has_a_reason(shipped_commands):
    for name in shipped_commands:
        for attached in (ATTACHED, HEADLESS):
            allowed, reason = policy.check(name, {}, attached)
            assert isinstance(allowed, bool) and isinstance(reason, str) and reason


@pytest.mark.parametrize("name", ["Get", "GetAsString", "GetVersion", "Count", "ItemNames", "Eval",
                                  "Unfold", "Optimize", "Pack", "Cut", "Weld", "Select", "Hide",
                                  "Undo", "Redo", "SnapshotWindowTree", "UiEventLog"])
def test_reads_scene_and_undo_are_allowed_everywhere(name):
    params = "Vars.Infos.Version.Full" if name in ("Get", "GetAsString", "GetVersion", "Count",
                                                     "ItemNames", "Eval") else {}
    assert policy.check(name, params, ATTACHED)[0]
    assert policy.check(name, params, HEADLESS)[0]


@pytest.mark.parametrize("name,tool", [("Load", "load tool"), ("Save", "save tool"),
                                       ("RasterExport", "render_layout")])
def test_attached_denials_name_the_dedicated_tool(name, tool):
    allowed, reason = policy.check(name, {}, ATTACHED)
    assert not allowed and tool in reason
    assert policy.check(name, {}, HEADLESS)[0]


@pytest.mark.parametrize("name", ["PsExport", "GenerateCheckerboardTexture", "LoadGridTexture",
                                  "LoadUserTexture", "ResetVars"])
def test_headless_only(name):
    assert not policy.check(name, {}, ATTACHED)[0]
    assert policy.check(name, {}, HEADLESS)[0]


@pytest.mark.parametrize("name", ["Quit", "Exit", "Test", "InitLib", "LibTaskUpdate", "LibTaskEnd", "Loop",
                                  "GenPythonModule", "GenerateHelp", "GenerateScriptingHelp", "Subscribe",
                                  "SavePreferences", "SaveControls", "LoadPrefs", "LoadControls",
                                  "ResetPrefs", "ResetControls", "MigrateUserData", "UiLayout",
                                  "ToolbarLayout", "TriggerFeature"])
def test_always_denied(name):
    assert not policy.check(name, {}, ATTACHED)[0]
    assert not policy.check(name, {}, HEADLESS)[0]


def test_denial_texts():
    assert "lifecycle" in policy.check("Quit", {}, HEADLESS)[1]
    assert "rizomuv://command" in policy.check("GenerateScriptingHelp", {}, ATTACHED)[1]


@pytest.mark.parametrize("path", ["Lib", "Lib.Mesh", "Lib.Scene", "", "  Lib.Mesh. "])
def test_get_guard(path):
    for name in ("Get", "GetAsString"):
        for attached in (ATTACHED, HEADLESS):
            allowed, reason = policy.check(name, path, attached)
            assert not allowed and ("crash" in reason or "serialized" in reason)


@pytest.mark.parametrize("path", ["Lib.Mesh.Islands", "Lib.Mesh.SAvg", "Vars.Infos", "Lib.Meshes"])
def test_get_guard_lets_precise_paths_through(path):
    assert policy.check("Get", path, ATTACHED)[0]


@pytest.mark.parametrize("params,allowed", [(None, True), ({}, True), ("", True), ({"MaxDepth": 2}, True),
                                            ("C:/tmp/tree.json", False), ({"Path": "C:/tmp/t.json"}, False)])
def test_snapshot_guard(params, allowed):
    for name in ("SnapshotWindowTree", "SnapshotFeatureTree", "UiEventLog"):
        assert policy.check(name, params, ATTACHED)[0] is allowed
        assert policy.check(name, params, HEADLESS)[0] is allowed


def test_ui_event_log_guard():
    assert policy.check("UiEventLog", {"Max": 20}, ATTACHED)[0]
    assert not policy.check("UiEventLog", {"Clear": True}, ATTACHED)[0]
    assert not policy.check("UiEventLog", {"Enable": False}, HEADLESS)[0]


def test_set():
    ok = {"Path": "Vars.WindowManager.Visibility.Log", "Value": False}
    assert not policy.check("Set", ok, ATTACHED)[0]
    assert policy.check("Set", ok, HEADLESS)[0]
    for path in ("Prefs.Pack.Padding", "Prefs", "Vars.RizomUVLink.InstanceToken"):
        allowed, reason = policy.check("Set", {"Path": path, "Value": 1}, HEADLESS)
        assert not allowed and reason
    assert not policy.check("Set", "Vars.X", HEADLESS)[0]  # not the dict form


def test_unknown_commands_are_denied():
    allowed, reason = policy.check("FormatDisk", {}, HEADLESS)
    assert not allowed and "not" in reason and "classified" in reason
    allowed, reason = policy.check("unfold", {}, ATTACHED)
    assert not allowed and "'Unfold'" in reason
