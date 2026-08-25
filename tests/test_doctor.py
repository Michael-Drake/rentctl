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

from rentctl.core import doctor
from rentctl.core.doctor import FAIL, OK, UNKNOWN, WARN, Check, Report


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
