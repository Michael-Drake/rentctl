# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Drake

"""Tests for the liveness detector (WI-0051).

The governing constraint: **every test here must be able to fail.** The outage
this detector was built for lasted nine days because a broken thing produced no
signal, so a test suite that only ever exercises the healthy path would reproduce
the original defect one level up (``verify-in-the-created-configuration``). Each
check therefore gets its broken case first, and the healthy case second.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import install_plugin

from rentctl.core import doctor, wiring
from rentctl.core.doctor import FAIL, OK, UNKNOWN, WARN, Check, Report


@pytest.fixture(autouse=True)
def no_real_claude_home(tmp_path, monkeypatch):
    """Every hooks check now reads Claude Code's plugin register and settings.
    Point the default `~/.claude` at an empty tmp dir, and drop the machine's
    managed-settings file, so no test here answers differently on a machine
    where the plugin really is installed — the developer's own, for one."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(wiring, "MANAGED_SETTINGS_FILES", ())


def runner_returning(code: int, out: str = "", err: str = ""):
    """A probe seam that simulates one command result."""

    def _run(argv):
        return (code, out, err)

    return _run


# --- the shim check -------------------------------------------------------


def test_missing_shim_fails(monkeypatch):
    """The literal nine-day outage, first half: the command is simply not there."""
    monkeypatch.setattr(doctor.shutil, "which", lambda _c: None)
    check = doctor.check_shim("rent", runner=runner_returning(0, '{"ok": true}'))
    assert check.status == FAIL
    assert "does not resolve" in check.detail


def test_shim_that_exists_but_dies_on_import_fails(monkeypatch):
    """The nine-day outage exactly: the shim file is present and executable, and
    the command dies on import. Presence is not the question."""
    monkeypatch.setattr(doctor.shutil, "which", lambda _c: "/usr/local/bin/rent")
    check = doctor.check_shim(
        "rent",
        runner=runner_returning(1, "", "ModuleNotFoundError: No module named 'rentctl.cli'"),
    )
    assert check.status == FAIL
    assert "fails to run" in check.detail
    assert "ModuleNotFoundError" in check.detail


def test_shim_exiting_zero_without_json_is_unknown_not_ok(monkeypatch):
    """`UNKNOWN` must not collapse into `OK` (``declare-what-a-check-assumes``).

    A command that exits 0 and says nothing structured has not demonstrated the
    capability — and calling that "fine" is how a detector certifies the gap it
    exists to find.
    """
    monkeypatch.setattr(doctor.shutil, "which", lambda _c: "/usr/local/bin/rent")
    check = doctor.check_shim("rent", runner=runner_returning(0, "hello"))
    assert check.status == UNKNOWN
    assert check.status != OK


def test_shim_reporting_not_ok_fails(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda _c: "/usr/local/bin/rent")
    check = doctor.check_shim("rent", runner=runner_returning(0, '{"ok": false}'))
    assert check.status == FAIL


def test_healthy_shim_passes_and_records_its_probe(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda _c: "/usr/local/bin/rent")
    check = doctor.check_shim("rent", runner=runner_returning(0, '{"ok": true, "environments": []}'))
    assert check.status == OK
    # capture-the-probe: the verdict must carry what produced it.
    assert check.probe == "rent ls"


# --- the hook-wiring check ------------------------------------------------


def _write_settings(root: Path, commands: list[str]) -> Path:
    path = root / ".claude" / "settings.local.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionEnd": [
                        {
                            "matcher": "*",
                            "hooks": [{"type": "command", "command": c} for c in commands],
                        }
                    ]
                }
            }
        )
    )
    return path


def test_wired_hook_naming_an_unresolvable_command_fails(tmp_path, monkeypatch):
    """**The signature of the outage.** The settings file says `rent down --all`,
    the string is present and correct, and `rent` does not exist. Checking the
    file's *text* passes here; only resolving the command catches it."""
    _write_settings(tmp_path, ['rent down --all --reason session-end'])
    monkeypatch.setattr(doctor.shutil, "which", lambda _c: None)
    check = doctor.check_project_hooks("weather", tmp_path)
    assert check.status == FAIL
    assert "does not resolve" in check.detail


def test_wired_hook_that_resolves_passes(tmp_path, monkeypatch):
    _write_settings(tmp_path, ["rent down --all --reason session-end", "rent sweep"])
    monkeypatch.setattr(doctor.shutil, "which", lambda _c: "/usr/local/bin/rent")
    check = doctor.check_project_hooks("weather", tmp_path)
    assert check.status == OK
    assert "2 wired hook(s)" in check.detail


def test_foreign_hooks_are_not_ours_to_judge(tmp_path, monkeypatch):
    """A project wiring somebody else's tooling is not a rentctl failure — and a
    detector that fails on other people's commands gets muted."""
    _write_settings(tmp_path, ["some-other-tool --flag"])
    monkeypatch.setattr(doctor.shutil, "which", lambda _c: None)
    check = doctor.check_project_hooks("weather", tmp_path)
    assert check.status == WARN
    assert "wires no rentctl hooks" in check.detail


def test_enrolled_project_with_no_settings_warns(tmp_path):
    check = doctor.check_project_hooks("weather", tmp_path)
    assert check.status == WARN
    assert "no hooks wired" in check.detail


def test_unparseable_settings_is_unknown_not_ok(tmp_path):
    path = tmp_path / ".claude" / "settings.local.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    check = doctor.check_project_hooks("weather", tmp_path)
    assert check.status == UNKNOWN


# --- which source wires the hooks (WI-0070) -------------------------------


def _claude(tmp_path: Path, *, enabled: dict | None = None) -> Path:
    """A fake `~/.claude` with the plugin installed at user scope."""
    home = tmp_path / "claude"
    install_plugin(home, name="rentctl")
    if enabled is not None:
        (home / "settings.json").write_text(json.dumps({"enabledPlugins": enabled}))
    return home


def test_plugin_only_is_ok_with_source_plugin(tmp_path, monkeypatch):
    """**The false alarm WI-0070 is about.** The published install path: plugin
    enabled at user scope, and `init` deliberately wrote no hooks. Reading only
    settings.local.json warned "layer 3 alone" here."""
    monkeypatch.setattr(doctor.shutil, "which", lambda _c: "/usr/local/bin/rent")
    check = doctor.check_project_hooks("weather", tmp_path, claude_home=_claude(tmp_path))
    assert check.status == OK
    assert check.source == doctor.SOURCE_PLUGIN
    assert "plugin" in check.detail


def test_plugin_only_with_no_cli_fails(tmp_path, monkeypatch):
    """The plugin supplying hooks proves nothing if the command they call is
    missing (WI-0071) — the outage's signature again, one layer over."""
    monkeypatch.setattr(doctor.shutil, "which", lambda _c: None)
    check = doctor.check_project_hooks("weather", tmp_path, claude_home=_claude(tmp_path))
    assert check.status == FAIL
    assert check.source == doctor.SOURCE_PLUGIN
    assert wiring.INSTALL_COMMAND in check.detail


def test_settings_only_is_ok_with_source_settings(tmp_path, monkeypatch):
    _write_settings(tmp_path, ["rent down --all --reason session-end"])
    monkeypatch.setattr(doctor.shutil, "which", lambda _c: "/usr/local/bin/rent")
    check = doctor.check_project_hooks("weather", tmp_path, claude_home=tmp_path / "no-plugins")
    assert check.status == OK
    assert check.source == doctor.SOURCE_SETTINGS


def test_plugin_and_settings_is_double_wired_warn(tmp_path, monkeypatch):
    """Claude Code keeps a plugin's copy of a handler separate from a settings
    copy, so both fire. Teardown is idempotent under the project lock, so this
    is a WARN — redundancy, not breakage — and the detail says why."""
    _write_settings(tmp_path, ["rent down --all --reason session-end"])
    monkeypatch.setattr(doctor.shutil, "which", lambda _c: "/usr/local/bin/rent")
    check = doctor.check_project_hooks("weather", tmp_path, claude_home=_claude(tmp_path))
    assert check.status == WARN
    assert check.source == doctor.SOURCE_BOTH
    assert "double-wired" in check.detail
    assert "idempotent" in check.detail


def test_neither_source_keeps_the_layer_3_warning(tmp_path, monkeypatch):
    _write_settings(tmp_path, ["some-other-tool --flag"])
    monkeypatch.setattr(doctor.shutil, "which", lambda _c: "/usr/local/bin/rent")
    check = doctor.check_project_hooks("weather", tmp_path, claude_home=tmp_path / "no-plugins")
    assert check.status == WARN
    assert check.source == doctor.SOURCE_NONE
    assert "layer 3 alone" in check.detail


def test_a_disabled_plugin_supplies_nothing(tmp_path, monkeypatch):
    """`claude plugin disable` leaves the install register alone. Counting an
    installed-but-disabled plugin would certify hooks that never run."""
    monkeypatch.setattr(doctor.shutil, "which", lambda _c: "/usr/local/bin/rent")
    home = _claude(tmp_path, enabled={"rentctl@rentctl": False})
    check = doctor.check_project_hooks("weather", tmp_path, claude_home=home)
    assert check.status == WARN
    assert check.source == doctor.SOURCE_NONE
    assert "disabled" in check.detail


def test_a_project_scope_can_reenable_what_the_user_disabled(tmp_path, monkeypatch):
    """The most specific scope with an entry wins."""
    monkeypatch.setattr(doctor.shutil, "which", lambda _c: "/usr/local/bin/rent")
    home = _claude(tmp_path, enabled={"rentctl@rentctl": False})
    local = tmp_path / ".claude" / "settings.local.json"
    local.parent.mkdir(parents=True)
    local.write_text(json.dumps({"enabledPlugins": {"rentctl@rentctl": True}}))
    check = doctor.check_project_hooks("weather", tmp_path, claude_home=home)
    assert check.status == OK
    assert check.source == doctor.SOURCE_PLUGIN


def test_an_unreadable_plugin_register_is_unknown_not_ok(tmp_path, monkeypatch):
    """`declare-what-a-check-assumes`: with no settings hooks, whether anything
    is wired hinges on the plugin — and we could not read it."""
    monkeypatch.setattr(doctor.shutil, "which", lambda _c: "/usr/local/bin/rent")
    home = tmp_path / "claude"
    (home / "plugins").mkdir(parents=True)
    (home / "plugins" / wiring.INSTALLED_PLUGINS_FILE).write_text("{not json")
    check = doctor.check_project_hooks("weather", tmp_path, claude_home=home)
    assert check.status == UNKNOWN
    assert "cannot tell" in check.detail


def test_settings_hooks_with_an_unreadable_plugin_state_warn_not_ok(tmp_path, monkeypatch):
    """The hooks are wired; only the double-wiring is in doubt. Not OK, because
    "cannot tell" is never folded into OK — and not UNKNOWN, because what is in
    doubt is itself only WARN-grade."""
    _write_settings(tmp_path, ["rent down --all --reason session-end"])
    monkeypatch.setattr(doctor.shutil, "which", lambda _c: "/usr/local/bin/rent")
    home = _claude(tmp_path, enabled={"rentctl@rentctl": "yes"})
    check = doctor.check_project_hooks("weather", tmp_path, claude_home=home)
    assert check.status == WARN
    assert "cannot tell" in check.detail
    assert check.source == doctor.SOURCE_SETTINGS


def test_the_plugin_is_not_consulted_for_another_runtime(tmp_path, monkeypatch):
    from rentctl.core.runtimes import GEMINI_CLI

    monkeypatch.setattr(doctor.shutil, "which", lambda _c: "/usr/local/bin/rent")
    check = doctor.check_project_hooks(
        "weather", tmp_path, binding=GEMINI_CLI, claude_home=_claude(tmp_path)
    )
    assert check.source == doctor.SOURCE_NONE
    assert check.status == WARN


def test_as_dict_carries_the_source():
    assert Check("a", OK, "", source="plugin").as_dict()["source"] == "plugin"
    assert "source" not in Check("a", OK, "").as_dict()


def test_diagnose_sees_the_plugin(devctl_home, write_registry, tmp_path, monkeypatch):
    """End to end: the registry's project has no settings hooks and the plugin
    is enabled — the report must say OK/plugin, not warn."""
    source = tmp_path / "webapp"
    source.mkdir()
    write_registry(
        {
            "projects": {
                "webapp": {
                    "block": 5180,
                    "runner": "process",
                    "source_dir": str(source),
                    "profiles": {
                        "default": {"cmd": "npm run dev", "cwd": "/tmp/webapp", "port_env": "PORT"}
                    },
                }
            }
        }
    )
    monkeypatch.setattr(doctor.shutil, "which", lambda _c: "/usr/local/bin/rent")
    report = doctor.diagnose(
        devctl_home, runner=runner_returning(0, '{"ok": true}'), claude_home=_claude(tmp_path)
    )
    (hooks,) = [c for c in report.checks if c.name == "hooks:webapp"]
    assert hooks.status == OK
    assert hooks.as_dict()["source"] == "plugin"


# --- the registry check ---------------------------------------------------


def test_missing_registry_warns(devctl_home):
    check = doctor.check_registry(devctl_home)
    assert check.status == WARN


def test_corrupt_registry_fails(devctl_home, write_registry):
    write_registry({"projects": "not-an-object"})
    check = doctor.check_registry(devctl_home)
    assert check.status == FAIL


def test_valid_registry_passes(devctl_home, write_registry, sample_registry_data):
    write_registry(sample_registry_data)
    check = doctor.check_registry(devctl_home)
    assert check.status == OK
    assert "1 project(s)" in check.detail


# --- the report's own arithmetic ------------------------------------------


def test_unknown_exits_nonzero():
    """"I could not tell" must not be reported as health. A scheduler that only
    pages on FAIL would otherwise sleep through a check that never ran."""
    report = Report(checks=[Check("a", OK, ""), Check("b", UNKNOWN, "")])
    assert report.exit_code == 1
    assert report.ok is False


def test_warn_does_not_exit_nonzero():
    """A warning is a thing to fix, not a thing that is broken now. Paging on
    warnings is how a detector gets muted."""
    report = Report(checks=[Check("a", OK, ""), Check("b", WARN, "")])
    assert report.exit_code == 0
    assert report.ok is True


def test_report_counts_every_status():
    report = Report(
        checks=[Check("a", OK, ""), Check("b", WARN, ""), Check("c", FAIL, ""), Check("d", UNKNOWN, "")]
    )
    assert report.as_dict()["summary"] == {"ok": 1, "warn": 1, "fail": 1, "unknown": 1}


# --- end to end -----------------------------------------------------------


def test_diagnose_reports_per_project_and_survives_a_broken_world(
    devctl_home, write_registry, tmp_path, monkeypatch
):
    """The whole examination against a machine where everything is wrong at once:
    no shim, and an enrolled project whose source_dir does not exist."""
    write_registry(
        {
            "projects": {
                "webapp": {
                    "block": 5180,
                    "runner": "process",
                    "source_dir": str(tmp_path / "gone"),
                    "profiles": {
                        "default": {"cmd": "npm run dev", "cwd": "/tmp/webapp", "port_env": "PORT"}
                    },
                }
            }
        }
    )
    monkeypatch.setattr(doctor.shutil, "which", lambda _c: None)
    report = doctor.diagnose(devctl_home, runner=runner_returning(127, "", "not found"))

    assert report.ok is False
    names = [c.name for c in report.checks]
    assert "shim:rent" in names
    assert "hooks:webapp" in names
    assert any(c.status == FAIL for c in report.checks)


def test_diagnose_never_raises_on_a_registry_it_cannot_read(devctl_home, write_registry, monkeypatch):
    write_registry({"projects": {}})
    monkeypatch.setattr(doctor.shutil, "which", lambda _c: "/usr/local/bin/rent")
    report = doctor.diagnose(devctl_home, runner=runner_returning(0, '{"ok": true}'))
    assert isinstance(report, Report)


def test_registry_with_no_source_dir_is_unknown_not_skipped(devctl_home, write_registry, monkeypatch):
    """A project whose hooks cannot be located must say so, not vanish from the
    report — an omitted check reads as a passed one."""
    write_registry(
        {
            "projects": {
                "webapp": {
                    "block": 5180,
                    "runner": "process",
                    "profiles": {
                        "default": {"cmd": "npm run dev", "cwd": "/tmp/webapp", "port_env": "PORT"}
                    },
                }
            }
        }
    )
    monkeypatch.setattr(doctor.shutil, "which", lambda _c: "/usr/local/bin/rent")
    report = doctor.diagnose(devctl_home, runner=runner_returning(0, '{"ok": true}'))
    hooks = [c for c in report.checks if c.name == "hooks:webapp"]
    assert len(hooks) == 1
    assert hooks[0].status == UNKNOWN


# --- the gaps that would otherwise ship untested ---------------------------


def test_as_dict_carries_the_probe():
    """``capture-the-probe``: the serialized report must keep the evidence, not
    just the verdict — a JSON consumer that sees only a status has the same
    problem the nine days had."""
    assert Check("a", OK, "fine", probe="rent ls").as_dict()["probe"] == "rent ls"
    assert "probe" not in Check("a", OK, "fine").as_dict()


def test_subprocess_runner_runs_a_real_command():
    """The default probe seam itself. Every other test injects a fake runner, so
    without this the code that actually talks to the machine is unexercised —
    the exact shape of gap this module exists to detect."""
    code, out, _err = doctor._subprocess_runner([doctor.sys.executable, "-c", "print('hi')"])
    assert code == 0
    assert out.strip() == "hi"


def test_subprocess_runner_reports_a_missing_binary_as_127():
    code, _out, err = doctor._subprocess_runner(["/nonexistent/definitely-not-here"])
    assert code == 127
    assert err


def test_subprocess_runner_times_out_without_raising(monkeypatch):
    """A hung probe must not take the detector down with it."""
    monkeypatch.setattr(doctor, "PROBE_TIMEOUT", 1)
    code, _out, err = doctor._subprocess_runner(
        [doctor.sys.executable, "-c", "import time; time.sleep(30)"]
    )
    assert code == 124
    assert "timed out" in err


def test_install_check_reads_the_real_package():
    """Whatever this repo's install shape is, the check must answer with one of
    its declared statuses rather than raising."""
    check = doctor.check_install_is_durable()
    assert check.status in (OK, WARN, UNKNOWN, FAIL)
    assert check.name == "install"


def test_install_check_passes_for_a_built_artifact(monkeypatch):
    """**The production path.** The suite runs from a source tree, so without
    this the branch that will actually execute on every installed copy is never
    exercised — and a check that only ever runs its own dev-time branch is the
    untested-in-the-created-configuration failure this module is about."""
    import rentctl

    monkeypatch.setattr(
        rentctl, "__file__", "/opt/tools/rentctl/lib/python3.13/site-packages/rentctl/__init__.py"
    )
    check = doctor.check_install_is_durable()
    assert check.status == OK
    assert "built artifact" in check.detail


def test_install_check_is_unknown_when_the_package_hides_its_origin(monkeypatch):
    import rentctl

    monkeypatch.setattr(rentctl, "__file__", "")
    check = doctor.check_install_is_durable()
    assert check.status == UNKNOWN


def test_hook_commands_ignores_malformed_entries():
    """Settings files are other people's data. Every shape here has been seen in
    the wild or is one bad merge away (``untrusted-data-stays-untrusted``)."""
    assert doctor._hook_commands({"hooks": "not-a-dict"}) == []
    assert doctor._hook_commands({"hooks": {"SessionEnd": "not-a-list"}}) == []
    assert doctor._hook_commands({"hooks": {"SessionEnd": ["not-a-dict"]}}) == []
    assert doctor._hook_commands({"hooks": {"SessionEnd": [{"hooks": ["not-a-dict"]}]}}) == []
    assert doctor._hook_commands({"hooks": {"SessionEnd": [{"hooks": [{"command": ""}]}]}}) == []
    assert doctor._hook_commands({"hooks": {"SessionEnd": [{"hooks": [{"command": 7}]}]}}) == []
    # An unbalanced quote must not take the detector down.
    assert doctor._hook_commands(
        {"hooks": {"SessionEnd": [{"hooks": [{"command": 'rent "'}]}]}}
    ) == []
    assert doctor._hook_commands({}) == []


def test_hook_check_falls_back_to_the_legacy_settings_file(tmp_path, monkeypatch):
    """Projects enrolled before ADR-0014 carry their hooks in `settings.json`.
    Reporting those as 'no hooks wired' would be a false alarm."""
    legacy = tmp_path / ".claude" / "settings.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionEnd": [
                        {"hooks": [{"type": "command", "command": "rent down --all"}]}
                    ]
                }
            }
        )
    )
    monkeypatch.setattr(doctor.shutil, "which", lambda _c: "/usr/local/bin/rent")
    check = doctor.check_project_hooks("weather", tmp_path)
    assert check.status == OK


def test_hook_check_on_a_non_object_settings_file_is_unknown(tmp_path):
    path = tmp_path / ".claude" / "settings.local.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1, 2, 3]")
    check = doctor.check_project_hooks("weather", tmp_path)
    assert check.status == UNKNOWN
    assert "not a JSON object" in check.detail


def test_hook_named_by_absolute_path_resolves_without_PATH(tmp_path, monkeypatch):
    """A hook wired as an absolute path is legitimate and must not read as broken
    just because the bare name is not on PATH."""
    shim = tmp_path / "rent"
    shim.write_text("#!/bin/sh\n")
    _write_settings(tmp_path, [f"{shim} down --all"])
    monkeypatch.setattr(doctor.shutil, "which", lambda _c: None)
    check = doctor.check_project_hooks("weather", tmp_path)
    assert check.status == OK


def test_diagnose_returns_the_report_when_the_registry_races_away(
    devctl_home, write_registry, sample_registry_data, monkeypatch
):
    """The registry passed its check and then failed to load — a real race with
    `rent init` holding the machine-wide lock. Report what we have; never raise."""
    write_registry(sample_registry_data)
    monkeypatch.setattr(doctor.shutil, "which", lambda _c: "/usr/local/bin/rent")

    calls = {"n": 0}
    real_load = doctor.Registry.load

    def flaky(path):
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("vanished")
        return real_load(path)

    monkeypatch.setattr(doctor.Registry, "load", staticmethod(flaky))
    report = doctor.diagnose(devctl_home, runner=runner_returning(0, '{"ok": true}'))
    assert isinstance(report, Report)
    assert not any(c.name.startswith("hooks:") for c in report.checks)


def test_main_prints_json_and_returns_the_exit_code(capsys, devctl_home, monkeypatch):
    """The scheduled entry point. Its exit code is the whole alarm."""
    monkeypatch.setattr(doctor.shutil, "which", lambda _c: None)
    code = doctor.main([])
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["ok"] is False
    assert code == 1


# --- Codex (ADR-0018 §8) ---------------------------------------------------

TRUSTED = """
[hooks.state."rentctl@rentctl:hooks/hooks.json:session_start:0:0"]
trusted_hash = "sha256:aaa"

[hooks.state."rentctl@rentctl:hooks/hooks.json:session_end:0:0"]
trusted_hash = "sha256:bbb"
"""


def _codex(tmp_path, text=None):
    home = tmp_path / "codex-home"
    home.mkdir(exist_ok=True)
    if text is not None:
        (home / "config.toml").write_text(text)
    return home


def _by_name(checks):
    return {c.name: c for c in checks}


def test_codex_home_honours_the_env_variable(tmp_path):
    assert doctor.codex_home({"CODEX_HOME": str(tmp_path)}) == tmp_path
    assert doctor.codex_home({}) == Path.home() / ".codex"
    assert doctor.codex_home({"CODEX_HOME": "  "}) == Path.home() / ".codex"


def test_codex_is_present_by_binary_or_by_home(tmp_path):
    missing = tmp_path / "nope"
    assert doctor.codex_present(missing, which=lambda _c: None) is False
    assert doctor.codex_present(missing, which=lambda _c: "/usr/local/bin/codex") is True
    assert doctor.codex_present(_codex(tmp_path), which=lambda _c: None) is True


def test_codex_without_config_is_simply_not_installed(tmp_path):
    (check,) = doctor.check_codex(_codex(tmp_path))
    assert check.name == "codex:plugin" and check.status == OK
    assert "not installed" in check.detail and "codex plugin add rentctl@rentctl" in check.detail


def test_codex_config_without_the_plugin_is_ok_and_names_the_install(tmp_path):
    (check,) = doctor.check_codex(_codex(tmp_path, 'model = "x"\n[plugins."other@m"]\nenabled = true\n'))
    assert check.status == OK and "not installed" in check.detail


def test_a_non_table_plugins_key_holds_no_rentctl_plugin(tmp_path):
    (check,) = doctor.check_codex(_codex(tmp_path, 'plugins = "rentctl@rentctl"\n'))
    assert check.status == OK and "not installed" in check.detail


def test_an_unreadable_codex_config_is_couldnt_tell(tmp_path):
    """declare-what-a-check-assumes: an unparseable file is not "not installed"."""
    (check,) = doctor.check_codex(_codex(tmp_path, "this is = = not toml ["))
    assert check.status == UNKNOWN and check.detail.startswith("couldn't tell")


def test_enabled_trusted_unshadowed_plugin_is_all_ok(tmp_path):
    home = _codex(tmp_path, '[plugins."rentctl@rentctl"]\nenabled = true\n' + TRUSTED)
    checks = _by_name(doctor.check_codex(home))
    assert set(checks) == {"codex:plugin", "codex:hooks", "codex:mcp-shadow"}
    assert all(c.status == OK for c in checks.values()), checks
    assert "trust entry present" in checks["codex:hooks"].detail


def test_any_marketplace_counts(tmp_path):
    home = _codex(tmp_path, '[plugins."rentctl@my-fork"]\nenabled = true\n' + TRUSTED.replace(
        "rentctl@rentctl", "rentctl@my-fork"))
    checks = _by_name(doctor.check_codex(home))
    assert checks["codex:plugin"].status == OK and "rentctl@my-fork" in checks["codex:plugin"].detail
    assert checks["codex:hooks"].status == OK


def test_a_disabled_plugin_warns(tmp_path):
    home = _codex(tmp_path, '[plugins."rentctl@rentctl"]\nenabled = false\n' + TRUSTED)
    checks = _by_name(doctor.check_codex(home))
    assert checks["codex:plugin"].status == WARN and "disabled" in checks["codex:plugin"].detail


def test_a_plugin_entry_without_enabled_is_couldnt_tell(tmp_path):
    home = _codex(tmp_path, '[plugins."rentctl@rentctl"]\n' + TRUSTED)
    checks = _by_name(doctor.check_codex(home))
    assert checks["codex:plugin"].status == UNKNOWN


def test_untrusted_hooks_warn_and_name_the_fix(tmp_path):
    """Codex runs no hook until it is trusted in /hooks, plugin hooks included."""
    home = _codex(tmp_path, '[plugins."rentctl@rentctl"]\nenabled = true\n')
    checks = _by_name(doctor.check_codex(home))
    hooks = checks["codex:hooks"]
    assert hooks.status == WARN
    assert "run /hooks in Codex" in hooks.detail and "lease expiry" in hooks.detail
    assert "session_start" in hooks.detail and "session_end" in hooks.detail


def test_half_trusted_hooks_warn_naming_the_missing_event(tmp_path):
    only_start = TRUSTED.split("[hooks.state.\"rentctl@rentctl:hooks/hooks.json:session_end")[0]
    home = _codex(tmp_path, '[plugins."rentctl@rentctl"]\nenabled = true\n' + only_start)
    hooks = _by_name(doctor.check_codex(home))["codex:hooks"]
    assert hooks.status == WARN and "session_end" in hooks.detail and "session_start" not in hooks.detail


def test_an_empty_trusted_hash_is_not_trust(tmp_path):
    home = _codex(tmp_path, '[plugins."rentctl@rentctl"]\nenabled = true\n'
                  + TRUSTED.replace('"sha256:bbb"', '""'))
    assert _by_name(doctor.check_codex(home))["codex:hooks"].status == WARN


def test_a_malformed_hooks_state_is_couldnt_tell(tmp_path):
    home = _codex(tmp_path, '[plugins."rentctl@rentctl"]\nenabled = true\n[hooks]\nstate = 3\n')
    assert _by_name(doctor.check_codex(home))["codex:hooks"].status == UNKNOWN


def test_a_config_mcp_server_shadowing_the_plugin_warns(tmp_path):
    home = _codex(tmp_path, '[plugins."rentctl@rentctl"]\nenabled = true\n' + TRUSTED
                  + '\n[mcp_servers.rentctl]\ncommand = "rent-mcp"\n')
    shadow = _by_name(doctor.check_codex(home))["codex:mcp-shadow"]
    assert shadow.status == WARN
    assert "[mcp_servers.rentctl]" in shadow.detail and "delete" in shadow.detail


def test_diagnose_includes_codex_only_when_codex_is_present(devctl_home, write_registry, tmp_path, monkeypatch):
    write_registry({"projects": {}})
    monkeypatch.setattr(doctor.shutil, "which", lambda c: None if c == "codex" else "/usr/local/bin/rent")
    report = doctor.diagnose(devctl_home, runner=runner_returning(0, '{"ok": true}'),
                             codex_home_dir=tmp_path / "absent")
    assert not any(c.name.startswith("codex") for c in report.checks)

    home = _codex(tmp_path, '[plugins."rentctl@rentctl"]\nenabled = true\n')
    report = doctor.diagnose(devctl_home, runner=runner_returning(0, '{"ok": true}'), codex_home_dir=home)
    names = [c.name for c in report.checks]
    assert {"codex:plugin", "codex:hooks", "codex:mcp-shadow"} <= set(names)


def test_diagnose_reads_codex_home_from_the_environment(devctl_home, write_registry, tmp_path, monkeypatch):
    write_registry({"projects": {}})
    monkeypatch.setattr(doctor.shutil, "which", lambda c: None if c == "codex" else "/usr/local/bin/rent")
    home = _codex(tmp_path, '[plugins."rentctl@rentctl"]\nenabled = false\n')
    monkeypatch.setenv("CODEX_HOME", str(home))
    report = doctor.diagnose(devctl_home, runner=runner_returning(0, '{"ok": true}'))
    assert _by_name(report.checks)["codex:plugin"].status == WARN
