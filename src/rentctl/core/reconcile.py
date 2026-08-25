# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Drake

"""The pure reconciler — lease claims vs. ground truth (spec §3.1, §10).

Given a set of parsed leases, a liveness oracle, and a clock, decide what to do
with each lease. No I/O, no process signals, no psutil — just the decision. The
caller (``service.py``, ``watchdog.py``) executes the plan: removes lease files,
calls ``runner.stop()``. Keeping the decision pure is what makes it exhaustively
unit-testable against a fake process table (spec §10 "reconcile decisions").

Three outcomes per lease:

* ``KEEP``   — same process alive and lease not past its deadline.
* ``EXPIRE`` — same process alive but ``now >= expires`` → stop it, remove lease.
* ``CLEAN``  — process gone or PID recycled → just remove the lease; nothing to kill.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .leases import Lease

# A liveness oracle: does this lease's handle still point at the same live
# process/container? Impurity (psutil) is the caller's; here it is just a function.
AliveFn = Callable[[Lease], bool]


class Action(str, Enum):
    KEEP = "keep"
    EXPIRE = "expire"
    CLEAN = "clean"


@dataclass(frozen=True)
class Verdict:
    lease: Lease
    action: Action
    reason: str

    @property
    def project(self) -> str:
        return self.lease.project


def decide(lease: Lease, is_alive: AliveFn, now: datetime) -> Verdict:
    """The single-lease decision. Liveness first, then expiry.

    Liveness is checked before expiry on purpose: a dead process needs no kill
    regardless of its deadline, so ``CLEAN`` (remove only) is safe and cheaper
    than ``EXPIRE`` (stop then remove).
    """
    if not is_alive(lease):
        return Verdict(lease, Action.CLEAN, "process gone or PID recycled")
    if lease.is_expired(now):
        return Verdict(lease, Action.EXPIRE, f"lease expired at {lease.expires.isoformat()}")
    return Verdict(lease, Action.KEEP, "alive and within lease")


def plan_reconcile(leases: Iterable[Lease], is_alive: AliveFn, now: datetime) -> list[Verdict]:
    """Decide for every lease. The full sweep/ls/watchdog plan (spec §4.3/§4.4/§6.2)."""
    return [decide(lease, is_alive, now) for lease in leases]
