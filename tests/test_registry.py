"""Unit tests for registry loading, validation (F10), and port allocation."""

from __future__ import annotations

import pytest

from rentctl.core.errors import REGISTRY_INVALID, UNKNOWN_PROFILE, UNKNOWN_PROJECT, DevctlError
from rentctl.core.registry import Registry


def load(data) -> Registry:
    return Registry.from_dict(data)


# --- the file stays readable to the human it warns ------------------------

def test_the_do_not_hand_edit_notice_survives_a_write(sample_registry_data, devctl_home):
    """The registry carries a `//` note telling a human not to hand-edit it.
    `json.dumps` defaulted to escaping, so that warning was sitting on disk as
    `ADR-0002 \\u00a73 ... regenerable view \\u2014 a lost claim`. A do-not-touch
    notice nobody can read is not a notice."""
    reg = Registry.from_dict(sample_registry_data)
    path = devctl_home.registry_file
    reg.write(path)
    raw = path.read_text(encoding="utf-8")
    assert "\\u2014" not in raw
    assert "\\u00a7" not in raw
    assert "—" in raw and "§" in raw
    Registry.load(path)  # and it still round-trips


def test_a_non_ascii_project_name_round_trips(sample_registry_data, devctl_home):
    sample_registry_data["projects"]["café"] = sample_registry_data["projects"].pop("webapp")
    reg = Registry.from_dict(sample_registry_data)
    reg.write(devctl_home.registry_file)
    assert "café" in devctl_home.registry_file.read_text(encoding="utf-8")
    assert Registry.load(devctl_home.registry_file).entry("café").block == 5180


# --- happy path -----------------------------------------------------------

def test_loads_valid_registry(sample_registry_data):
    reg = load(sample_registry_data)
    entry = reg.entry("webapp")
    assert entry.block == 5180
    assert entry.runner == "process"
    assert entry.preferred_port() == 5180              # default prefers block + 0
    assert entry.preferred_port("api-only") == 5181    # preferred_offset 1
    assert entry.profile().cmd == "npm run dev"


def test_load_from_file(write_registry, sample_registry_data, devctl_home):
    path = write_registry(sample_registry_data)
    reg = Registry.load(path)
    assert reg.entry("webapp").preferred_port() == 5180


def test_offset_is_read_as_preferred_offset(sample_registry_data):
    """Entries authored against the pre-ADR-0004 schema keep working unedited."""
    entry = load(sample_registry_data).entry("webapp")
    assert entry.profile("api-only").preferred_offset == 1


def test_preferred_offset_spelling_also_accepted(sample_registry_data):
    sample_registry_data["projects"]["webapp"]["profiles"]["api-only"] = {
        "cmd": "npm run api",
        "cwd": "/tmp/webapp",
        "port_env": "PORT",
        "preferred_offset": 4,
    }
    assert load(sample_registry_data).entry("webapp").preferred_port("api-only") == 5184


def test_port_owner_map_covers_the_whole_block(sample_registry_data):
    """Ownership is by range, so a squatter on an undeclared offset is attributable."""
    owners = load(sample_registry_data).port_owner_map()
    assert owners[5180] == "webapp"
    assert owners[5187] == "webapp"   # no profile declares +7; the block still owns it
    assert 5190 not in owners          # ...and ownership stops at the block edge


def test_block_ports_is_the_whole_block(sample_registry_data):
    entry = load(sample_registry_data).entry("webapp")
    assert list(entry.block_ports()) == list(range(5180, 5190))


# --- offsets are preferences now, not assignments (ADR-0004 §4) -----------

def test_duplicate_preferred_offsets_are_allowed(sample_registry_data):
    """Two profiles wanting the same slot is resolved by the draw, not rejected."""
    sample_registry_data["projects"]["webapp"]["profiles"]["api-only"]["offset"] = 0
    entry = load(sample_registry_data).entry("webapp")
    assert entry.preferred_port() == entry.preferred_port("api-only") == 5180


def test_default_need_not_prefer_offset_zero(sample_registry_data):
    sample_registry_data["projects"]["webapp"]["profiles"]["default"]["offset"] = 3
    assert load(sample_registry_data).entry("webapp").preferred_port() == 5183


# --- unknown project / profile -------------------------------------------

def test_unknown_project(sample_registry_data):
    reg = load(sample_registry_data)
    with pytest.raises(DevctlError) as ei:
        reg.entry("nope")
    assert ei.value.code == UNKNOWN_PROJECT


def test_unknown_profile(sample_registry_data):
    entry = load(sample_registry_data).entry("webapp")
    with pytest.raises(DevctlError) as ei:
        entry.profile("ghost")
    assert ei.value.code == UNKNOWN_PROFILE


# --- fail-closed validation (F10) ----------------------------------------

def test_missing_file_is_registry_invalid(devctl_home):
    with pytest.raises(DevctlError) as ei:
        Registry.load(devctl_home.registry_file)  # never written
    assert ei.value.code == REGISTRY_INVALID


def test_bad_json_is_registry_invalid(devctl_home):
    reg = devctl_home.registry_file
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text("{not json")
    with pytest.raises(DevctlError) as ei:
        Registry.load(reg)
    assert ei.value.code == REGISTRY_INVALID


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda d: d.pop("projects"), id="no-projects"),
        pytest.param(lambda d: d.update(projects={}), id="empty-projects"),
        pytest.param(lambda d: d["projects"]["webapp"].update(block="5180"), id="block-not-int"),
        pytest.param(lambda d: d["projects"]["webapp"].update(block=True), id="block-bool"),
        pytest.param(lambda d: d["projects"]["webapp"].update(block=80), id="block-too-low"),
        pytest.param(lambda d: d["projects"]["webapp"].update(block=70000), id="block-too-high"),
        pytest.param(lambda d: d["projects"]["webapp"].update(runner="magic"), id="bad-runner"),
        pytest.param(lambda d: d["projects"]["webapp"].update(profiles={}), id="empty-profiles"),
        pytest.param(
            lambda d: d["projects"]["webapp"]["profiles"].pop("default"), id="no-default-profile"
        ),
        pytest.param(
            lambda d: d["projects"]["webapp"]["profiles"]["default"].pop("cmd"), id="profile-no-cmd"
        ),
        pytest.param(
            lambda d: d["projects"]["webapp"]["profiles"]["default"].update(cmd=""),
            id="profile-empty-cmd",
        ),
        pytest.param(
            lambda d: d["projects"]["webapp"]["profiles"]["api-only"].update(offset=10),
            id="offset-out-of-range",
        ),
        pytest.param(
            lambda d: d["projects"]["webapp"]["profiles"]["api-only"].update(
                preferred_offset=-1
            ),
            id="preferred-offset-negative",
        ),
    ],
)
def test_schema_violations_fail_closed(sample_registry_data, mutate):
    mutate(sample_registry_data)
    with pytest.raises(DevctlError) as ei:
        load(sample_registry_data)
    assert ei.value.code == REGISTRY_INVALID


def test_cross_project_block_collision(sample_registry_data):
    # A second project overlapping webapp's block must be rejected.
    sample_registry_data["projects"]["clash"] = {
        "block": 5181,
        "runner": "process",
        "profiles": {"default": {"cmd": "x", "cwd": "/x", "port_env": "PORT"}},
    }
    with pytest.raises(DevctlError) as ei:
        load(sample_registry_data)
    assert ei.value.code == REGISTRY_INVALID


def test_block_overlap_is_rejected_even_with_no_declared_collision(sample_registry_data):
    """Blocks 5180 and 5185 declare no common port, but every port in a block is
    drawable now, so the overlap would collide in use."""
    sample_registry_data["projects"]["clash"] = {
        "block": 5185,
        "runner": "process",
        "profiles": {"default": {"cmd": "x", "cwd": "/x", "port_env": "PORT"}},
    }
    with pytest.raises(DevctlError) as ei:
        load(sample_registry_data)
    assert ei.value.code == REGISTRY_INVALID


def test_root_not_object():
    with pytest.raises(DevctlError) as ei:
        load([1, 2, 3])
    assert ei.value.code == REGISTRY_INVALID


def test_two_non_overlapping_projects_load(sample_registry_data):
    sample_registry_data["projects"]["worldcup"] = {
        "block": 5190,
        "runner": "process",
        "profiles": {"default": {"cmd": "npm run dev", "cwd": "/tmp/wc", "port_env": "PORT"}},
    }
    reg = load(sample_registry_data)
    assert reg.entry("worldcup").preferred_port() == 5190
    assert reg.port_owner_map()[5190] == "worldcup"
    assert reg.port_owner_map()[5180] == "webapp"


def test_profile_not_object_fails_closed(sample_registry_data):
    sample_registry_data["projects"]["webapp"]["profiles"]["default"] = "not-an-object"
    with pytest.raises(DevctlError) as ei:
        load(sample_registry_data)
    assert ei.value.code == REGISTRY_INVALID


def test_entry_not_object_fails_closed(sample_registry_data):
    sample_registry_data["projects"]["webapp"] = "nope"
    with pytest.raises(DevctlError) as ei:
        load(sample_registry_data)
    assert ei.value.code == REGISTRY_INVALID
