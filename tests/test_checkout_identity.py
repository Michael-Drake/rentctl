"""A lease is keyed on the checkout, not the directory a session was launched in.

Found by the 1.2.0 live acceptance run (D1): a Claude Code session launched in
``app/sub/dir`` and one launched in ``app`` drew two ports and two servers for
the same tree, bypassing ADR-0017's sharing. The git worktree top level is the
checkout; separate worktrees stay separate (ADR-0007).
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime

import pytest
from test_service import CDT, Clock, FakeSupervision, make_service

from rentctl.core import procutil
from rentctl.core import service as service_mod
from rentctl.core.worktree import checkout_root

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "app"
    (root / "sub" / "dir").mkdir(parents=True)
    _git("init", "-q", cwd=root)
    _git("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q",
         "--allow-empty", "-m", "init", cwd=root)
    return root


def _svc(devctl_home, write_registry, sample_registry_data, monkeypatch, sid):
    write_registry(sample_registry_data)
    monkeypatch.setattr(procutil, "port_owner", lambda port: None)
    monkeypatch.setattr(service_mod, "_port_answering", lambda port: False)
    clock = Clock(datetime(2026, 7, 14, 8, 0, tzinfo=CDT))
    world = FakeSupervision(devctl_home, clock)
    return world, clock, (lambda s: make_service(devctl_home, world, clock, session_id_fn=lambda: s))


def test_checkout_root_is_the_worktree_top_level(repo, tmp_path):
    assert checkout_root(str(repo / "sub" / "dir")) == str(repo.resolve())
    wt = tmp_path / "app-wt2"
    _git("worktree", "add", "-q", str(wt), cwd=repo)
    assert checkout_root(str(wt)) == str(wt.resolve())


def test_outside_git_the_directory_is_its_own_key(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    assert checkout_root(str(d)) == str(d.resolve())


def test_root_and_subdirectory_sessions_share_one_environment(
    repo, devctl_home, write_registry, sample_registry_data, monkeypatch
):
    _, _, svc = _svc(devctl_home, write_registry, sample_registry_data, monkeypatch, None)
    a = svc("A").env_up("webapp", cwd=str(repo))
    b = svc("B").env_up("webapp", cwd=str(repo / "sub" / "dir"))
    assert a["ok"] and b["ok"], (a, b)
    assert b["port"] == a["port"]
    assert b["already_running"] is True
    assert {c["session"] for c in b["claims"]} == {"A", "B"}

    # B's session ends from its launch subdirectory: only B's claim goes.
    out = svc("B").env_down(cwd=str(repo / "sub" / "dir"), reason="session-end", wait_s=0)
    row = out["downed"][0]
    assert row["stopped"] is False and row["held_by"] == ["A"], out


def test_separate_worktrees_keep_separate_environments(
    repo, tmp_path, devctl_home, write_registry, sample_registry_data, monkeypatch
):
    wt = tmp_path / "app-wt2"
    _git("worktree", "add", "-q", str(wt), cwd=repo)
    _, _, svc = _svc(devctl_home, write_registry, sample_registry_data, monkeypatch, None)
    a = svc("A").env_up("webapp", cwd=str(repo))
    c = svc("C").env_up("webapp", cwd=str(wt))
    assert a["port"] != c["port"]
    assert c["already_running"] is False
