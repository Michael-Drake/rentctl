# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Drake

"""Where a project's dev server actually runs (ADR-0010).

The registry records **one** ``cwd`` per profile — the directory ``rent init``
was run against. A git worktree of that same repo is a *different directory
holding the same project*, and an agent working in a lane needs its server to
serve the lane's files.

Spawning at the registry's ``cwd`` regardless makes every lane serve the main
checkout. That failure is silent in the worst way: the lease, the port draw, and
the teardown are all correctly per-lane (ADR-0007), so the board looks right and
only the *bytes being served* are wrong. An agent edits its lane, starts the
server, is handed the main checkout, and concludes its change did not work — or
that it did.

**Re-rooting is refused unless git proves the caller is a worktree of the same
repository.** ``--git-common-dir`` is the probe: two worktrees of one repo share
it, two unrelated checkouts do not. Nothing else is trusted — not a path prefix,
not a name match — because the thing being decided is which directory an
approved command executes in.

The fallback is **always the registry's own cwd**, the directory the operator
approved. Every refusal therefore degrades to the previous behaviour rather than
running an approved command somewhere unverified, and every refusal carries a
reason so a caller can tell "did not need re-rooting" from "could not verify"
(the `declare-what-a-check-assumes` habit).

Why the approved-command pin is *not* re-checked against the lane
----------------------------------------------------------------
Re-rooting stays inside the repository the operator enrolled, so it does not
widen the trust boundary ADR-0003 §6 draws. The command string still comes from
the registry — a lane's own ``devctl.toml`` is never read, so a lane cannot
escalate by editing it. What a lane *can* change is the repo code the approved
command runs (its ``package.json`` dev script, say) — but so can ``git pull`` on
the main checkout, which the pin has never covered and was never meant to: the
pin covers *what devctl is asked to execute*, not the code that execution
reaches. Running a lane's branch is the same trust level as checking that branch
out on main and running it, which is the entire point of a worktree.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

_GIT_TIMEOUT_S = 5.0

# A git probe: (args, cwd) -> stdout stripped, or None when git cannot answer.
GitFn = Callable[[list[str], str], "str | None"]


@dataclass(frozen=True)
class SpawnTarget:
    """Where to spawn, whether that differs from the registry, and why.

    ``reason`` is always populated — including on the no-op paths — so a caller
    can distinguish "the registry cwd was already right" from "we could not
    prove it was safe to move." Folding those together is how a check that
    silently could not run reads as a check that passed.
    """

    cwd: str
    rerooted: bool
    reason: str


def _run_git(args: list[str], cwd: str) -> str | None:
    """Run ``git -C cwd <args>``; ``None`` on any failure at all.

    Absent git, a non-repo directory, a timeout and a non-zero exit are all the
    same answer here — *we cannot prove the caller is a sibling worktree* — and
    they all land on the registry cwd.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _toplevel(git: GitFn, cwd: str) -> str | None:
    out = git(["rev-parse", "--show-toplevel"], cwd)
    return os.path.realpath(out) if out else None


def _common_dir(git: GitFn, cwd: str) -> str | None:
    """The repository's shared git dir — identical across all its worktrees.

    ``--git-common-dir`` may answer relatively (plain ``.git`` in a normal
    checkout), and it answers relative to the command's own directory, which
    ``-C`` has already set to ``cwd``.
    """
    out = git(["rev-parse", "--git-common-dir"], cwd)
    if not out:
        return None
    return os.path.realpath(os.path.join(cwd, out))


def resolve_spawn_cwd(
    enrolled_cwd: str,
    caller_cwd: str,
    *,
    git: GitFn = _run_git,
) -> SpawnTarget:
    """Pick the directory to spawn in: the caller's worktree, or the registry's.

    ``enrolled_cwd`` is the profile's registered directory and is frequently a
    *subdirectory* of the repo (``<root>/frontend``), which is why this cannot
    simply be "use the caller's cwd" — the relative path below the repo root is
    what gets carried over to the caller's worktree.
    """
    enrolled = os.path.realpath(enrolled_cwd)
    caller = os.path.realpath(caller_cwd)

    if caller == enrolled:
        return SpawnTarget(enrolled, False, "caller is the enrolled directory")

    enrolled_root = _toplevel(git, enrolled)
    if enrolled_root is None:
        return SpawnTarget(enrolled, False, "the enrolled directory is not in a git worktree")

    caller_root = _toplevel(git, caller)
    if caller_root is None:
        return SpawnTarget(enrolled, False, "the caller is not in a git worktree")

    if caller_root == enrolled_root:
        # Same checkout, caller merely sits elsewhere inside it (repo root vs
        # frontend/). The registry cwd is already the right answer.
        return SpawnTarget(enrolled, False, "caller is in the enrolled checkout")

    enrolled_repo = _common_dir(git, enrolled)
    caller_repo = _common_dir(git, caller)
    if enrolled_repo is None or caller_repo is None:
        return SpawnTarget(enrolled, False, "could not identify the repository")
    if enrolled_repo != caller_repo:
        return SpawnTarget(enrolled, False, "caller is a worktree of a different repository")

    try:
        rel = Path(enrolled).relative_to(enrolled_root)
    except ValueError:  # pragma: no cover - defensive; toplevel always contains cwd
        return SpawnTarget(enrolled, False, "enrolled directory is outside its own worktree")

    candidate = Path(caller_root) / rel
    if not candidate.is_dir():
        # A lane that does not carry the subdirectory (branch predates it, sparse
        # checkout) must not be spawned into — the command would run somewhere
        # that is not the project.
        return SpawnTarget(
            enrolled,
            False,
            f"{rel.as_posix()!r} does not exist in the caller's worktree {caller_root}",
        )

    return SpawnTarget(
        str(candidate),
        True,
        f"re-rooted onto the caller's worktree {caller_root}",
    )
