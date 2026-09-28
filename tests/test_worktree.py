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

from rentctl.core.containment import CwdEscapesRoot, resolve_within
from rentctl.core.errors import CWD_ESCAPES_ROOT, DevctlError
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


# --- containment (WI-0068) ------------------------------------------------
#
# `git worktree` proves the caller's checkout belongs to the same repository.
# It proves nothing about where ``<lane>/frontend`` *points*: a lane is free to
# replace that directory with a symlink, and ``is_dir()`` follows it. These
# build the shape that was reproduced externally — a real enrolled checkout, a
# sibling worktree whose ``frontend`` is a symlink — and pin the outcome.


def _repo_and_lane(tmp_path: Path) -> tuple[Path, Path]:
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
    return main, lane


def _swap_for_symlink(lane: Path, target: Path) -> None:
    shutil.rmtree(lane / "frontend")
    (lane / "frontend").symlink_to(target, target_is_directory=True)


@pytestmark_git
def test_a_symlink_that_stays_inside_the_lane_still_reroots(tmp_path: Path):
    """Symlinks are not the problem; leaving the worktree is. An internal one is fine,
    and the spawn is handed the resolved directory, not the link."""
    main, lane = _repo_and_lane(tmp_path)
    (lane / "web").mkdir()
    _swap_for_symlink(lane, Path("web"))  # relative link, resolved against lane/

    got = resolve_spawn_cwd(str(main / "frontend"), str(lane))
    assert got.rerooted is True
    assert got.cwd == str((lane / "web").resolve())


@pytestmark_git
def test_a_symlink_escaping_the_lane_is_refused_loudly(tmp_path: Path):
    """The reproduced bug: rerooted=True onto a directory outside the repository."""
    main, lane = _repo_and_lane(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    _swap_for_symlink(lane, outside)

    with pytest.raises(DevctlError) as ei:
        resolve_spawn_cwd(str(main / "frontend"), str(lane))
    assert ei.value.code == CWD_ESCAPES_ROOT
    # Both ends named, so the operator can see what the lane did.
    assert str(outside.resolve()) in ei.value.message
    assert str(lane.resolve()) in ei.value.message


@pytestmark_git
def test_a_dangling_escaping_symlink_is_refused_not_treated_as_missing(tmp_path: Path):
    """Containment is checked before existence: a link to a not-yet-existing
    outside path is an escape, not an absent subdirectory to fall back from."""
    main, lane = _repo_and_lane(tmp_path)
    _swap_for_symlink(lane, tmp_path / "nowhere")

    with pytest.raises(DevctlError) as ei:
        resolve_spawn_cwd(str(main / "frontend"), str(lane))
    assert ei.value.code == CWD_ESCAPES_ROOT


@pytestmark_git
def test_a_real_lane_missing_the_subdirectory_falls_back(tmp_path: Path):
    """Missing is not escaping: the documented fallback to the approved cwd stands."""
    main, lane = _repo_and_lane(tmp_path)
    shutil.rmtree(lane / "frontend")

    got = resolve_spawn_cwd(str(main / "frontend"), str(lane))
    assert got.rerooted is False
    assert got.cwd == str((main / "frontend").resolve())
    assert "does not exist" in got.reason


# --- the shared validator -------------------------------------------------


def test_resolve_within_accepts_the_root_itself_and_its_children(tmp_path: Path):
    (tmp_path / "a").mkdir()
    assert resolve_within(tmp_path, ".") == tmp_path.resolve()
    assert resolve_within(tmp_path, "a") == (tmp_path / "a").resolve()


def test_resolve_within_rejects_dotdot_and_absolute_escapes(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    for rel in ("..", "../x", "/etc"):
        with pytest.raises(CwdEscapesRoot):
            resolve_within(root, rel)


def test_resolve_within_rejects_a_sibling_sharing_the_root_as_a_string_prefix(tmp_path: Path):
    """``/x/lane2`` starts with ``/x/lane`` as a string; it is not inside it."""
    root, sibling = tmp_path / "lane", tmp_path / "lane2"
    root.mkdir()
    sibling.mkdir()
    (root / "f").symlink_to(sibling, target_is_directory=True)
    with pytest.raises(CwdEscapesRoot):
        resolve_within(root, "f")


def test_resolve_within_resolves_the_root_too(tmp_path: Path):
    """A root reached through a symlink must not make its own contents look foreign."""
    real = tmp_path / "real"
    (real / "sub").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    assert resolve_within(alias, "sub") == (real / "sub").resolve()


def test_resolve_within_never_lets_a_symlink_loop_escape_raw(tmp_path: Path):
    """A loop is either refused or lands inside the root; it never propagates.

    Python 3.12 raises RuntimeError on a loop in non-strict ``resolve()``; 3.13+
    returns the path unresolved. Both answers are acceptable, a traceback is not.
    """
    (tmp_path / "loop").symlink_to(tmp_path / "loop")
    try:
        got = resolve_within(tmp_path, "loop/x")
    except CwdEscapesRoot:
        return
    assert got.is_relative_to(tmp_path.resolve())


@pytest.mark.parametrize("boom", [RuntimeError("Symlink loop"), OSError(62, "ELOOP")])
def test_resolve_within_refuses_what_it_cannot_resolve(tmp_path: Path, monkeypatch, boom):
    """Forced, so the refusal branch is exercised on every Python, not only 3.12."""
    real_resolve = Path.resolve

    def resolve(self, strict=False):
        if self.name == "x":
            raise boom
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(CwdEscapesRoot) as ei:
        resolve_within(tmp_path, "x")
    assert ei.value.target is None
    assert ei.value.code == CWD_ESCAPES_ROOT
