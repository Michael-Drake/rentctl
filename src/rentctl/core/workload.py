# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Drake

"""One stop algorithm for a workload session (ADR-0016 §4).

Explicit ``down``, session end, expiry and a failed start all stop a workload
the same way, so there is one implementation: :func:`stop_workload`. It
**returns** its outcome. The 1.0.x runner's ``stop()`` returned nothing and
returned as soon as the leader shell was gone, which is how a SIGTERM-ignoring
child of a dead shell survived its teardown while the lease was deleted (S1).

Two modes:

* ``recovery`` — the caller is *not* the session's owner (today's runner, and
  later a CLI recovering after a supervisor died). Every member gets its own
  verified signal. There is no ``killpg``: from outside the session, the group
  number is a remembered id whose owner may be gone, and signalling a remembered
  group is exactly what the design forbids.
* ``owner`` — the caller *is* session S (the supervisor, plan step 4). Then
  ``killpg(S, SIGTERM)`` is safe: S is the caller's own live group and cannot be
  a recycled number at the moment of the call.

SIGKILL is never sent to a group in either mode, and the owner is never on the
kill list, so whoever runs this is still alive to verify the result.
"""

from __future__ import annotations

import os
import signal
import time
from collections.abc import Callable

from . import procutil
from .models import CLEANUP_INCOMPLETE, CLEANUP_VERIFIED, StopOutcome, WorkloadIdentity
from .procutil import Membership, ProcessTable, ProcRow, SessionScan, SignalResult

TERM_GRACE_S = 10.0   # SIGTERM → wait (spec §6.1; unchanged defaults, ADR-0016 §14)
KILL_GRACE_S = 2.0    # per-pid SIGKILL rounds
RESCAN_S = 0.25       # graceful phase: re-scan cadence; new members get their own TERM
KILL_ROUND_S = 0.05   # escalation: one round cleared every survivor in E3 (51–61 ms)

RECOVERY = "recovery"
OWNER = "owner"


def stop_workload(
    identity: WorkloadIdentity,
    mode: str = RECOVERY,
    *,
    term_grace_s: float = TERM_GRACE_S,
    kill_grace_s: float = KILL_GRACE_S,
    rescan_s: float = RESCAN_S,
    table: ProcessTable | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    reap: Callable[[], None] | None = None,
) -> StopOutcome:
    """Stop every verified member of ``identity``'s session; return what happened.

    1. **Graceful.** Owner mode: ``killpg(S, SIGTERM)``, then a verified
       per-pid TERM to members outside group S (setpgid escapers). Recovery
       mode: a verified per-pid TERM to every member.
    2. **Wait.** Re-scan every ``rescan_s``. A member not seen before — forked
       during shutdown, after the TERM — gets its own verified TERM. Ends when
       the session is empty or ``term_grace_s`` has passed.
    3. **Escalate.** Until empty or ``kill_grace_s``: scan, verified per-pid
       SIGKILL to each member. A fork mid-escalation dies next round.
    4. **Verify.** One more scan. Empty → ``verified``; otherwise
       ``incomplete`` with the survivors.

    An ``AMBIGUOUS`` scan at any point stops all signalling and returns
    ``incomplete`` with ``identity_ambiguous``: the session number can no
    longer be proved ours (ADR-0016 §2). ``reap`` lets an owner collect its own
    exited child each pass; zombies are never members, so it is housekeeping,
    not correctness.
    """
    if mode not in (RECOVERY, OWNER):
        raise ValueError(f"unknown stop mode {mode!r}")
    sid = identity.sid

    def scan() -> SessionScan:
        if reap is not None:
            reap()
        return procutil.session_scan(
            sid, identity.owner_start, identity.exclude, table=table, tree_root=identity.tree_root
        )

    state = _Stop(sid, table, identity.tree_root)
    current = scan()
    if current.state is not Membership.MEMBERS:
        return state.outcome(current)

    # --- 1. graceful ---------------------------------------------------------
    if mode == OWNER:
        # Owner mode is only meaningful from inside the session. A caller that
        # is not S's group leader must not killpg(S): from outside, S is just a
        # remembered number.
        if os.getpgid(0) != sid:
            raise ValueError(f"owner-mode stop called from outside session {sid}")
        os.killpg(sid, signal.SIGTERM)
        state.signalled = True
        for m in current.members:
            state.seen.add(_key(m))
            if m.pgid != sid:
                state.send(m, signal.SIGTERM)
    else:
        for m in current.members:
            state.seen.add(_key(m))
            state.send(m, signal.SIGTERM)

    # --- 2. wait -------------------------------------------------------------
    deadline = clock() + term_grace_s
    while True:
        sleep(min(rescan_s, max(0.0, deadline - clock())))
        current = scan()
        if current.state is not Membership.MEMBERS:
            return state.outcome(current)
        if clock() >= deadline:
            break
        for m in current.members:
            if _key(m) not in state.seen:
                state.seen.add(_key(m))
                state.send(m, signal.SIGTERM)

    # --- 3. escalate ---------------------------------------------------------
    kill_deadline = clock() + kill_grace_s
    while current.state is Membership.MEMBERS and clock() < kill_deadline:
        state.escalated = True
        for m in current.members:
            state.send(m, signal.SIGKILL)
        sleep(KILL_ROUND_S)
        current = scan()

    # --- 4. verify -----------------------------------------------------------
    if current.state is Membership.MEMBERS:
        current = scan()
    return state.outcome(current)


def _key(row: ProcRow) -> tuple[int, float | None]:
    # (pid, start) rather than pid: a pid that exits and is reused inside the
    # session during shutdown is a new member, and it gets its own TERM.
    return (row.pid, row.start_time)


class _Stop:
    """Running state of one stop: what was seen, sent, and escalated."""

    def __init__(self, sid: int, table: ProcessTable | None, tree_root: int | None = None) -> None:
        self.sid = sid
        self.table = table
        self.tree_root = tree_root
        self.seen: set[tuple[int, float | None]] = set()
        self.signalled = False
        self.escalated = False

    def send(self, member: ProcRow, sig: int) -> SignalResult:
        result = procutil.verified_signal(
            member.pid, member.start_time, self.sid, sig, table=self.table, tree_root=self.tree_root
        )
        if result is SignalResult.SIGNALLED:
            self.signalled = True
        return result

    def outcome(self, scan: SessionScan) -> StopOutcome:
        if scan.state is Membership.EMPTY:
            return StopOutcome(
                cleanup=CLEANUP_VERIFIED, escalated=self.escalated, signalled=self.signalled
            )
        if scan.state is Membership.AMBIGUOUS:
            return StopOutcome(
                cleanup=CLEANUP_INCOMPLETE,
                escalated=self.escalated,
                signalled=self.signalled,
                identity_ambiguous=True,
                detail=f"not signalled: {scan.reason}",
            )
        return StopOutcome(
            cleanup=CLEANUP_INCOMPLETE,
            survivors=tuple(m.survivor() for m in scan.members),
            escalated=self.escalated,
            signalled=self.signalled,
            detail=(
                f"{len(scan.members)} process(es) in session {self.sid} survived "
                "SIGTERM and SIGKILL"
            ),
        )
