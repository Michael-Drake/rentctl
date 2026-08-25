"""Spawn-directory re-rooting (ADR-0010).

Two layers, deliberately:

* A **fake-git** layer that drives every branch of the decision, including the
  ones a real repo cannot easily be forced into (git absent, a timeout).
* A **real-git** layer that builds an actual repo and an actual worktree. The
  fake proves the logic; only the real one proves the ``git`` invocations —
  ``--git-common-dir``'s relative answer is exactly the kind of thing a fake
  agrees with and a real repo does not.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from rentctl.core.worktree import resolve_spawn_cwd

# --- fake-git layer -------------------------------------------------------


def fake_git(answers: dict[tuple[str, str], str | None]):
    """A git stand-in keyed by (subcommand-flag, cwd) → stdout, else None."""

    def _git(args: list[str], cwd: str) -> str | None:
        return answers.get((args[-1], cwd))

    return _git


def test_caller_is_the_enrolled_directory_needs_no_rerooting(tmp_path: Path):
    d = tmp_path / "app"
    d.mkdir()
    got = resolve_spawn_cwd(str(d), str(d), git=fake_git({}))
    assert got.cwd == str(d.resolve())
    assert got.rerooted is False
    assert "enrolled directory" in got.reason


def test_enrolled_dir_not_in_a_repo_falls_back(tmp_path: Path):
    enrolled, caller = tmp_path / "a", tmp_path / "b"
    enrolled.mkdir()
    caller.mkdir()
    got = resolve_spawn_cwd(str(enrolled), str(caller), git=fake_git({}))
    assert got.cwd == str(enrolled.resolve())
    assert got.rerooted is False
    assert "enrolled directory is not in a git worktree" in got.reason


def test_caller_not_in_a_repo_falls_back(tmp_path: Path):
    enrolled, caller = tmp_path / "a", tmp_path / "b"
    enrolled.mkdir()
    caller.mkdir()
    git = fake_git({("--show-toplevel", str(enrolled.resolve())): str(enrolled.resolve())})
    got = resolve_spawn_cwd(str(enrolled), str(caller), git=git)
    assert got.rerooted is False
    assert "caller is not in a git worktree" in got.reason


def test_same_checkout_keeps_the_registry_cwd(tmp_path: Path):
    """Caller at the repo root, profile in frontend/ — same checkout, no move."""
    root = tmp_path / "repo"
    front = root / "frontend"
    front.mkdir(parents=True)
    git = fake_git(
        {
            ("--show-toplevel", str(front.resolve())): str(root.resolve()),
            ("--show-toplevel", str(root.resolve())): str(root.resolve()),
        }
    )
    got = resolve_spawn_cwd(str(front), str(root), git=git)
    assert got.cwd == str(front.resolve())
    assert got.rerooted is False
    assert "enrolled checkout" in got.reason


def test_different_repository_is_refused(tmp_path: Path):
    """The safety property: an unrelated checkout never captures the spawn."""
    a_root, b_root = tmp_path / "a", tmp_path / "b"
    a_root.mkdir()
    b_root.mkdir()
    git = fake_git(
        {
            ("--show-toplevel", str(a_root.resolve())): str(a_root.resolve()),
            ("--show-toplevel", str(b_root.resolve())): str(b_root.resolve()),
            ("--git-common-dir", str(a_root.resolve())): str(a_root.resolve() / ".git"),
            ("--git-common-dir", str(b_root.resolve())): str(b_root.resolve() / ".git"),
        }
    )
    got = resolve_spawn_cwd(str(a_root), str(b_root), git=git)
    assert got.cwd == str(a_root.resolve())
    assert got.rerooted is False
    assert "different repository" in got.reason


def test_unidentifiable_repository_falls_back(tmp_path: Path):
    """git answers toplevel but not common-dir — cannot prove sibling, so refuse."""
    a_root, b_root = tmp_path / "a", tmp_path / "b"
    a_root.mkdir()
    b_root.mkdir()
    git = fake_git(
        {
            ("--show-toplevel", str(a_root.resolve())): str(a_root.resolve()),
            ("--show-toplevel", str(b_root.resolve())): str(b_root.resolve()),
        }
    )
    got = resolve_spawn_cwd(str(a_root), str(b_root), git=git)
    assert got.rerooted is False
    assert "could not identify the repository" in got.reason


def test_sibling_worktree_reroots_and_carries_the_subdirectory(tmp_path: Path):
    """The bug this exists for: <main>/frontend → <lane>/frontend."""
    main, lane = tmp_path / "main", tmp_path / "lane"
    (main / "frontend").mkdir(parents=True)
    (lane / "frontend").mkdir(parents=True)
    shared = str((main / ".git").resolve())
    git = fake_git(
        {
            ("--show-toplevel", str((main / "frontend").resolve())): str(main.resolve()),
            ("--show-toplevel", str(lane.resolve())): str(lane.resolve()),
            ("--git-common-dir", str((main / "frontend").resolve())): shared,
            ("--git-common-dir", str(lane.resolve())): shared,
        }
    )
    got = resolve_spawn_cwd(str(main / "frontend"), str(lane), git=git)
    assert got.cwd == str((lane / "frontend").resolve())
    assert got.rerooted is True
    assert "re-rooted" in got.reason


def test_missing_subdirectory_in_the_lane_falls_back(tmp_path: Path):
    """A lane whose branch predates frontend/ must not be spawned into."""
    main, lane = tmp_path / "main", tmp_path / "lane"
    (main / "frontend").mkdir(parents=True)
    lane.mkdir()
    shared = str((main / ".git").resolve())
    git = fake_git(
        {
            ("--show-toplevel", str((main / "frontend").resolve())): str(main.resolve()),
            ("--show-toplevel", str(lane.resolve())): str(lane.resolve()),
            ("--git-common-dir", str((main / "frontend").resolve())): shared,
            ("--git-common-dir", str(lane.resolve())): shared,
        }
    )
    got = resolve_spawn_cwd(str(main / "frontend"), str(lane), git=git)
    assert got.cwd == str((main / "frontend").resolve())
    assert got.rerooted is False
    assert "'frontend' does not exist" in got.reason


def test_profile_at_the_repo_root_reroots_to_the_lane_root(tmp_path: Path):
    """rel == '.' — the common single-package case."""
    main, lane = tmp_path / "main", tmp_path / "lane"
    main.mkdir()
    lane.mkdir()
    shared = str((main / ".git").resolve())
    git = fake_git(
        {
            ("--show-toplevel", str(main.resolve())): str(main.resolve()),
            ("--show-toplevel", str(lane.resolve())): str(lane.resolve()),
            ("--git-common-dir", str(main.resolve())): shared,
            ("--git-common-dir", str(lane.resolve())): shared,
        }
    )
    got = resolve_spawn_cwd(str(main), str(lane), git=git)
    assert got.cwd == str(lane.resolve())
    assert got.rerooted is True


def test_git_answering_nothing_falls_back_to_the_approved_directory(tmp_path: Path):
    """Fail toward the operator-approved directory, never toward an unverified one."""
    enrolled, caller = tmp_path / "a", tmp_path / "b"
    enrolled.mkdir()
    caller.mkdir()
    got = resolve_spawn_cwd(str(enrolled), str(caller), git=lambda a, c: None)
    assert got.cwd == str(enrolled.resolve())
    assert got.rerooted is False


def test_git_probe_returns_none_on_a_non_repo(tmp_path: Path):
    """The default runner answers None rather than raising outside a repo."""
    from rentctl.core.worktree import _run_git

    assert _run_git(["rev-parse", "--show-toplevel"], str(tmp_path)) is None


@pytest.mark.parametrize("boom", [OSError("git not installed"), subprocess.TimeoutExpired("git", 5)])
def test_git_probe_swallows_a_missing_or_hanging_binary(tmp_path: Path, monkeypatch, boom):
    """No git, or a git that hangs, must not propagate out of a start.

    This is the Linux-container case devctl will meet the moment it is
    published: the probe has to degrade, not raise, or `env_up` dies on a
    machine where the *fallback* would have worked perfectly well.
    """
    from rentctl.core import worktree as wt

    def explode(*a, **kw):
        raise boom

    monkeypatch.setattr(wt.subprocess, "run", explode)
    assert wt._run_git(["rev-parse", "--show-toplevel"], str(tmp_path)) is None

    enrolled, caller = tmp_path / "a", tmp_path / "b"
    enrolled.mkdir()
    caller.mkdir()
    got = wt.resolve_spawn_cwd(str(enrolled), str(caller))
    assert got.cwd == str(enrolled.resolve())
    assert got.rerooted is False


# --- real-git layer -------------------------------------------------------

pytestmark_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        env={"HOME": str(cwd), "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"},
    )


@pytestmark_git
def test_real_worktree_end_to_end(tmp_path: Path):
    """An actual `git worktree add` — the probe the fake cannot stand in for.

    ``--git-common-dir`` answers *relatively* in a normal checkout and
    absolutely in a linked worktree; a fake that returns absolutes from both
    would agree with a broken implementation.
    """
    main = tmp_path / "main"
    (main / "frontend").mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=main)
    _git("config", "user.email", "t@example.com", cwd=main)
    _git("config", "user.name", "t", cwd=main)
    (main / "frontend" / "package.json").write_text("{}\n")
    _git("add", "-A", cwd=main)
    _git("commit", "-qm", "init", cwd=main)

    lane = tmp_path / "lane"
    _git("worktree", "add", "-q", str(lane), "-b", "lane", cwd=main)
    assert (lane / "frontend").is_dir()

    got = resolve_spawn_cwd(str(main / "frontend"), str(lane))
    assert got.rerooted is True
    assert got.cwd == str((lane / "frontend").resolve())


@pytestmark_git
def test_real_unrelated_repos_are_refused(tmp_path: Path):
    """Two genuine repos, side by side — the spawn must not cross between them."""
    a, b = tmp_path / "a", tmp_path / "b"
    for repo in (a, b):
        repo.mkdir()
        _git("init", "-q", "-b", "main", cwd=repo)
        _git("config", "user.email", "t@example.com", cwd=repo)
        _git("config", "user.name", "t", cwd=repo)
        (repo / "f.txt").write_text("x\n")
        _git("add", "-A", cwd=repo)
        _git("commit", "-qm", "init", cwd=repo)

    got = resolve_spawn_cwd(str(a), str(b))
    assert got.rerooted is False
    assert got.cwd == str(a.resolve())
    assert "different repository" in got.reason
