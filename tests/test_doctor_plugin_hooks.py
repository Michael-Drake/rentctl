"""`rent doctor` reads the hooks an INSTALLED plugin actually wires (1.2.0, D2).

The acceptance run broke a plugin's SessionEnd command: `claude plugin validate
--strict` passed and doctor said `hooks: ok`. These checks read the installed
copy and compare it with this rentctl's render.
"""

from __future__ import annotations

import json
from pathlib import Path

from rentctl.core import doctor, wiring


def _plugin(root: Path, *, hooks_file=True, inline=None, mutate=None) -> Path:
    (root / ".claude-plugin").mkdir(parents=True)
    manifest = wiring.plugin_manifest()
    if inline is not None:
        manifest["hooks"] = inline
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest))
    if hooks_file:
        data = wiring.plugin_hooks_file()
        if mutate:
            mutate(data)
        (root / "hooks").mkdir()
        (root / "hooks" / "hooks.json").write_text(json.dumps(data))
    return root


def test_a_faithful_install_is_ok(tmp_path):
    c = doctor.check_installed_plugin_hooks("x", _plugin(tmp_path / "p"), inline_ok=True)
    assert c.status == doctor.OK, c.detail


def test_a_broken_session_end_command_is_a_warning_not_ok(tmp_path):
    def break_it(d):
        d["hooks"]["SessionEnd"][0]["hooks"][0]["command"] = "rent sessionend"

    c = doctor.check_installed_plugin_hooks("x", _plugin(tmp_path / "p", mutate=break_it), inline_ok=True)
    assert c.status == doctor.WARN
    assert "differ" in c.detail


def test_no_session_end_hook_is_a_failure(tmp_path):
    def drop(d):
        del d["hooks"]["SessionEnd"]

    c = doctor.check_installed_plugin_hooks("x", _plugin(tmp_path / "p", mutate=drop), inline_ok=True)
    assert c.status == doctor.FAIL
    assert "cleanup is OFF" in c.detail


def test_codex_ignores_a_bare_inline_map(tmp_path):
    """1.1.0's shape: inline hooks only. Claude Code loads them; Codex does not."""
    inline = wiring.plugin_hooks_fragment()
    root = _plugin(tmp_path / "p", hooks_file=False, inline=inline)
    assert doctor.check_installed_plugin_hooks("c", root, inline_ok=False).status == doctor.FAIL
    assert doctor.check_installed_plugin_hooks("x", root, inline_ok=True).status in (doctor.OK, doctor.WARN)


def test_an_unparseable_hook_file_is_unknown(tmp_path):
    root = _plugin(tmp_path / "p")
    (root / "hooks" / "hooks.json").write_text("{nope")
    assert doctor.check_installed_plugin_hooks("x", root, inline_ok=True).status == doctor.UNKNOWN


def test_a_missing_install_dir_is_a_warning(tmp_path):
    c = doctor.check_installed_plugin_hooks("x", tmp_path / "gone", inline_ok=True)
    assert c.status == doctor.WARN


def test_roots_come_from_the_install_registers(tmp_path):
    claude = tmp_path / "claude"
    (claude / "plugins").mkdir(parents=True)
    (claude / "plugins" / "installed_plugins.json").write_text(json.dumps({
        "version": 2,
        "plugins": {
            "rentctl@rentctl": [{"scope": "user", "installPath": str(tmp_path / "c1")}],
            "other@x": [{"scope": "user", "installPath": str(tmp_path / "o")}],
        },
    }))
    assert doctor.claude_plugin_roots(claude) == [tmp_path / "c1"]

    codex = tmp_path / "codex"
    (codex / "plugins" / "cache" / "rentctl" / "rentctl" / "1.2.0").mkdir(parents=True)
    (codex / "plugins" / "cache" / "m" / "other" / "1.0.0").mkdir(parents=True)
    assert doctor.codex_plugin_roots(codex) == [codex / "plugins" / "cache" / "rentctl" / "rentctl" / "1.2.0"]


def test_diagnose_reports_the_installed_claude_plugin(tmp_path, devctl_home):
    root = _plugin(tmp_path / "inst")
    claude = tmp_path / "claude"
    (claude / "plugins").mkdir(parents=True)
    (claude / "plugins" / "installed_plugins.json").write_text(json.dumps(
        {"version": 2, "plugins": {"rentctl@rentctl": [{"scope": "user", "installPath": str(root)}]}}
    ))
    report = doctor.diagnose(devctl_home, claude_home=claude, codex_home_dir=tmp_path / "nocodex",
                             runner=lambda argv: (0, "rentctl 1.2.0", ""))
    names = {c.name: c.status for c in report.checks}
    assert names.get("plugin-hooks:claude-code") == doctor.OK
