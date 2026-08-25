"""Unit tests for enrollment wiring — MCP registration and session hooks (ADR-0002 §1, §5, §7).

The merge behaviour here is the whole point: `.mcp.json` and `.claude/settings.json`
almost always already exist with other content, and enrollment that replaces them
breaks the project it was meant to enroll.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rentctl.core import runtimes, wiring


def read(p):
    return json.loads(p.read_text())


def settings_with(command, event="SessionEnd"):
    return {"hooks": {event: [{"matcher": "*", "hooks": [
        {"type": "command", "command": command, "timeout": 30}
    ]}]}}


# --- the single source both delivery paths render from (ADR-0002 §5) ------

def test_hooks_fragment_has_both_session_boundaries():
    frag = wiring.hooks_fragment()
    assert set(frag) == {"SessionStart", "SessionEnd"}
    end = frag["SessionEnd"][0]["hooks"][0]["command"]
    start = frag["SessionStart"][0]["hooks"][0]["command"]
    assert "down --all" in end
    assert "sweep" in start


def test_mcp_fragment_registers_the_stdio_server():
    assert wiring.mcp_fragment() == {"command": "rent-mcp", "args": []}


def test_print_hooks_is_valid_json_and_matches_the_fragment():
    """`--print-hooks` is the escape hatch for nonstandard setups (ADR-0002 §7);
    it must emit exactly what init would have written, not a prose approximation."""
    text = wiring.render_hooks_text()
    assert json.loads(text)["hooks"] == wiring.hooks_fragment()


# --- .mcp.json merge — the named hazard -----------------------------------

def test_mcp_merge_creates_file_when_absent(tmp_path):
    p = tmp_path / ".mcp.json"
    wiring.merge_mcp_config(p)
    assert read(p)["mcpServers"]["rentctl"] == wiring.mcp_fragment()


def test_mcp_merge_preserves_existing_servers(tmp_path):
    p = tmp_path / ".mcp.json"
    p.write_text(json.dumps({
        "mcpServers": {"other": {"command": "other-mcp", "args": ["--x"]}},
        "somethingElse": {"keep": True},
    }))
    wiring.merge_mcp_config(p)
    data = read(p)
    assert data["mcpServers"]["other"] == {"command": "other-mcp", "args": ["--x"]}
    assert data["mcpServers"]["rentctl"] == wiring.mcp_fragment()
    assert data["somethingElse"] == {"keep": True}


# --- the ADR-0009 rename: a legacy entry is renamed, not joined ------------

def test_mcp_merge_renames_a_legacy_devctl_entry(tmp_path):
    """A project enrolled before ADR-0009 has a `devctl` key. Two entries would
    start two stdio servers advertising the same four tools in one session."""
    p = tmp_path / ".mcp.json"
    p.write_text(json.dumps({
        "mcpServers": {
            "devctl": {"command": "devctl-mcp", "args": []},
            "other": {"command": "other-mcp", "args": []},
        }
    }))
    assert wiring.merge_mcp_config(p) is True
    servers = read(p)["mcpServers"]
    assert "devctl" not in servers
    assert servers["rentctl"] == wiring.mcp_fragment()
    assert servers["other"] == {"command": "other-mcp", "args": []}


def test_mcp_merge_renames_even_when_the_new_entry_already_exists(tmp_path):
    """Both keys present is the state a half-finished migration leaves behind —
    the idempotence short-circuit must not read it as 'already done'."""
    p = tmp_path / ".mcp.json"
    p.write_text(json.dumps({
        "mcpServers": {
            "devctl": {"command": "devctl-mcp", "args": []},
            "rentctl": wiring.mcp_fragment(),
        }
    }))
    assert wiring.merge_mcp_config(p) is True
    assert list(read(p)["mcpServers"]) == ["rentctl"]


def test_mcp_merge_is_idempotent(tmp_path):
    p = tmp_path / ".mcp.json"
    wiring.merge_mcp_config(p)
    first = p.read_text()
    wiring.merge_mcp_config(p)
    assert p.read_text() == first


def test_a_touched_file_keeps_its_non_ascii_text_verbatim(tmp_path):
    """We edit files we do not own. A one-hook change must not rewrite the
    project's own prose: `ensure_ascii=True` turns every em-dash into \\u2014 and
    buries the real edit in a whole-file diff (observed on webapp, 2026-07-31)."""
    p = tmp_path / ".mcp.json"
    p.write_text(json.dumps({
        "//note": "generated — do not hand-edit; the “next” push overwrites it",
        "mcpServers": {},
    }, ensure_ascii=False))
    wiring.merge_mcp_config(p)
    raw = p.read_text()
    assert "generated — do not hand-edit" in raw
    assert "\\u2014" not in raw
    assert read(p)["mcpServers"][wiring.SERVER_NAME] == wiring.mcp_fragment()


def test_mcp_merge_refuses_unparseable_file(tmp_path):
    """Overwriting a file we cannot read is how enrollment eats someone's config."""
    p = tmp_path / ".mcp.json"
    p.write_text("{not json")
    with pytest.raises(Exception):
        wiring.merge_mcp_config(p)
    assert p.read_text() == "{not json"  # untouched


# --- settings.json hook install -------------------------------------------

def test_hooks_install_creates_file_when_absent(tmp_path):
    p = tmp_path / "settings.json"
    wiring.install_hooks(p)
    assert read(p)["hooks"]["SessionEnd"] == wiring.hooks_fragment()["SessionEnd"]


def test_hooks_install_preserves_unrelated_hooks_and_keys(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "x"}]}]},
        "permissions": {"allow": ["Bash(ls:*)"]},
    }))
    wiring.install_hooks(p)
    data = read(p)
    assert data["hooks"]["PreToolUse"][0]["matcher"] == "Bash"
    assert "SessionEnd" in data["hooks"]
    assert data["permissions"] == {"allow": ["Bash(ls:*)"]}


def test_hooks_install_appends_to_an_existing_same_event(tmp_path):
    """A project with its own SessionEnd hook keeps it — devctl adds, never replaces."""
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({
        "hooks": {"SessionEnd": [{"matcher": "*", "hooks": [{"type": "command", "command": "mine"}]}]}
    }))
    wiring.install_hooks(p)
    commands = [
        h["command"]
        for group in read(p)["hooks"]["SessionEnd"]
        for h in group["hooks"]
    ]
    assert "mine" in commands
    assert any("down --all" in c for c in commands)


def test_hooks_install_is_idempotent(tmp_path):
    p = tmp_path / "settings.json"
    wiring.install_hooks(p)
    first = p.read_text()
    wiring.install_hooks(p)
    assert p.read_text() == first


def test_a_legacy_devctl_hook_is_repaired_to_the_rent_spelling(tmp_path):
    """webapp and sampleapp have `devctl down --all` committed (ADR-0009 §4).

    Two failures are possible here and the repair path has to avoid both: not
    recognising the old spelling appends a second hook that tears the same
    environment down twice, and recognising it but blessing it leaves the project
    on a command that ADR-0009 §4 eventually drops. Ownership is by first token
    (`OWNED_COMMANDS`), so the old entry is ours to rewrite in place.
    """
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({
        "hooks": {
            "SessionEnd": [{
                "matcher": "*",
                "hooks": [{
                    "type": "command",
                    "command": 'devctl down --all --cwd "$CLAUDE_PROJECT_DIR" --reason session-end',
                }],
            }]
        }
    }))
    result = wiring.install_hooks_detailed(p)
    assert result.repaired == ("SessionEnd",)
    assert result.installed == ("SessionStart",)  # absent in the fixture, so added
    end = read(p)["hooks"]["SessionEnd"]
    assert len(end) == 1
    assert end[0]["hooks"][0]["command"].startswith("rent ")


def test_a_hook_the_project_wrote_itself_is_never_rewritten(tmp_path):
    """The repair path only touches commands whose first token we own — a
    project's own `devctl-wrapper.sh` is not ours."""
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({
        "hooks": {
            "SessionEnd": [{
                "matcher": "*",
                "hooks": [{"type": "command", "command": "./devctl-wrapper.sh --tidy"}],
            }]
        }
    }))
    wiring.install_hooks(p)
    commands = [h["command"] for g in read(p)["hooks"]["SessionEnd"] for h in g["hooks"]]
    assert "./devctl-wrapper.sh --tidy" in commands
    assert any(c.startswith("rent down") for c in commands)


# --- writing into a generated file is provisional (WI-0027) ---------------

def test_a_generated_settings_file_makes_the_write_provisional(tmp_path):
    """The write happens — it is still the best repair available now — but the
    result says it will be reverted. Reporting plain success here is what let
    webapp's `--reason session-end` die twice: added 07-29, stripped by a
    substrate refresh 07-31, with three days of teardowns uncounted in between."""
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({
        "//generated": "GENERATED by curate/gen_settings.py — the next push overwrites it",
    }))
    result = wiring.install_hooks_detailed(p)
    assert result.changed is True
    assert result.provisional is not None
    assert "//generated" in result.provisional         # names what tipped us off
    assert "--print-hooks" in result.provisional       # names the way out
    assert read(p)["hooks"]["SessionEnd"]              # and it did still write
    # ADR-0014: the warning must not name any particular consumer's config. This
    # message is user-facing output from a tool published to strangers, and the
    # previous wording told every reader to edit `session.config.json` under
    # `settings_extras` — a file that exists in exactly one fleet and nowhere else.
    assert "settings_extras" not in result.provisional
    assert "session.config.json" not in result.provisional
    assert "federation" not in result.provisional.lower()


def test_an_ordinary_settings_file_is_not_flagged(tmp_path):
    """`provisional` must stay absent on the normal path, or it becomes noise a
    reader learns to skip — and then it is not a warning any more."""
    p = tmp_path / "settings.json"
    result = wiring.install_hooks_detailed(p)
    assert result.changed is True
    assert result.provisional is None


def test_a_generated_file_needing_no_change_is_not_flagged(tmp_path):
    """No write, nothing to revert. Warning about a no-op trains the reader to
    ignore the field."""
    p = tmp_path / "settings.json"
    wiring.install_hooks(p)
    data = read(p)
    data["//generated"] = "generated"
    p.write_text(json.dumps(data))
    result = wiring.install_hooks_detailed(p)
    assert result.changed is False
    assert result.provisional is None


def test_generated_marker_ignores_an_unparseable_file(tmp_path):
    """Detection must not raise where the writer has its own, better-worded
    refusal for the same file."""
    p = tmp_path / "settings.json"
    p.write_text("{not json")
    assert wiring.generated_marker(p) is None


# --- carrying an MCP approval across the rename (WI-0039) -----------------
#
# The governing constraint is that this list is an APPROVAL list. Every test
# below exists to hold the line between "carry a decision the user already made"
# and "make one on their behalf."

def approvals(*names):
    return {"enabledMcpjsonServers": list(names)}


def test_a_legacy_approval_is_renamed_to_the_current_server(tmp_path):
    """Both pilot projects' exact state: `.mcp.json` says rentctl, the approval
    list still says devctl, so the approved server does not exist and the
    existing one is unapproved."""
    p = tmp_path / "settings.local.json"
    p.write_text(json.dumps(approvals("memory", wiring.LEGACY_SERVER_NAME, "github")))
    assert wiring.rename_approved_mcp_server(p) is True
    assert read(p)["enabledMcpjsonServers"] == ["memory", wiring.SERVER_NAME, "github"]


def test_an_unapproved_project_is_not_approved_for_us(tmp_path):
    """The line this function must not cross. A project that never approved
    devctl does not get rentctl written in — that would be devctl granting
    itself a permission on the user's behalf. The runtime's own gate asks."""
    p = tmp_path / "settings.local.json"
    p.write_text(json.dumps(approvals("memory")))
    assert wiring.rename_approved_mcp_server(p) is False
    assert read(p)["enabledMcpjsonServers"] == ["memory"]


def test_an_absent_approval_list_is_not_created(tmp_path):
    p = tmp_path / "settings.local.json"
    p.write_text(json.dumps({"permissions": {"allow": []}}))
    assert wiring.rename_approved_mcp_server(p) is False
    assert "enabledMcpjsonServers" not in read(p)


def test_an_already_renamed_list_is_a_no_op(tmp_path):
    """Idempotent, and does not rewrite — enrollment runs this every time."""
    p = tmp_path / "settings.local.json"
    p.write_text(json.dumps(approvals(wiring.SERVER_NAME)))
    before = p.read_text()
    assert wiring.rename_approved_mcp_server(p) is False
    assert p.read_text() == before


def test_a_list_carrying_both_names_does_not_end_up_with_a_duplicate(tmp_path):
    """A half-migrated project could legitimately hold both. Renaming naively
    would approve the same server twice."""
    p = tmp_path / "settings.local.json"
    p.write_text(json.dumps(approvals(wiring.SERVER_NAME, wiring.LEGACY_SERVER_NAME)))
    assert wiring.rename_approved_mcp_server(p) is True
    assert read(p)["enabledMcpjsonServers"] == [wiring.SERVER_NAME]


def test_other_peoples_approvals_are_preserved(tmp_path):
    p = tmp_path / "settings.local.json"
    data = approvals("memory", wiring.LEGACY_SERVER_NAME)
    data["permissions"] = {"allow": ["Bash(ls:*)"]}
    data["disabledMcpjsonServers"] = ["filesystem"]
    p.write_text(json.dumps(data))
    wiring.rename_approved_mcp_server(p)
    got = read(p)
    assert got["permissions"] == {"allow": ["Bash(ls:*)"]}
    assert got["disabledMcpjsonServers"] == ["filesystem"]
    assert "memory" in got["enabledMcpjsonServers"]


def test_a_missing_file_is_not_an_error(tmp_path):
    p = tmp_path / "settings.local.json"
    assert wiring.rename_approved_mcp_server(p) is False
    assert not p.exists()


def test_a_runtime_with_no_approval_concept_is_a_no_op(tmp_path):
    """Gemini has no such list in its bundled docs, so its binding declares
    none. That must be a no-op rather than a crash or an invented file."""
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(approvals(wiring.LEGACY_SERVER_NAME)))
    assert runtimes.GEMINI_CLI.mcp_enable_key is None
    assert wiring.rename_approved_mcp_server(p, runtimes.GEMINI_CLI) is False
    assert read(p)["enabledMcpjsonServers"] == [wiring.LEGACY_SERVER_NAME]


# --- clearing the pre-move hook location (ADR-0014) -----------------------
#
# `remove_hooks` DELETES from a file devctl does not own, which is the most
# dangerous thing in this module. Every test below exists to bound that: what it
# removes, what it must never touch, and what it leaves behind when done.

def test_removing_our_hook_leaves_the_file_without_it(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(settings_with(f'{wiring.COMMAND} down --all')))
    assert wiring.remove_hooks(p) == ("SessionEnd",)
    assert "hooks" not in read(p)


def test_removing_our_hook_preserves_the_projects_own(tmp_path):
    """The whole reason ownership is decided by the command's first token. A
    migration that took someone's unrelated SessionEnd hook with it would be a
    far worse bug than the stale copy it was cleaning up."""
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"hooks": {"SessionEnd": [
        {"matcher": "*", "hooks": [{"type": "command", "command": "./my-cleanup.sh"}]},
        {"matcher": "*", "hooks": [{"type": "command", "command": f"{wiring.COMMAND} down --all"}]},
    ]}}))
    assert wiring.remove_hooks(p) == ("SessionEnd",)
    groups = read(p)["hooks"]["SessionEnd"]
    assert len(groups) == 1
    assert groups[0]["hooks"][0]["command"] == "./my-cleanup.sh"


def test_a_legacy_devctl_spelling_is_removed_too(tmp_path):
    """Projects enrolled before the rename carry `devctl …`, not `rent …`. The
    move has to clean those or the migration silently skips every project that
    predates ADR-0009 — the ones most likely to be carrying a stale hook."""
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(settings_with('devctl down --all --cwd "$CLAUDE_PROJECT_DIR"')))
    assert wiring.remove_hooks(p) == ("SessionEnd",)
    assert "hooks" not in read(p)


def test_removing_keeps_everything_that_is_not_hooks(tmp_path):
    p = tmp_path / "settings.json"
    data = settings_with(f'{wiring.COMMAND} down --all')
    data["permissions"] = {"allow": ["Bash(git status:*)"]}
    p.write_text(json.dumps(data))
    wiring.remove_hooks(p)
    assert read(p)["permissions"] == {"allow": ["Bash(git status:*)"]}


def test_removing_nothing_of_ours_is_a_no_op(tmp_path):
    """Returns `()` AND does not rewrite the file. A no-op that still rewrites
    would put devctl in the diff of every project it never enrolled."""
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"hooks": {"SessionEnd": [
        {"matcher": "*", "hooks": [{"type": "command", "command": "./my-cleanup.sh"}]},
    ]}}))
    before = p.read_text()
    assert wiring.remove_hooks(p) == ()
    assert p.read_text() == before


def test_removing_from_an_absent_file_is_not_an_error(tmp_path):
    """The normal case for a project enrolled after the move: there is no old
    location to clean. It must not raise, and must not create the file."""
    p = tmp_path / "settings.json"
    assert wiring.remove_hooks(p) == ()
    assert not p.exists()


def test_removing_from_a_file_with_no_hooks_map_is_a_no_op(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"permissions": {"allow": []}}))
    assert wiring.remove_hooks(p) == ()


def test_removing_our_hook_from_one_event_leaves_the_other(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"hooks": {
        "SessionEnd": [{"matcher": "*", "hooks": [
            {"type": "command", "command": f"{wiring.COMMAND} down --all"}]}],
        "PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": "./guard.sh"}]}],
    }}))
    assert wiring.remove_hooks(p) == ("SessionEnd",)
    hooks = read(p)["hooks"]
    assert "SessionEnd" not in hooks
    assert hooks["PreToolUse"][0]["hooks"][0]["command"] == "./guard.sh"


def test_install_then_remove_round_trips_to_the_original(tmp_path):
    """Enrollment must be reversible on the file it touched — nothing of ours
    left over, and nothing of theirs lost (P21 / reversible writes)."""
    p = tmp_path / "settings.json"
    original = {"permissions": {"allow": ["Bash(ls:*)"]}}
    p.write_text(json.dumps(original))
    wiring.install_hooks(p)
    wiring.remove_hooks(p)
    assert read(p) == original


# --- the plugin manifest is a render, not a copy (WI-0008, ADR-0002 §5) ---

MANIFEST_PATH = Path(__file__).resolve().parent.parent / "plugin" / ".claude-plugin" / "plugin.json"


def test_the_checked_in_manifest_matches_what_wiring_renders():
    """The whole point of WI-0008. The plugin is the only channel that installs
    the HOOKS as well as the server, so a manifest that drifts from `init`'s hook
    text ships two different cleanup contracts under one name. This test is what
    makes the checked-in file a render rather than a hand-kept second copy —
    regenerate with `wiring.render_plugin_manifest()`, never by editing."""
    assert MANIFEST_PATH.read_text() == wiring.render_plugin_manifest()


def test_the_manifest_hooks_are_the_same_object_init_writes():
    manifest = wiring.plugin_manifest()
    assert manifest["hooks"]["hooks"] == wiring.hooks_fragment()
    assert manifest["mcpServers"][wiring.SERVER_NAME] == wiring.mcp_fragment()


def test_the_manifest_carries_the_only_required_field():
    """Claude Code rejects a manifest with no `name`; everything else is optional."""
    assert wiring.plugin_manifest()["name"] == wiring.SERVER_NAME


def test_the_manifest_declares_no_version():
    """rentctl's version is an open decision (WI-0023). A build script must not
    mint a version claim ahead of a release record."""
    assert "version" not in wiring.plugin_manifest()


def test_the_manifest_keeps_non_ascii_readable():
    """The description is what a user reads to decide whether to install this.
    json.dumps' default would render the em-dash as an escape — the same defect
    already fixed for the files enrollment writes."""
    assert "—" in wiring.render_plugin_manifest()
    assert "\\u2014" not in wiring.render_plugin_manifest()


# --- plugin detection (ADR-0002 §5) ---------------------------------------

def test_plugin_absent_by_default(tmp_path):
    assert wiring.plugin_installed(tmp_path) is False


def test_plugin_detected_when_marker_present(tmp_path):
    (tmp_path / "plugins" / "rentctl").mkdir(parents=True)
    assert wiring.plugin_installed(tmp_path) is True


def test_plugin_detected_under_the_legacy_name(tmp_path):
    """A plugin installed before the rename still counts — missing it makes init
    write hooks the plugin already supplies."""
    (tmp_path / "plugins" / "devctl").mkdir(parents=True)
    assert wiring.plugin_installed(tmp_path) is True
