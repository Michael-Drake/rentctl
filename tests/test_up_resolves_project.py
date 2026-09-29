"""``env_up`` with no project named: the checkout declares it (1.2.0).

An agent asked to "run this app" knows where it is standing, not the registry
key. Resolution only reads the name; enrollment and the approved-command pin
are checked exactly as for a named project.
"""

from __future__ import annotations

from test_service import CDT, Clock, FakeSupervision, kinds, make_service

import pytest
from datetime import datetime

from rentctl.core import procutil
from rentctl.core import service as service_mod


@pytest.fixture
def service(devctl_home, write_registry, sample_registry_data, monkeypatch):
    write_registry(sample_registry_data)
    monkeypatch.setattr(procutil, "port_owner", lambda port: None)
    monkeypatch.setattr(service_mod, "_port_answering", lambda port: False)
    clock = Clock(datetime(2026, 7, 14, 8, 0, tzinfo=CDT))
    return make_service(devctl_home, FakeSupervision(devctl_home, clock), clock)


def _declare(root, name="webapp"):
    root.mkdir(parents=True, exist_ok=True)
    (root / "rentctl.toml").write_text(
        f'[project]\nname = "{name}"\nrunner = "process"\n\n'
        '[profiles.default]\ncmd = "npm run dev"\ncwd = "."\nport_env = "PORT"\n'
    )
    return root


def test_resolves_the_nearest_rentctl_toml_above_the_cwd(service, tmp_path):
    root = _declare(tmp_path / "repo")
    sub = root / "src" / "deep"
    sub.mkdir(parents=True)
    out = service.env_up(cwd=str(sub))
    assert out["ok"] is True, out
    assert out["project"] == "webapp"


def test_outside_any_checkout_is_not_a_project_and_logs_nothing(service, tmp_path):
    bare = tmp_path / "nothing-here"
    bare.mkdir()
    out = service.env_up(cwd=str(bare))
    assert out["ok"] is False
    assert out["error"] == "NOT_A_PROJECT"
    assert "rent init" in out["message"]
    assert "up_failed" not in kinds(service)


def test_a_declared_but_unenrolled_project_says_so(service, tmp_path):
    root = _declare(tmp_path / "other", name="not-enrolled")
    out = service.env_up(cwd=str(root))
    assert out["ok"] is False
    assert out["error"] == "UNKNOWN_PROJECT"
    assert out["enrolled"] is False
    assert out["resolved_from"].endswith("rentctl.toml")
    assert "not enrolled on this machine" in out["message"]


def test_a_named_unknown_project_keeps_the_plain_message(service):
    out = service.env_up("nope")
    assert out["error"] == "UNKNOWN_PROJECT"
    assert "enrolled" not in out


def test_an_invalid_declaration_is_reported_not_guessed(service, tmp_path):
    root = tmp_path / "broken"
    root.mkdir()
    (root / "rentctl.toml").write_text("[project]\n")
    out = service.env_up(cwd=str(root))
    assert out["ok"] is False
    assert out["error"] == "PROJECT_CONFIG_INVALID"
