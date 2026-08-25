"""Unit tests for ``devctl.toml`` loading and validation (ADR-0002 §3).

This file is the untrusted-input boundary: ``devctl.toml`` is authored in a repo
devctl does not own, and its ``cmd`` becomes a process. Validation is therefore
adversarial, not just schema-shaped — see the "hostile input" section.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rentctl.core.errors import PROJECT_CONFIG_INVALID, DevctlError
from rentctl.core.project_config import (  # noqa: F401
    LEGACY_TOML_NAME,
    TOML_NAME,
    ProjectConfig,
    profile_fingerprint,
)

GOOD = """
[project]
name = "sampleapp"
runner = "process"

[profiles.default]
cmd = "npm run dev"
cwd = "."
port_env = "PORT"
"""


def write(root: Path, text: str) -> Path:
    (root / TOML_NAME).write_text(text)
    return root


def load(root: Path) -> ProjectConfig:
    return ProjectConfig.load(root)


# --- the ADR-0009 filename rename, with a fallback (WI-0024) --------------

def test_the_current_name_is_rentctl_toml():
    """Asserted directly. The rename's whole point is that a stranger installing
    rentctl never creates a file named after the retired name."""
    assert TOML_NAME == "rentctl.toml"
    assert LEGACY_TOML_NAME == "devctl.toml"


def test_a_project_still_using_the_legacy_name_keeps_working(tmp_path):
    """Nobody is asked to rename a file to stay enrolled. webapp and sampleapp
    have devctl.toml committed; breaking them to tidy a name would break the
    cleanup hooks the pilot exists to prove."""
    (tmp_path / LEGACY_TOML_NAME).write_text(GOOD)
    assert ProjectConfig.load(tmp_path).name == "sampleapp"
    assert ProjectConfig.exists_in(tmp_path) is True


def test_the_current_name_wins_when_both_are_present(tmp_path):
    """A half-finished rename must not leave the old file authoritative — the
    project renamed it to mean something."""
    (tmp_path / LEGACY_TOML_NAME).write_text(GOOD)
    (tmp_path / TOML_NAME).write_text(GOOD.replace("sampleapp", "renamed"))
    assert ProjectConfig.load(tmp_path).name == "renamed"


def test_a_written_config_always_uses_the_current_name(tmp_path):
    """`new_path_in` is separate from `path_in` so adoption cannot mint fresh
    instances of the retired name just because resolution would land there."""
    (tmp_path / LEGACY_TOML_NAME).write_text(GOOD)
    assert ProjectConfig.path_in(tmp_path).name == LEGACY_TOML_NAME   # reads the real file
    assert ProjectConfig.new_path_in(tmp_path).name == TOML_NAME      # writes the new one


def test_the_write_target_is_the_current_name_on_a_bare_repo(tmp_path):
    assert ProjectConfig.path_in(tmp_path).name == TOML_NAME


# --- the fingerprint must NOT gain ensure_ascii=False ---------------------

def test_the_fingerprint_escaping_is_load_bearing_and_pinned():
    """A guard against a well-meaning sweep.

    Adding `ensure_ascii=False` to `profile_fingerprint` is the correct fix
    everywhere else in this codebase and a regression *here*: the payload is
    hashed, not written, so escaping is invisible in the output and decides the
    input. Changing it changes the digest for any project with a non-ASCII `cmd`
    or `cwd`, so the `cmd_sha256` already in the registry stops matching and
    `env_up` refuses with CMD_CHANGED on a config nobody edited.

    The literal below is what today's implementation produces. If this test
    fails, the hash basis moved and every stored pin was silently invalidated —
    that needs a migration, not a re-baselined constant."""
    digest = profile_fingerprint(
        runner="process",
        cmd='npm run dev — with an em-dash',
        cwd_abs="/tmp/café",
        port_env="PORT",
    )
    assert digest == "44be657e9550973d5b6226cdebb2fc67f0fd5371dd75481904496778a8e20358"


# --- happy path -----------------------------------------------------------

def test_loads_minimal_config(tmp_path):
    cfg = load(write(tmp_path, GOOD))
    assert cfg.name == "sampleapp"
    assert cfg.runner == "process"
    assert cfg.profile("default").cmd == "npm run dev"
    assert cfg.profile("default").port_env == "PORT"
    assert cfg.profile("default").preferred_offset == 0


def test_extra_profile_with_preferred_offset(tmp_path):
    cfg = load(write(tmp_path, GOOD + """
[profiles.api]
cmd = "npm run api"
cwd = "server"
port_env = "PORT"
preferred_offset = 3
"""))
    assert set(cfg.profiles) == {"default", "api"}
    assert cfg.profile("api").preferred_offset == 3


def test_resolve_makes_cwd_absolute_and_inside_repo(tmp_path):
    (tmp_path / "server").mkdir()
    cfg = load(write(tmp_path, GOOD + """
[profiles.api]
cmd = "npm run api"
cwd = "server"
port_env = "PORT"
preferred_offset = 1
"""))
    resolved = cfg.resolve(tmp_path)
    assert Path(resolved["default"].cwd) == tmp_path.resolve()
    assert Path(resolved["api"].cwd) == (tmp_path / "server").resolve()
    # The repo-relative form is not lost — it is what stays in the tracked file.
    assert resolved["api"].source_cwd == "server"


# --- hostile input: the reason this loader is strict ----------------------

@pytest.mark.parametrize(
    "name",
    [
        pytest.param("../escape", id="path-traversal"),
        pytest.param("a/b", id="path-separator"),
        pytest.param("..", id="dotdot"),
        pytest.param(".hidden", id="leading-dot"),
        pytest.param("", id="empty"),
        pytest.param("UPPER", id="uppercase"),
        pytest.param("has space", id="space"),
        pytest.param("weird$name", id="shell-metachar"),
    ],
)
def test_project_name_must_be_a_safe_identifier(tmp_path, name):
    """The name becomes a lease *filename*; a traversing name would write outside
    the leases dir. Rejected at the door rather than sanitised later."""
    write(tmp_path, GOOD.replace('name = "sampleapp"', f'name = "{name}"'))
    with pytest.raises(DevctlError) as ei:
        load(tmp_path)
    assert ei.value.code == PROJECT_CONFIG_INVALID


def test_cwd_may_not_escape_the_repo(tmp_path):
    """``cwd = "../.."`` would run the command outside the repo that declared it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    write(repo, GOOD.replace('cwd = "."', 'cwd = "../.."'))
    with pytest.raises(DevctlError) as ei:
        load(repo)
    assert ei.value.code == PROJECT_CONFIG_INVALID


def test_cwd_may_not_be_absolute(tmp_path):
    write(tmp_path, GOOD.replace('cwd = "."', 'cwd = "/etc"'))
    with pytest.raises(DevctlError) as ei:
        load(tmp_path)
    assert ei.value.code == PROJECT_CONFIG_INVALID


@pytest.mark.parametrize(
    "snippet, where",
    [
        pytest.param('port = 5210', "profiles.default", id="profile-port"),
        pytest.param('block = 5210', "project", id="project-block"),
        pytest.param('ports = [1, 2]', "profiles.default", id="profile-ports"),
    ],
)
def test_port_numbers_are_structurally_unwritable(tmp_path, snippet, where):
    """ADR-0002 §6: numbers are drawn, never picked, and therefore never written
    down. The promise is only real if the schema *rejects* the field rather than
    merely lacking it — an ignored key would let a repo believe it pinned a port."""
    lines = GOOD.splitlines()
    marker = "[project]" if where == "project" else "[profiles.default]"
    idx = lines.index(marker)
    lines.insert(idx + 1, snippet)
    write(tmp_path, "\n".join(lines))
    with pytest.raises(DevctlError) as ei:
        load(tmp_path)
    assert ei.value.code == PROJECT_CONFIG_INVALID
    assert "port" in ei.value.message.lower() or "block" in ei.value.message.lower()


# --- schema violations ----------------------------------------------------

def test_missing_file_is_config_invalid(tmp_path):
    with pytest.raises(DevctlError) as ei:
        load(tmp_path)
    assert ei.value.code == PROJECT_CONFIG_INVALID


def test_unparseable_toml_is_config_invalid(tmp_path):
    write(tmp_path, "[project\nname = ")
    with pytest.raises(DevctlError) as ei:
        load(tmp_path)
    assert ei.value.code == PROJECT_CONFIG_INVALID


@pytest.mark.parametrize(
    "text, id_",
    [
        (GOOD.replace('runner = "process"', 'runner = "magic"'), "bad-runner"),
        (GOOD.replace("[project]", "[nope]"), "no-project-table"),
        (GOOD.replace("[profiles.default]", "[profiles.other]"), "no-default-profile"),
        (GOOD.replace('cmd = "npm run dev"', 'cmd = ""'), "empty-cmd"),
        (GOOD.replace('cmd = "npm run dev"', "cmd = 42"), "cmd-not-string"),
        (GOOD.replace('port_env = "PORT"', 'port_env = ""'), "empty-port-env"),
        (GOOD + "\npreferred_offset = 1\n", "stray-top-level-key"),
    ],
)
def test_schema_violations_fail_closed(tmp_path, text, id_):
    write(tmp_path, text)
    with pytest.raises(DevctlError) as ei:
        load(tmp_path)
    assert ei.value.code == PROJECT_CONFIG_INVALID


def test_default_profile_offset_must_be_zero(tmp_path):
    write(tmp_path, GOOD + "preferred_offset = 2\n")
    with pytest.raises(DevctlError) as ei:
        load(tmp_path)
    assert ei.value.code == PROJECT_CONFIG_INVALID


def test_duplicate_preferred_offsets_rejected(tmp_path):
    write(tmp_path, GOOD + """
[profiles.api]
cmd = "npm run api"
cwd = "."
port_env = "PORT"
preferred_offset = 0
""")
    with pytest.raises(DevctlError) as ei:
        load(tmp_path)
    assert ei.value.code == PROJECT_CONFIG_INVALID


def test_preferred_offset_out_of_range(tmp_path):
    write(tmp_path, GOOD + """
[profiles.api]
cmd = "npm run api"
cwd = "."
port_env = "PORT"
preferred_offset = 10
""")
    with pytest.raises(DevctlError) as ei:
        load(tmp_path)
    assert ei.value.code == PROJECT_CONFIG_INVALID


# --- fingerprint (ADR-0003 §2) --------------------------------------------

def test_fingerprint_is_stable():
    a = profile_fingerprint("process", "npm run dev", "/repo", "PORT")
    b = profile_fingerprint("process", "npm run dev", "/repo", "PORT")
    assert a == b
    assert len(a) == 64


@pytest.mark.parametrize(
    "kwargs",
    [
        {"cmd": "npm run evil"},
        {"cwd": "/elsewhere"},
        {"port_env": "OTHER"},
        {"runner": "compose"},
    ],
)
def test_fingerprint_changes_when_execution_changes(kwargs):
    base = dict(runner="process", cmd="npm run dev", cwd_abs="/repo", port_env="PORT")
    renamed = {("cwd_abs" if k == "cwd" else k): v for k, v in kwargs.items()}
    assert profile_fingerprint(**base) != profile_fingerprint(**{**base, **renamed})
