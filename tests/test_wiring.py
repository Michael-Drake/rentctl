"""Unit tests for enrollment wiring — MCP registration and session hooks (ADR-0002 §1, §5, §7).

The merge behaviour here is the whole point: `.mcp.json` and `.claude/settings.json`
almost always already exist with other content, and enrollment that replaces them
breaks the project it was meant to enroll.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

import rentctl
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
    assert manifest["hooks"] == wiring.hooks_fragment()
    assert manifest["mcpServers"][wiring.SERVER_NAME] == wiring.mcp_fragment()


def test_the_manifest_hooks_are_not_wrapped_in_a_settings_file_envelope():
    """The defect that shipped in 1.0.0. `hooks` here is the event map itself;
    the `{"hooks": {...}}` envelope is the *settings file* shape. Wrapped, Claude
    Code reads `hooks` as the event name, matches nothing, and ignores every entry
    at runtime — so the plugin installs no cleanup while looking installed. That
    is the one failure mode the plugin channel exists to remove.

    Asserted on the KEYS rather than the nesting, so any future envelope fails
    too: every key here must be a real hook event."""
    hooks = wiring.plugin_manifest()["hooks"]
    assert "hooks" not in hooks
    assert set(hooks) == {"SessionEnd", "SessionStart"}


def test_the_manifest_carries_the_only_required_field():
    """Claude Code rejects a manifest with no `name`; everything else is optional."""
    assert wiring.plugin_manifest()["name"] == wiring.SERVER_NAME


def test_the_manifest_version_tracks_the_package():
    """Absent while rentctl had no release (WI-0023); 1.0.0 settled that. Read
    from the package so a version bump cannot update one and miss the other —
    a plugin declaring no version cannot be updated by someone who has it."""
    assert wiring.plugin_manifest()["version"] == rentctl.__version__


MARKETPLACE_PATH = Path(__file__).resolve().parent.parent / ".claude-plugin" / "marketplace.json"


def test_the_checked_in_marketplace_matches_what_wiring_renders():
    assert MARKETPLACE_PATH.read_text() == wiring.render_marketplace_manifest()


def test_the_marketplace_lists_the_plugin_it_ships():
    """1.0.0 shipped `plugin/` with no marketplace manifest, so there was no route
    to install it by — a correct plugin nobody could reach. This is that route,
    and the listing is rendered from the plugin manifest so the two cannot
    describe different things."""
    market = wiring.marketplace_manifest()
    plugin = wiring.plugin_manifest()
    (entry,) = market["plugins"]
    assert entry["name"] == plugin["name"]
    assert entry["description"] == plugin["description"]
    assert entry["source"] == wiring.PLUGIN_SOURCE


def test_the_marketplace_source_points_at_a_real_plugin():
    """The source is a path relative to the repo root, and a typo in it is
    invisible until someone tries to install."""
    root = MARKETPLACE_PATH.parent.parent
    source = wiring.marketplace_manifest()["plugins"][0]["source"]
    assert (root / source / ".claude-plugin" / "plugin.json").is_file()


# --- the MCP registry entry (WI-0009) -------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_JSON_PATH = REPO_ROOT / "server.json"
PUBLIC_README = REPO_ROOT / "README-public.md"


def test_the_checked_in_server_json_matches_what_wiring_renders():
    assert SERVER_JSON_PATH.read_text() == wiring.render_registry_manifest()


def test_the_registry_versions_track_the_package():
    """Both must equal what is actually on PyPI. A hand-kept server.json drifts
    at every release and the registry then advertises a version nobody can
    install."""
    entry = wiring.registry_manifest()
    assert entry["version"] == rentctl.__version__
    assert entry["packages"][0]["version"] == rentctl.__version__
    assert entry["packages"][0]["identifier"] == wiring.SERVER_NAME


def test_the_registry_description_fits_the_registry_limit():
    """The registry rejects a longer description with HTTP 422 — found by
    validating rather than by reading a schema, which does not state the cap."""
    assert len(wiring.REGISTRY_DESCRIPTION) <= wiring.REGISTRY_DESCRIPTION_LIMIT


def test_the_published_readme_carries_the_ownership_marker():
    """How the registry proves we own the PyPI package: it greps the PUBLISHED
    README for this token. It is checked against the artifact on PyPI, not this
    repo, so a release that drops it makes the registry publish fail with nothing
    in the repo looking wrong. 1.0.0 shipped without it, which is why the registry
    entry could not be published against 1.0.0 at all."""
    assert wiring.MCP_NAME_MARKER in PUBLIC_README.read_text()


def test_the_marker_names_the_server_it_publishes():
    assert wiring.REGISTRY_NAME in wiring.MCP_NAME_MARKER
    assert wiring.registry_manifest()["name"] == wiring.REGISTRY_NAME


def test_the_registry_entry_names_the_console_script_not_the_distribution():
    """`rent-mcp` is the executable; `rentctl` is the distribution. A client that
    assumes they match runs nothing, which is why runtimeHint and the explicit
    --from spelling are both present."""
    package = wiring.registry_manifest()["packages"][0]
    args = package["packageArguments"]
    assert package["runtimeHint"] == "uvx"
    assert {"type": "named", "name": "--from", "value": wiring.SERVER_NAME} in args
    assert {"type": "positional", "value": f"{wiring.COMMAND}-mcp"} in args


def test_the_manifest_keeps_non_ascii_readable():
    """The description is what a user reads to decide whether to install this.
    json.dumps' default would render the em-dash as an escape — the same defect
    already fixed for the files enrollment writes."""
    assert "—" in wiring.render_plugin_manifest()
    assert "\\u2014" not in wiring.render_plugin_manifest()


# --- plugin detection (ADR-0002 §5) ---------------------------------------

def write_register(claude_home, plugins):
    """Claude Code's install register, in the shape observed on disk after a real
    `claude plugin install` (WI-0028) — not a shape invented for the test."""
    d = claude_home / "plugins"
    d.mkdir(parents=True, exist_ok=True)
    (d / wiring.INSTALLED_PLUGINS_FILE).write_text(
        json.dumps({"version": 2, "plugins": plugins})
    )


def test_plugin_absent_by_default(tmp_path):
    assert wiring.plugin_installed(tmp_path) is False


def test_plugin_absent_when_the_register_is_unreadable(tmp_path):
    """Absent is the fail-safe answer: init writes the hooks itself and
    install_hooks is idempotent. A false POSITIVE leaves a project with none."""
    (tmp_path / "plugins").mkdir(parents=True)
    (tmp_path / "plugins" / wiring.INSTALLED_PLUGINS_FILE).write_text("{not json")
    assert wiring.plugin_installed(tmp_path) is False


def test_plugin_detected_at_user_scope(tmp_path):
    write_register(tmp_path, {"rentctl@rentctl": [{"scope": "user"}]})
    assert wiring.plugin_installed(tmp_path) is True


def test_plugin_detected_under_the_legacy_name(tmp_path):
    """A plugin installed before the rename still counts — missing it makes init
    write hooks the plugin already supplies."""
    write_register(tmp_path, {"devctl@devctl": [{"scope": "user"}]})
    assert wiring.plugin_installed(tmp_path) is True


def test_plugin_detected_from_any_marketplace(tmp_path):
    """The key is `<plugin>@<marketplace>`. Where someone installed it from is
    not our business; the plugin name is."""
    write_register(tmp_path, {"rentctl@somebody-elses-list": [{"scope": "user"}]})
    assert wiring.plugin_installed(tmp_path) is True


def test_a_local_install_counts_only_for_its_own_project(tmp_path):
    """The false positive that matters. A local-scope install supplies hooks to
    ONE project; counting it elsewhere makes init skip writing hooks for a
    project the plugin does not cover, leaving it with no cleanup at all."""
    mine, theirs = tmp_path / "mine", tmp_path / "theirs"
    mine.mkdir()
    theirs.mkdir()
    write_register(tmp_path, {"rentctl@rentctl": [{"scope": "local", "projectPath": str(mine)}]})
    assert wiring.plugin_installed(tmp_path, mine) is True
    assert wiring.plugin_installed(tmp_path, theirs) is False


def test_a_local_install_does_not_count_with_no_project_to_compare(tmp_path):
    write_register(tmp_path, {"rentctl@rentctl": [{"scope": "local", "projectPath": "/somewhere"}]})
    assert wiring.plugin_installed(tmp_path) is False


def test_another_plugin_is_not_ours(tmp_path):
    write_register(tmp_path, {"something-else@rentctl": [{"scope": "user"}]})
    assert wiring.plugin_installed(tmp_path) is False


# ---------------------------------------------------------------------------
# The version has exactly one home
#
# It used to have two -- a literal in `pyproject.toml` and another in
# `src/rentctl/__init__.py` -- with nothing tying them together and no test
# comparing them. A bump that touched one and missed the other would publish a
# package whose `rent --version` disagreed with its own metadata, and whose three
# generated manifests agreed with neither. These tests fail if anyone reintroduces
# the second home.
# ---------------------------------------------------------------------------

def _pyproject():
    root = Path(__file__).resolve().parent.parent
    with (root / "pyproject.toml").open("rb") as fh:
        return root, tomllib.load(fh)


def test_pyproject_does_not_restate_the_version():
    """A literal `version` under `[project]` is the defect coming back."""
    root, cfg = _pyproject()
    assert "version" not in cfg["project"], (
        "pyproject.toml states a literal version again. The version belongs only in "
        "src/rentctl/__init__.py; pyproject derives it via [tool.hatch.version]."
    )
    assert "version" in cfg["project"]["dynamic"]


def test_hatch_reads_the_version_from_the_module_that_defines_it():
    """The build's source and the manifests' source must be the same file.

    `core/wiring.py` imports `__version__` from the source tree to stamp the
    plugin, marketplace and registry manifests. If hatchling read the version
    from anywhere else, a build and a manifest render could disagree.
    """
    root, cfg = _pyproject()
    declared = root / cfg["tool"]["hatch"]["version"]["path"]
    assert declared == root / "src" / "rentctl" / "__init__.py"
    assert declared.read_text(encoding="utf-8").count("__version__ = ") == 1


def test_the_built_metadata_version_matches_the_module():
    """Belt and braces: whatever hatchling extracts is what the code reports.

    Runs hatchling's own version hook rather than re-parsing the file with a
    second regex -- a test that reimplements the mechanism it checks agrees with
    itself, not with the build.
    """
    root, _ = _pyproject()
    core = pytest.importorskip("hatchling.metadata.core")
    manager = pytest.importorskip("hatchling.plugin.manager")
    metadata = core.ProjectMetadata(str(root), manager.PluginManager())
    assert metadata.version == rentctl.__version__
