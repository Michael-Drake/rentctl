"""Unit tests for runtime-agnostic enrollment (ADR-0011).

The capability under test exists because of a near-miss: an agent on a runtime
devctl did not know about looked for a broker, found nothing, and started a dev
server by hand. So the assertions here are mostly about the *absence* cases —
what happens for a runtime with no project markers, for a project with no
context file, for a typo'd runtime id. Those are the paths the incident ran
through, and each one previously succeeded quietly.

`runtimes.py` reports high line coverage from other modules' tests merely by
being imported — it is largely dataclass fields and module constants. That
number certifies nothing about behaviour, which is the reason this file exists.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from rentctl.core import enroll, runtimes, wiring
from rentctl.core import events as ev
from rentctl.core.errors import UNKNOWN_RUNTIME, DevctlError
from rentctl.core.service import Service

TOML = """
[project]
name = "{name}"
runner = "process"

[profiles.default]
cmd = "npm run dev"
cwd = "."
port_env = "PORT"
"""

YES = lambda plan: True  # noqa: E731


@pytest.fixture
def repo(tmp_path):
    def _make(name="sampleapp", where=None):
        root = where or (tmp_path / name)
        root.mkdir(parents=True, exist_ok=True)
        (root / "devctl.toml").write_text(TOML.format(name=name))
        return root
    return _make


def read(p):
    return json.loads(p.read_text())


# --- §2 bindings are data, and each one carries its provenance -------------

def test_every_binding_records_when_it_was_verified():
    """A binding written from memory is how you ship a fix that does not fix
    (ADR-0011 §2). The field is the only thing standing between a verified
    binding and a plausible one, so it may not be blank."""
    for binding in runtimes.BINDINGS:
        assert binding.verified.strip(), f"{binding.id} has no verified stamp"
        assert "2026" in binding.verified


def test_bindings_are_registered_under_their_own_id():
    assert set(runtimes.BY_ID) == {b.id for b in runtimes.BINDINGS}
    assert runtimes.BY_ID["claude-code"] is runtimes.CLAUDE_CODE
    assert runtimes.BY_ID["gemini-cli"] is runtimes.GEMINI_CLI


def test_gemini_shares_one_settings_file_for_mcp_and_hooks():
    """Claude Code splits these across two files and Gemini does not. If the
    binding got this wrong the second write would clobber the first, and the
    symptom would be enrollment that silently registers only half of itself."""
    g = runtimes.GEMINI_CLI
    assert g.mcp_file == g.hooks_file == (".gemini", "settings.json")
    assert runtimes.CLAUDE_CODE.mcp_file != runtimes.CLAUDE_CODE.hooks_file


# --- §5 selection is generous on purpose -----------------------------------

def test_a_runtime_installed_but_unused_is_still_enrolled(tmp_path, monkeypatch):
    """The incident case. The project had no `.gemini/` yet — that absence is
    precisely *why* the agent found no devctl — so selecting on project markers
    alone would skip the one runtime that needed enrolling."""
    monkeypatch.setattr(runtimes.shutil, "which", lambda name: f"/usr/bin/{name}")
    chosen = runtimes.select(tmp_path)
    assert runtimes.GEMINI_CLI in chosen


def test_a_runtime_used_in_the_project_is_enrolled_even_if_not_installed(tmp_path, monkeypatch):
    """The project is the stronger signal: a checkout carrying `.gemini/` will
    be opened with Gemini eventually, whatever this machine has on PATH."""
    monkeypatch.setattr(runtimes.shutil, "which", lambda name: None)
    (tmp_path / ".gemini").mkdir()
    chosen = runtimes.select(tmp_path)
    assert runtimes.GEMINI_CLI in chosen


def test_a_project_matching_nothing_still_gets_the_reference_runtime(tmp_path, monkeypatch):
    """Enrolling a project for zero runtimes would write nothing and report
    success — the same silent non-enrollment this ADR closes."""
    monkeypatch.setattr(runtimes.shutil, "which", lambda name: None)
    chosen = runtimes.select(tmp_path)
    assert chosen == [runtimes.CLAUDE_CODE]


def test_explicit_selection_overrides_detection(tmp_path, monkeypatch):
    monkeypatch.setattr(runtimes.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert runtimes.select(tmp_path, only=("gemini-cli",)) == [runtimes.GEMINI_CLI]


def test_unknown_ids_are_reported_not_dropped():
    assert runtimes.unknown_ids(("claude-code", "emacs-doctor")) == ["emacs-doctor"]


# --- why a runtime was chosen is part of the answer (WI-0026) -------------

def test_a_detected_runtime_is_labelled_as_a_guess(tmp_path, monkeypatch):
    """`detected` means devctl inferred this from an executable on the machine,
    not from anything the project says. webapp declares `gemini-antigravity`;
    devctl enrolled `gemini-cli` off the `gemini` binary and reported plain
    success, so a guess was indistinguishable from a verified fact."""
    monkeypatch.setattr(runtimes.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert runtimes.selection_reason(runtimes.GEMINI_CLI, tmp_path, None) == runtimes.DETECTED


def test_a_configured_runtime_outranks_mere_detection(tmp_path, monkeypatch):
    """The project carrying the runtime's files is evidence about the project;
    an executable on the machine is evidence about the machine."""
    monkeypatch.setattr(runtimes.shutil, "which", lambda name: f"/usr/bin/{name}")
    (tmp_path / ".gemini").mkdir()
    assert runtimes.selection_reason(runtimes.GEMINI_CLI, tmp_path, None) == runtimes.CONFIGURED


def test_an_explicitly_requested_runtime_is_not_a_guess(tmp_path, monkeypatch):
    monkeypatch.setattr(runtimes.shutil, "which", lambda name: None)
    reason = runtimes.selection_reason(runtimes.GEMINI_CLI, tmp_path, ("gemini-cli",))
    assert reason == runtimes.REQUESTED


def test_the_fallback_runtime_says_it_is_a_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(runtimes.shutil, "which", lambda name: None)
    assert runtimes.selection_reason(runtimes.CLAUDE_CODE, tmp_path, None) == runtimes.DEFAULTED


def test_enrollment_reports_why_each_runtime_was_chosen(repo, devctl_home, tmp_path, monkeypatch):
    """The label has to reach the caller, not just exist in the module — the
    defect was the *output* reading as verified, not the selection itself.

    It must also be computed before enrollment writes anything: `install_rule`
    creates GEMINI.md and `.gemini/`, the exact markers `configured_in()` reads,
    so asking mid-loop reported `configured` about a file devctl had just
    written itself. A check answering questions about its own side effects
    manufactures the evidence that makes a guess look verified."""
    monkeypatch.setattr(runtimes.shutil, "which", lambda name: f"/usr/bin/{name}")
    root = repo()
    assert not (root / ".gemini").exists()  # nothing here says "Gemini" yet
    result = enroll.enroll(root, devctl_home, approve=lambda plan: True,
                           claude_home=tmp_path / "no-plugins")
    assert result["runtimes"]["gemini-cli"]["selected_by"] == runtimes.DETECTED
    assert (root / ".gemini").exists()  # ...and enrollment did create it


# --- §3 the policy rule ----------------------------------------------------

def test_rule_creates_the_context_file_when_absent(tmp_path):
    """A project with no CLAUDE.md is exactly a project whose agent has nowhere
    to learn the policy from. Declining to create it would decline to fix the
    worst instance of the bug."""
    p = tmp_path / "CLAUDE.md"
    assert wiring.install_rule(p) is True
    assert wiring.RULE_BEGIN in p.read_text()


def test_rule_appends_without_disturbing_existing_content(tmp_path):
    p = tmp_path / "CLAUDE.md"
    p.write_text("# My project\n\nRun the tests before committing.\n")
    wiring.install_rule(p)
    text = p.read_text()
    assert "Run the tests before committing." in text
    assert text.index("My project") < text.index(wiring.RULE_BEGIN)


def test_rule_is_idempotent(tmp_path):
    p = tmp_path / "CLAUDE.md"
    wiring.install_rule(p)
    first = p.read_text()
    assert wiring.install_rule(p) is False
    assert p.read_text() == first


def test_reenrollment_replaces_the_rule_in_place_and_keeps_ordering(tmp_path, monkeypatch):
    """The delimiters exist so an updated rule does not accumulate copies, and
    so a project that moved the block keeps where it put it."""
    p = tmp_path / "CLAUDE.md"
    p.write_text(f"# Top\n\n{wiring.render_rule()}\n\n## Trailing section\n")

    monkeypatch.setattr(wiring, "RULE_BODY", "## Revised rule\n\nNew text.")
    assert wiring.install_rule(p) is True

    text = p.read_text()
    assert text.count(wiring.RULE_BEGIN) == 1
    assert "New text." in text
    assert text.index("# Top") < text.index(wiring.RULE_BEGIN)
    assert text.index(wiring.RULE_END) < text.index("## Trailing section")


def test_the_rule_forbids_the_manual_fallback(tmp_path):
    """The load-bearing sentence (ADR-0011 §3). Without it a helpful agent that
    cannot reach devctl routes around the rule in order to be useful — which is
    the incident again, with an extra step."""
    body = wiring.RULE_BODY.lower()
    assert "do not start the dev server yourself" in body
    assert "stop and say so" in body
    assert "do not start the server" in body


# --- §1 the rule goes in first, and unconditionally ------------------------

def test_enrollment_writes_the_rule_alongside_the_wiring(repo, devctl_home, tmp_path):
    root = repo()
    result = enroll.enroll(root, devctl_home, approve=YES,
                           claude_home=tmp_path / "no-plugins",
                           runtimes=("claude-code",))
    assert result["runtimes"]["claude-code"]["rule_written"] is True
    assert wiring.RULE_BEGIN in (root / "CLAUDE.md").read_text()


def test_the_plugin_supplies_hooks_but_never_the_rule(repo, devctl_home, tmp_path):
    """ADR-0011 §1. The plugin is a hooks delivery mechanism; it puts nothing in
    the project's own context file, so skipping the rule when it is installed
    would leave exactly the projects that use it stating no policy."""
    from conftest import install_plugin

    claude_home = tmp_path / "claude"
    install_plugin(claude_home)
    root = repo()
    result = enroll.enroll(root, devctl_home, approve=YES, claude_home=claude_home,
                           runtimes=("claude-code",))
    wrote = result["runtimes"]["claude-code"]
    assert result["plugin"] is True
    assert wrote["hooks_written"] is False
    assert wrote["rule_written"] is True


def test_no_hooks_still_writes_the_rule(repo, devctl_home, tmp_path):
    root = repo()
    result = enroll.enroll(root, devctl_home, approve=YES, write_hooks=False,
                           claude_home=tmp_path / "no-plugins",
                           runtimes=("claude-code",))
    wrote = result["runtimes"]["claude-code"]
    assert wrote["hooks_written"] is False
    assert wrote["rule_written"] is True


def test_enrolling_two_runtimes_writes_both_shapes(repo, devctl_home, tmp_path):
    root = repo()
    result = enroll.enroll(root, devctl_home, approve=YES,
                           claude_home=tmp_path / "no-plugins",
                           runtimes=("claude-code", "gemini-cli"))
    assert set(result["runtimes"]) == {"claude-code", "gemini-cli"}
    assert (root / "CLAUDE.md").exists()
    assert (root / "GEMINI.md").exists()

    gemini = read(root / ".gemini" / "settings.json")
    assert gemini["mcpServers"][wiring.SERVER_NAME] == wiring.mcp_fragment()
    hook = gemini["hooks"]["SessionEnd"][0]["hooks"][0]
    assert hook["name"] == f"{wiring.COMMAND}-down"
    assert "$GEMINI_PROJECT_DIR" in hook["command"]

    claude_hook = read(root / ".claude" / "settings.local.json")["hooks"]["SessionEnd"][0]["hooks"][0]
    assert "name" not in claude_hook
    assert "$CLAUDE_PROJECT_DIR" in claude_hook["command"]


def test_legacy_result_fields_report_the_reference_runtime(repo, devctl_home, tmp_path):
    """Consumers predating multi-runtime enrollment read `mcp_written` /
    `hooks_written`; they meant Claude Code's result all along."""
    root = repo()
    result = enroll.enroll(root, devctl_home, approve=YES,
                           claude_home=tmp_path / "no-plugins",
                           runtimes=("claude-code",))
    assert result["mcp_written"] == result["runtimes"]["claude-code"]["mcp_written"]
    assert result["hooks_written"] == result["runtimes"]["claude-code"]["hooks_written"]


# --- §6 an unknown runtime id refuses --------------------------------------

def test_unknown_runtime_raises_and_names_the_known_ids(repo, devctl_home):
    with pytest.raises(DevctlError) as excinfo:
        enroll.enroll(repo(), devctl_home, approve=YES, runtimes=("gemni-cli",))
    assert excinfo.value.code == UNKNOWN_RUNTIME
    assert "claude-code" in str(excinfo.value)


def test_a_typo_alongside_a_valid_id_still_refuses(repo, devctl_home):
    """Enrolling the half it understood and staying quiet about the half it did
    not is the silent partial-enrollment this is meant to prevent."""
    with pytest.raises(DevctlError):
        enroll.enroll(repo(), devctl_home, approve=YES,
                      runtimes=("claude-code", "emacs-doctor"))


def test_a_refused_runtime_writes_nothing_at_all(repo, devctl_home):
    """The check runs before any claim is drawn or any file is touched, so a
    typo cannot leave a half-enrolled project or burn a port block."""
    root = repo()
    with pytest.raises(DevctlError):
        enroll.enroll(root, devctl_home, approve=YES, runtimes=("nope",))
    assert not (root / "CLAUDE.md").exists()
    assert not (root / ".mcp.json").exists()
    assert not devctl_home.registry_file.exists()


# --- §4 the environment contract is runtime-neutral first ------------------

# Derived from the source of truth, NOT retyped. This file used to carry its own
# copy of both tuples, and the copy repeated `CLAUDE_SESSION_ID` — the name
# nothing sets. So `clean_env` dutifully cleared a variable that was never
# present, `_caller_session()` returned `unknown` exactly as the test demanded,
# and the suite certified the defect for 33 real leases (WI-0036). A test that
# restates the value it is checking cannot fail on that value being wrong.
from rentctl.core.service import PROJECT_DIR_ENVS as PROJECT_VARS
from rentctl.core.service import SESSION_ID_ENVS as SESSION_VARS

# The runtime variables this suite must keep clearing even after devctl stops
# naming them — otherwise a real one leaking in from the environment the tests
# run in (Claude Code sets CLAUDE_CODE_SESSION_ID) silently changes what a
# "nothing is set" test is actually asserting.
STRAY_VARS = ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "GEMINI_SESSION_ID")


@pytest.fixture
def clean_env(monkeypatch):
    for name in tuple(PROJECT_VARS) + tuple(SESSION_VARS) + STRAY_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_devctl_project_dir_wins_over_every_runtime_name(clean_env):
    clean_env.setenv("DEVCTL_PROJECT_DIR", "/neutral")
    clean_env.setenv("CLAUDE_PROJECT_DIR", "/claude")
    clean_env.setenv("GEMINI_PROJECT_DIR", "/gemini")
    assert Service._caller_cwd() == "/neutral"


def test_a_runtime_variable_is_used_when_the_neutral_one_is_unset(clean_env):
    clean_env.setenv("GEMINI_PROJECT_DIR", "/gemini")
    assert Service._caller_cwd() == "/gemini"


def test_project_dir_falls_back_to_the_working_directory(clean_env, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert Service._caller_cwd() == str(tmp_path)


def test_an_empty_variable_is_treated_as_unset(clean_env):
    """An exported-but-empty variable is the shape a hook produces when its own
    runtime did not populate it. Honouring it would resolve the project to the
    empty string, which is not a directory and not an error either."""
    clean_env.setenv("DEVCTL_PROJECT_DIR", "")
    clean_env.setenv("CLAUDE_PROJECT_DIR", "/claude")
    assert Service._caller_cwd() == "/claude"


def test_session_id_prefers_the_neutral_variable(clean_env):
    clean_env.setenv("DEVCTL_SESSION_ID", "neutral-1")
    clean_env.setenv("CLAUDE_CODE_SESSION_ID", "claude-1")
    assert Service._caller_session() == "neutral-1"


def test_session_id_is_unattributed_when_no_runtime_supplies_one(clean_env):
    """The fallback, with nothing set. Note this asserts what devctl COULD READ,
    not that the runtime has no session identity — the old wording claimed the
    latter and was wrong for the entire pilot."""
    assert Service._caller_session() == ev.UNATTRIBUTED


# The two tests that would have caught WI-0036. They assert the variable NAMES
# against the runtimes that set them, because deriving the fixture from
# `SESSION_ID_ENVS` protects against the copy drifting — not against the
# original being wrong. Only a claim about the outside world does that.

def test_claude_codes_real_session_variable_is_read(clean_env):
    """Observed directly in a live Claude Code session, where this exact variable
    is set and `CLAUDE_SESSION_ID` is not. Reading the wrong one is why all 33
    leases in the pilot log recorded `unknown` and G1 could not be scored."""
    assert "CLAUDE_CODE_SESSION_ID" in SESSION_VARS
    clean_env.setenv("CLAUDE_CODE_SESSION_ID", "cc-session-1")
    assert Service._caller_session() == "cc-session-1"


def test_gemini_clis_documented_session_variable_is_read(clean_env):
    """gemini-cli 0.35.2, docs/hooks/index.md: "GEMINI_SESSION_ID: The unique ID
    for the current session." Contract rather than observation — but checked,
    because the neighbouring entry in this same tuple was neither."""
    assert "GEMINI_SESSION_ID" in SESSION_VARS
    clean_env.setenv("GEMINI_SESSION_ID", "gem-session-1")
    assert Service._caller_session() == "gem-session-1"


def test_no_session_variable_is_carried_that_nothing_sets(clean_env):
    """`CLAUDE_SESSION_ID` is not a variable any runtime exports. Carrying it
    made the tuple LOOK verified and cost the pilot its session attribution;
    a plausible wrong name is worse than a missing one."""
    assert "CLAUDE_SESSION_ID" not in SESSION_VARS


# --- the hook command is spelled per runtime -------------------------------

def test_session_end_names_each_runtime_s_project_variable():
    assert "$CLAUDE_PROJECT_DIR" in wiring.session_end_command(runtimes.CLAUDE_CODE)
    assert "$GEMINI_PROJECT_DIR" in wiring.session_end_command(runtimes.GEMINI_CLI)


def test_a_runtime_with_no_project_variable_omits_cwd_entirely(tmp_path):
    """`--cwd ""` would be indistinguishable from a deliberate empty value and
    would scope teardown to nothing. Omitting the argument lets devctl resolve
    the caller directory itself."""
    bare = dataclasses.replace(runtimes.CLAUDE_CODE, project_dir_env=None)
    cmd = wiring.session_end_command(bare)
    assert "--cwd" not in cmd
    assert "--reason session-end" in cmd


def test_print_hooks_matches_what_would_be_written_for_that_runtime():
    for binding in runtimes.BINDINGS:
        text = wiring.render_hooks_text(binding)
        assert json.loads(text)["hooks"] == wiring.hooks_fragment(binding)
