# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Drake

"""Drawing a port from a project's block (ADR-0004).

Spec §5.1 assigned each profile a fixed port, ``block + offset``. That assumed
one running instance per project+profile per machine, which lane-based
concurrency retired: N worktrees of one repository all resolved ``default`` to
the same port. ADR-0004 amends it — the block stays the unit of allocation and
of squatter ownership, and the *specific* port becomes a draw at ``env_up``.

The draw is pure: it takes the block, what the profile would prefer, and the
set of ports already spoken for, and returns a port or reports exhaustion. The
caller supplies the taken set from two sources — live leases of the project, and
anything actually listening — and holds the per-project lock across draw-plus-
write, which is what makes two concurrent ``env_up`` calls unable to draw the
same port.

**Free-ness is derived, never stored.** There is no free-list to drift out of
sync with reality; a port is free when no live lease claims it and nothing is
listening on it. That keeps the spec's core property intact: all state on disk,
the process table as ground truth, no daemon.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Draw:
    """The outcome of a draw: a port, and whether the preference was honoured."""

    port: int
    preferred: bool


def draw_port(block: int, block_size: int, preferred_offset: int, taken: set[int]) -> Draw | None:
    """Draw the profile's preferred port if free, else the lowest free one.

    Returns ``None`` when every port in the block is taken — the caller raises
    ``BLOCK_EXHAUSTED`` with the holders, rather than borrowing from a
    neighbouring block. A loud failure beats an invisible cross-project
    collision (ADR-0004 §3).

    Lowest-free rather than random keeps the assignment stable and readable: a
    second worktree gets ``block + 1``, a third ``block + 2``, and a gap left by
    a teardown is reused before the range grows.
    """
    ports = range(block, block + block_size)
    preferred = block + preferred_offset
    if preferred in ports and preferred not in taken:
        return Draw(port=preferred, preferred=True)
    for port in ports:
        if port not in taken:
            return Draw(port=port, preferred=False)
    return None
