"""`devctl init` must repair, not merely report success (ADR-0012).

Two defects, both found because webapp and sampleapp could not be enrolled:

* `init` dead-ended on a missing devctl.toml with "run `devctl init`" — the
  message you get FROM running init.
* `init` repaired *absent* wiring and silently certified *stale* wiring as fine,
  because any command starting with "devctl " counted as already-installed.

The second one is the dangerous half, and the tests that matter most here are
the rename ones: every enrolled project has `devctl …` in a committed hook, and
ADR-0009 §4 drops those aliases. A matcher that knows only the current name
leaves a dead entry AND appends a live one beside it.
"""

from __future__ import annotations

import json
import tomllib

import pytest

from rentctl.core import enroll, runtimes, wiring
from rentctl.core.errors import CANNOT_ADOPT, NOT_APPROVED, DevctlError
from rentctl.core.project_config import TOML_NAME, ProjectConfig, render_toml
from rentctl.core.registry import Registry

YES = lambda plan: True  # noqa: E731
NO = lambda plan: False  # noqa: E731

WEATHER_CMD = 'npm run dev -- --port "$PORT" --strictPort'


def read(p):
    return json.loads(p.read_text())


@pytest.fixture
def bare_repo(tmp_path):
    """A project directory with NO devctl.toml — the case init could not handle."""
    root = tmp_path / "webapp-planetarium"
    (root / "frontend").mkdir(parents=True)
    return root


@pytest.fixture
def legacy_registry(devctl_home, write_registry):
    """A generator-era entry: no source_dir, absolute cwd, `offset` spelling."""
    def _make(root, name="webapp", cwd=None, cmd=WEATHER_CMD):
        write_registry({
            "enforcement": "advisory",
            "projects": {
                name: {
                    "block": 5180,
                    "runner": "process",
                    "profiles": {
                        "default": {
                            "cmd": cmd,
                            "cwd": str(cwd if cwd is not None else root / "frontend"),
                            "port_env": "PORT",
                            "offset": 0,
                        }
                    },
                }
            },
        })
    return _make


# --- the circular error ----------------------------------------------------

def test_missing_config_error_does_not_tell_you_to_run_the_command_you_ran(bare_repo):
    """The whole defect in one assertion. A stranger runs `devctl init`, has no
    devctl.toml yet, and must not be told to run `devctl init`."""
    with pytest.raises(DevctlError) as ei:
        ProjectConfig.load(bare_repo)
    message = ei.value.message
    assert "[project]" in message and "port_env" in message, "must show what to write"
    assert "run `devctl init` from the project root" not in message


# --- adoption: rebuilding from what is already there -----------------------

def test_adopt_writes_a_config_derived_from_the_registry(bare_repo, devctl_home,
                                                         legacy_registry, tmp_path):
    legacy_registry(bare_repo)
    result = enroll.enroll(bare_repo, devctl_home, approve=YES, adopt=True,
                           claude_home=tmp_path / "no-plugins")

    assert result["adopted"] is True
    assert result["block"] == 5180, "adoption keeps the existing block"

    written = tomllib.loads(ProjectConfig.path_in(bare_repo).read_text())
    assert written["project"]["name"] == "webapp"
    assert written["profiles"]["default"]["cmd"] == WEATHER_CMD
    assert written["profiles"]["default"]["cwd"] == "frontend", "must be repo-relative"


def test_adopt_re_anchors_an_absolute_cwd_to_the_repo_root(bare_repo, devctl_home,
                                                           legacy_registry, tmp_path):
    """The registry holds machine-absolute paths; devctl.toml is a tracked file
    and must not. Re-anchoring is the substance of adoption."""
    legacy_registry(bare_repo, cwd=bare_repo)
    enroll.enroll(bare_repo, devctl_home, approve=YES, adopt=True,
                  claude_home=tmp_path / "no-plugins")
    written = tomllib.loads(ProjectConfig.path_in(bare_repo).read_text())
    assert written["profiles"]["default"]["cwd"] == "."


def test_declining_an_adoption_writes_no_file(bare_repo, devctl_home, legacy_registry):
    """"Declining leaves the machine untouched" has to keep holding once init
    can author a file in the repo — otherwise a refused adoption still litters."""
    legacy_registry(bare_repo)
    with pytest.raises(DevctlError) as ei:
        enroll.enroll(bare_repo, devctl_home, approve=NO, adopt=True)
    assert ei.value.code == NOT_APPROVED
    assert not ProjectConfig.path_in(bare_repo).exists()


def test_adopt_refuses_an_entry_pointing_outside_this_repo(bare_repo, devctl_home,
                                                           legacy_registry, tmp_path):
    """sampleapp's entry pointed at a worktree lane. Adopting an entry whose cwd
    is not inside this repo would author a devctl.toml claiming a directory the
    repo does not own."""
    legacy_registry(bare_repo, cwd=tmp_path / "somewhere-else")
    with pytest.raises(DevctlError) as ei:
        enroll.enroll(bare_repo, devctl_home, approve=YES, adopt=True)
    assert ei.value.code == CANNOT_ADOPT
    assert not ProjectConfig.path_in(bare_repo).exists()


def test_adopt_with_nothing_to_adopt_says_so(bare_repo, devctl_home):
    with pytest.raises(DevctlError) as ei:
        enroll.enroll(bare_repo, devctl_home, approve=YES, adopt=True)
    assert ei.value.code == CANNOT_ADOPT
    assert "nothing to adopt" in ei.value.message


def test_ambiguous_adoption_refuses_rather_than_guessing(bare_repo, devctl_home,
                                                         write_registry):
    """Guessing would enroll this repo under a name it may not own, and the
    registry is what decides which project a port block belongs to."""
    write_registry({
        "enforcement": "advisory",
        "projects": {
            name: {
                "block": block,
                "runner": "process",
                "profiles": {"default": {
                    "cmd": "npm run dev", "cwd": str(bare_repo),
                    "port_env": "PORT", "offset": 0,
                }},
            }
            for name, block in (("webapp", 5180), ("webapp-old", 5190))
        },
    })
    with pytest.raises(DevctlError) as ei:
        enroll.enroll(bare_repo, devctl_home, approve=YES, adopt=True)
    assert ei.value.code == CANNOT_ADOPT
    assert "webapp" in ei.value.message and "--adopt" in ei.value.message


def test_naming_the_entry_resolves_the_ambiguity(bare_repo, devctl_home, write_registry,
                                                 tmp_path):
    write_registry({
        "enforcement": "advisory",
        "projects": {
            name: {
                "block": block,
                "runner": "process",
                "profiles": {"default": {
                    "cmd": "npm run dev", "cwd": str(bare_repo),
                    "port_env": "PORT", "offset": 0,
                }},
            }
            for name, block in (("webapp", 5180), ("webapp-old", 5190))
        },
    })
    result = enroll.enroll(bare_repo, devctl_home, approve=YES, adopt="webapp",
                           claude_home=tmp_path / "no-plugins")
    assert result["project"] == "webapp"
    assert result["block"] == 5180


def test_an_existing_config_is_never_overwritten_by_adopt(tmp_path, devctl_home,
                                                          legacy_registry):
    """--adopt is for a project that has no devctl.toml. If one exists it is the
    authority, and adoption must not clobber the user's own declaration."""
    root = tmp_path / "webapp-planetarium"
    (root / "frontend").mkdir(parents=True)
    mine = render_toml(ProjectConfig.from_dict({
        "project": {"name": "webapp", "runner": "process"},
        "profiles": {"default": {"cmd": "my own command", "cwd": ".",
                                 "port_env": "PORT"}},
    }))
    ProjectConfig.path_in(root).write_text(mine)
    legacy_registry(root)

    enroll.enroll(root, devctl_home, approve=YES, adopt=True,
                  claude_home=tmp_path / "no-plugins")
    assert "my own command" in ProjectConfig.path_in(root).read_text()


def test_the_rendered_config_round_trips(bare_repo, devctl_home, legacy_registry,
                                         tmp_path):
    """Rendering something load() would reject means writing a file that breaks
    the *next* command, far from the line that caused it."""
    legacy_registry(bare_repo)
    enroll.enroll(bare_repo, devctl_home, approve=YES, adopt=True,
                  claude_home=tmp_path / "no-plugins")
    reloaded = ProjectConfig.load(bare_repo)
    assert reloaded.profile().cmd == WEATHER_CMD
    assert reloaded.name == "webapp"


def test_a_command_containing_single_quotes_still_round_trips(bare_repo, devctl_home,
                                                              legacy_registry, tmp_path):
    """A TOML literal string cannot hold a single quote, so the renderer has to
    fall back to a basic string. Shell commands do contain them."""
    tricky = """sh -c 'npm run dev' --flag "$PORT" """.strip()
    legacy_registry(bare_repo, cmd=tricky)
    enroll.enroll(bare_repo, devctl_home, approve=YES, adopt=True,
                  claude_home=tmp_path / "no-plugins")
    assert ProjectConfig.load(bare_repo).profile().cmd == tricky


def test_adopt_by_name_refuses_a_name_that_does_not_exist(bare_repo, devctl_home,
                                                          legacy_registry):
    legacy_registry(bare_repo)
    with pytest.raises(DevctlError) as ei:
        enroll.enroll(bare_repo, devctl_home, approve=YES, adopt="typo")
    assert ei.value.code == CANNOT_ADOPT
    assert "webapp" in ei.value.message, "must list what IS available"


def test_a_self_enrolled_project_that_lost_its_config_can_readopt(tmp_path, devctl_home,
                                                                  write_registry):
    """The other adoption case: source_dir recorded, devctl.toml deleted. The
    entry names this exact root, so there is no ambiguity to resolve."""
    root = tmp_path / "myapp"
    root.mkdir()
    write_registry({
        "enforcement": "advisory",
        "projects": {"myapp": {
            "block": 5100, "runner": "process",
            "source_dir": str(root), "approved_unattended": False,
            "profiles": {"default": {"cmd": "npm run dev", "cwd": str(root),
                                     "port_env": "PORT", "offset": 0}},
        }},
    })
    result = enroll.enroll(root, devctl_home, approve=YES, adopt=True,
                           claude_home=tmp_path / "no-plugins")
    assert result["adopted"] is True
    assert result["block"] == 5100


def test_an_entry_owned_by_another_directory_is_not_a_candidate(tmp_path, devctl_home,
                                                                write_registry):
    """A safety boundary, not a convenience: adopting an entry whose source_dir
    is some other checkout would let this repo claim that project's name and
    port block."""
    root = tmp_path / "myapp"
    root.mkdir()
    write_registry({
        "enforcement": "advisory",
        "projects": {"other": {
            "block": 5100, "runner": "process",
            "source_dir": str(tmp_path / "somewhere-else"),
            "approved_unattended": False,
            "profiles": {"default": {"cmd": "npm run dev",
                                     "cwd": str(tmp_path / "somewhere-else"),
                                     "port_env": "PORT", "offset": 0}},
        }},
    })
    registry = Registry.load(devctl_home.registry_file)
    assert enroll.adoption_candidates(root, registry) == []

    with pytest.raises(DevctlError) as ei:
        enroll.enroll(root, devctl_home, approve=YES, adopt=True)
    assert ei.value.code == CANNOT_ADOPT


# --- hook repair: the half that certifies staleness as fine ----------------

def settings_with(command, event="SessionEnd"):
    return {"hooks": {event: [{"matcher": "*", "hooks": [
        {"type": "command", "command": command, "timeout": 30}
    ]}]}}


def test_a_stale_devctl_hook_is_repaired_not_blessed(tmp_path):
    """sampleapp's exact shape: a devctl SessionEnd hook missing
    `--reason session-end`. It survived every re-run while init reported
    success, so its teardowns recorded as `inferred` and scored nothing."""
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(settings_with('devctl down --all --cwd "$CLAUDE_PROJECT_DIR"')))

    result = wiring.install_hooks_detailed(p)

    assert "SessionEnd" in result.repaired
    command = read(p)["hooks"]["SessionEnd"][0]["hooks"][0]["command"]
    assert "--reason session-end" in command


def test_repair_replaces_in_place_and_does_not_append_a_duplicate(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(settings_with('devctl down --all --cwd "$CLAUDE_PROJECT_DIR"')))
    wiring.install_hooks_detailed(p)
    assert len(read(p)["hooks"]["SessionEnd"]) == 1


def test_a_hook_the_project_wrote_is_never_touched(tmp_path):
    """Ownership is the whole safety boundary. devctl rewrites only what it
    wrote; anything else in the user's settings file is theirs."""
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(settings_with("./scripts/my-teardown.sh")))
    wiring.install_hooks_detailed(p)

    commands = [h["command"] for g in read(p)["hooks"]["SessionEnd"] for h in g["hooks"]]
    assert "./scripts/my-teardown.sh" in commands, "the project's own hook survives"
    assert any("--reason session-end" in c for c in commands), "devctl's is added beside it"


@pytest.mark.parametrize("command", [
    "devctl-wrapper.sh --all",       # on PATH, no './' — a bare prefix test matches this
    "rentctl-legacy down",
    "devctlx down --all",
])
def test_a_lookalike_command_is_not_ours(tmp_path, command):
    """Ownership is decided by the command's **first token**, not a prefix.

    These are the cases a `startswith("devctl")` test gets wrong: each begins
    with one of our names but is a different program. Overwriting one would
    silently replace a project's own teardown with devctl's.
    """
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(settings_with(command)))
    wiring.install_hooks_detailed(p)
    commands = [h["command"] for g in read(p)["hooks"]["SessionEnd"] for h in g["hooks"]]
    assert command in commands, "someone else's command must survive untouched"
    assert any("--reason session-end" in c for c in commands), "ours is added beside it"


def test_a_relative_path_lookalike_is_not_ours(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(settings_with("./devctl-wrapper.sh --all")))
    wiring.install_hooks_detailed(p)
    commands = [h["command"] for g in read(p)["hooks"]["SessionEnd"] for h in g["hooks"]]
    assert "./devctl-wrapper.sh --all" in commands


def test_a_current_hook_is_left_alone(tmp_path):
    p = tmp_path / "settings.json"
    wiring.install_hooks_detailed(p)
    before = p.read_text()
    second = wiring.install_hooks_detailed(p)
    assert not second.changed
    assert p.read_text() == before


def test_installed_and_repaired_are_reported_separately(tmp_path):
    """One added wiring that was absent; the other overwrote a line already in
    the user's file. Folding them into one boolean is how a silent rewrite
    happens."""
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(settings_with('devctl down --all')))
    result = wiring.install_hooks_detailed(p)
    assert result.repaired == ("SessionEnd",)
    assert result.installed == ("SessionStart",)


# --- the rename, which is what makes repair load-bearing -------------------

def test_a_rent_spelled_hook_is_recognised_as_ours(tmp_path):
    """After ADR-0009 §4 drops the devctl aliases, hooks will read `rent …`.
    A matcher that knew only "devctl " would not recognise them and would append
    a second entry beside the first."""
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(settings_with("rent down --all --cwd \"$CLAUDE_PROJECT_DIR\"")))
    wiring.install_hooks_detailed(p)
    assert len(read(p)["hooks"]["SessionEnd"]) == 1


def test_every_shipped_console_name_counts_as_ours():
    """The set must track pyproject's [project.scripts]; a name that ships but
    is not listed here is a hook devctl can no longer repair."""
    assert {"devctl", "rent"} <= wiring.OWNED_COMMANDS
    assert {"devctl-watchdog", "rent-watchdog"} <= wiring.OWNED_COMMANDS


def test_repair_works_for_a_non_claude_runtime(tmp_path):
    """Gemini's hook entries carry a `name` key, so the equality check that
    decides "stale" has to compare the runtime's own shape, not Claude's."""
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"hooks": {"SessionEnd": [{"matcher": "*", "hooks": [
        {"name": "devctl-down", "type": "command",
         "command": "devctl down --all", "timeout": 30}
    ]}]}}))
    result = wiring.install_hooks_detailed(p, runtimes.GEMINI_CLI)
    assert "SessionEnd" in result.repaired
    hook = read(p)["hooks"]["SessionEnd"][0]["hooks"][0]
    assert "$GEMINI_PROJECT_DIR" in hook["command"]
    assert hook["name"] == f"{wiring.COMMAND}-down"


def test_enrollment_surfaces_what_it_repaired(tmp_path, devctl_home, legacy_registry):
    """A rewrite of the user's settings file must show up in the result, not
    just in the file (ADR-0012 §3).

    Post-ADR-0014 this is a *migration*: webapp's stale hook lives in the old
    committed location, so enrollment installs fresh into `settings.local.json`
    and removes the stale copy from `settings.json`. Both halves are reported —
    deleting a line from a file the project may have committed is precisely the
    kind of change that must not happen silently."""
    root = tmp_path / "webapp-planetarium"
    (root / "frontend").mkdir(parents=True)
    (root / ".claude").mkdir()
    legacy = root / ".claude" / "settings.json"
    legacy.write_text(
        json.dumps(settings_with('devctl down --all --cwd "$CLAUDE_PROJECT_DIR"'))
    )
    legacy_registry(root)

    result = enroll.enroll(root, devctl_home, approve=YES, adopt=True,
                           runtimes=("claude-code",),
                           claude_home=tmp_path / "no-plugins")
    wired = result["runtimes"]["claude-code"]
    assert wired["hooks_installed"] == ["SessionEnd", "SessionStart"]
    assert wired["hooks_repaired"] == []
    assert wired["hooks_legacy_removed"]["events"] == ["SessionEnd"]
    assert wired["hooks_legacy_removed"]["file"] == str(legacy)

    # The stale copy is GONE, not merely shadowed. Hook entries merge across
    # settings levels, so leaving it would fire the flag-less spelling beside the
    # correct one and race it — and a flag-less win records `inferred`, which is
    # the exact evidence loss this whole line of work exists to close.
    assert "hooks" not in json.loads(legacy.read_text())
    fresh = json.loads((root / ".claude" / "settings.local.json").read_text())
    assert "--reason session-end" in fresh["hooks"]["SessionEnd"][0]["hooks"][0]["command"]


def test_the_approval_prompt_says_a_file_will_be_written(bare_repo, devctl_home,
                                                         legacy_registry):
    """Approving an adoption authors a tracked file in the user's repo, which is
    a different kind of consent from approving a command devctl will run."""
    legacy_registry(bare_repo)
    seen = {}

    def capture(plan):
        seen["text"] = "\n".join(plan.summary_lines())
        return False

    with pytest.raises(DevctlError):
        enroll.enroll(bare_repo, devctl_home, approve=capture, adopt=True)
    assert "adopting" in seen["text"]
    assert TOML_NAME in seen["text"]
