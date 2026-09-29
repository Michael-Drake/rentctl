# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Drake

"""The in-process side of the per-lease supervisor (ADR-0016 §3, §6, §10, R0).

``rentctl/supervisor.py`` is the long-lived process. This module is what the
rest of rentctl calls to deal with one, and it stays importable from
``core/service.py`` without a cycle (it imports nothing above ``core``):

* :func:`spawn_supervisor` — start a detached supervisor for ``(key,
  generation)`` and return its identity. The caller records it (``SPAWNED``)
  under L, per §6 up-step 2.
* :func:`wake_supervisor` — the verified SIGWINCH of §3. Optional by design:
  the supervisor stats its lease every second, so a lost wake costs 1 s.
* :func:`supervisor_alive` — pid + start time + cmdline marker, the §10
  liveness input.
* :func:`recover_lease` — the §10 recovery for a lease nobody supervises:
  claim, stop in recovery mode, record the outcome. R0's detached recovery
  supervisor runs exactly this; a reconciler may also run it in-process.

None of these ever sends SIGTERM or SIGKILL to a supervisor. A supervisor ends
itself (§3).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psutil

from . import events as ev
from . import procutil
from .errors import SUPERVISOR_START_FAILED, DevctlError
from .events import EventLog
from .leases import Lease, ProcessRef, SupervisorRef
from .lifecycle import (
    REMOVED_ON_REACH,
    Actor,
    ActorKind,
    Event,
    EventKind,
    LifecycleAction,
    Observation,
    State,
    decide,
    generation_of,
    state_of,
    transition,
)
from .locking import project_lock
from .models import StopOutcome
from .paths import DevctlPaths, project_from_key
from .procutil import Membership, ProcessTable
from .workload import KILL_GRACE_S, RECOVERY, TERM_GRACE_S, stop_workload

SUPERVISOR_MODULE = "rentctl.supervisor"
RECOVER_FLAG = "--recover"
# The wake. Its default action is *ignore*, so one delivered to the wrong process
# is harmless (ADR-0016 E5, R5). SIGURG was the other candidate; Go runtimes use
# it for preemption.
WAKE_SIGNAL = signal.SIGWINCH


def _now() -> datetime:
    return datetime.now().astimezone()


def self_ref() -> ProcessRef:
    """This process, named the way every lease names a process: pid + start time."""
    pid = os.getpid()
    return ProcessRef(pid, procutil.observe_start_time(pid) or 0.0)


# ==========================================================================
# spawning, liveness, wake
# ==========================================================================

def supervisor_argv(
    key: str,
    generation: str,
    *,
    recover: bool = False,
    reason: str | None = None,
    reason_source: str = ev.DECLARED,
    op: str | None = None,
) -> list[str]:
    """The supervisor's argv. The key and generation are positional so the
    §3 cmdline check can find the key without parsing flags."""
    argv = [sys.executable, "-m", SUPERVISOR_MODULE]
    if recover:
        argv.append(RECOVER_FLAG)
        if reason is not None:
            argv += ["--reason", reason, "--reason-source", reason_source]
        if op is not None:
            argv += ["--op", op]
    return [*argv, key, generation]


def spawn_supervisor(
    key: str,
    generation: str,
    *,
    paths: DevctlPaths | None = None,
    recover: bool = False,
    reason: str | None = None,
    reason_source: str = ev.DECLARED,
    op: str | None = None,
    extra_env: Mapping[str, str] | None = None,
) -> SupervisorRef:
    """Start a detached supervisor for ``(key, generation)``; return who it is.

    ``start_new_session=True`` makes it a session and group leader before it
    runs a line of Python, so S (its pid) exists from birth. The returned
    :class:`SupervisorRef` is ``registered=False``: the CLI writes it under L
    (``SPAWNED``) and the supervisor's own registration flips it (§6).

    The caller is the parent at this moment, so the pid cannot be recycled
    before its start time is read — that is why the read happens here rather
    than later from the lease.

    ``paths`` pins the child to the same state root as the caller. Without it
    the child resolves its own from the environment, which is the same thing in
    production and a leak into live state in a test that forgot.

    Raises ``DevctlError(SUPERVISOR_START_FAILED)`` if it could not be started.
    """
    env = dict(os.environ)
    if paths is not None:
        env["RENTCTL_STATE_HOME"] = str(paths.state_home)
        env["RENTCTL_CONFIG_HOME"] = str(paths.config_home)
    if extra_env:
        env.update(extra_env)
    argv = supervisor_argv(
        key, generation, recover=recover, reason=reason, reason_source=reason_source, op=op
    )
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            env=env,
        )
    except OSError as e:
        raise DevctlError(SUPERVISOR_START_FAILED, f"could not start the supervisor: {e}") from e
    started = procutil.observe_start_time(proc.pid)
    if started is None:
        # Dead before we could name it. A lease must never record a supervisor
        # by pid alone, so this is a failed spawn, not a success with a gap.
        raise DevctlError(
            SUPERVISOR_START_FAILED, f"supervisor {proc.pid} exited before it could be identified"
        )
    return SupervisorRef(proc.pid, started, registered=False)


def is_supervisor(pid: int, start_time: float | None, key: str) -> bool:
    """Is ``pid`` still the supervisor of ``key`` that started at ``start_time``?

    All three checks of §3: the start time matches, and the cmdline names both
    the supervisor module and this lease key. On macOS another user's cmdline is
    AccessDenied, which reads as "not ours" — correct, since ours runs as us.
    """
    if pid <= 0 or pid == os.getpid() or start_time is None:
        return False
    try:
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return False
        if not procutil.start_time_matches(start_time, proc.create_time()):
            return False
        cmdline = proc.cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False
    return any(SUPERVISOR_MODULE in arg for arg in cmdline) and key in cmdline


def supervisor_alive(ref: SupervisorRef | ProcessRef, key: str) -> bool:
    """The §10 liveness input for a lease's supervisor."""
    return is_supervisor(ref.pid, ref.start_time, key)


def wake_supervisor(ref: SupervisorRef | ProcessRef, key: str) -> bool:
    """Send the verified SIGWINCH wake (§3, §8). Returns whether it was sent.

    Sent only after the identity check passes. The window between the check
    and ``kill`` is harmless here in a way it is not for TERM/KILL: SIGWINCH's
    default action is to ignore, so a recycled pid would not even notice.
    """
    if not is_supervisor(ref.pid, ref.start_time, key):
        return False
    try:
        os.kill(ref.pid, WAKE_SIGNAL)
    except OSError:
        return False
    return True


def _ref_alive(ref: ProcessRef) -> bool:
    return procutil.start_time_matches(ref.start_time, procutil.observe_start_time(ref.pid))


# ==========================================================================
# events shared by the supervisor and the recoverer (§13)
# ==========================================================================

def leader_pid(lease: Lease) -> int | None:
    pid = (lease.handle or {}).get("pid")
    return None if pid is None else int(pid)


def escaped_listener(port: int, sid: int) -> dict[str, Any] | None:
    """§4 step 5: after a verified stop, is something outside S still on the port?

    Recorded, never signalled: rentctl does not own it. A probe that cannot run
    reports nothing rather than guessing — this is evidence, not a verdict.
    """
    try:
        owner = procutil.port_owner(port)
    except procutil.ProbeUnavailable:
        return None
    if owner is None:
        return None
    try:
        if os.getsid(owner.pid) == sid:
            return None
    except OSError:
        pass
    return {"pid": owner.pid, "name": owner.name}


def record_down(
    log: EventLog,
    lease: Lease,
    *,
    actor: str,
    outcome: StopOutcome | None,
    reason: str,
    reason_source: str,
    op: str,
    mode: str | None = None,
    supervisor_lost: bool = False,
    escaped: dict[str, Any] | None = None,
) -> bool:
    """The one ``down`` per terminal teardown, written by whoever verified it."""
    cleanup = lease.cleanup
    stop = lease.stop
    # ADR-0017 §7, §9: which sessions this teardown concerned. The request
    # carries who released the last claim or whose claims a deliberate stop
    # overrode; an expiry lists the claims that lapsed.
    lapsed = sorted(lease.claim_map()) if reason in (ev.EXPIRY, ev.SWEEP_EXPIRED) else None
    return log.record_down(
        lease.project,
        op=op,
        reason=reason,
        reason_source=reason_source,
        killed=bool(outcome and outcome.signalled),
        port=lease.port,
        pid=leader_pid(lease),
        cleanup=None if outcome is None else outcome.cleanup,
        escalated=None if outcome is None else outcome.escalated,
        generation=lease.generation,
        actor=actor,
        mode=mode,
        attempts=None if cleanup is None else cleanup.attempts,
        supervisor_lost=supervisor_lost or None,
        escaped_listener=escaped,
        supervisor_pid=None if lease.supervisor is None else lease.supervisor.pid,
        released_by=None if stop is None else stop.released_by,
        overrode_claims=None if stop is None or stop.overrode_claims is None
        else list(stop.overrode_claims),
        lapsed_claims=lapsed or None,
    )


def record_cleanup_incomplete(
    log: EventLog,
    lease: Lease,
    outcome: StopOutcome,
    *,
    reason: str,
    reason_source: str,
    op: str,
) -> bool:
    return log.record_cleanup_incomplete(
        lease.project,
        op=op,
        reason=reason,
        reason_source=reason_source,
        survivors=outcome.survivor_dicts(),
        identity_ambiguous=outcome.identity_ambiguous,
        escalated=outcome.escalated,
        port=lease.port,
        pid=leader_pid(lease),
        detail=outcome.detail,
        generation=lease.generation,
        attempt=None if lease.cleanup is None else lease.cleanup.attempts,
    )


def record_up_failed(
    log: EventLog, lease: Lease, *, error: dict[str, Any], cleanup: str | None
) -> bool:
    return log.record_up_failed(
        lease.project,
        profile=lease.profile,
        error=str(error.get("code")),
        port=lease.port,
        generation=lease.generation,
        phase=error.get("phase"),
        cleanup=cleanup,
        message=error.get("message"),
    )


def record_supervisor_lost(
    log: EventLog, lease: Lease, *, state_before: State, membership: Membership, detected_by: str
) -> bool:
    return log.record(
        ev.SUPERVISOR_LOST,
        lease.project,
        generation=generation_of(lease),
        state_before=state_before.value,
        members=membership.value,
        detected_by=detected_by,
        supervisor_pid=None if lease.supervisor is None else lease.supervisor.pid,
        port=lease.port,
    )


def record_stop_requested(log: EventLog, lease: Lease, requester: ProcessRef) -> bool:
    stop = lease.stop
    if stop is None:  # pragma: no cover - only called after a STOP_REQUEST wrote one
        return False
    return log.record(
        ev.STOP_REQUESTED,
        lease.project,
        generation=stop.generation,
        op=stop.op,
        reason=stop.reason,
        reason_source=stop.reason_source,
        requester_pid=requester.pid,
        port=lease.port,
        released_by=stop.released_by,
        overrode_claims=None if stop.overrode_claims is None else list(stop.overrode_claims),
    )


# ==========================================================================
# recovery (§10, R0)
# ==========================================================================

RECOVERED_STOPPED = "stopped"
RECOVERED_INCOMPLETE = "cleanup_incomplete"
RECOVERED_STARTUP_FAILED = "startup_failed"
RECOVERED_CLEANED = "cleaned"          # nothing to stop: the record was cleaned or removed
RECOVERED_KEPT = "kept"                # nothing safe to do (owned, within lease, ambiguous, claimed)
RECOVERED_GONE = "gone"                # no lease at that generation
RECOVERED_SUPERSEDED = "superseded"    # the claim was lost while the stop ran


@dataclass(frozen=True)
class RecoveryResult:
    """What :func:`recover_lease` did. ``outcome`` is one of the ``RECOVERED_*``."""

    outcome: str
    detail: str = ""
    stop: StopOutcome | None = None


def observe(
    lease: Lease,
    key: str,
    *,
    table: ProcessTable | None = None,
    alive_fn: Callable[[SupervisorRef, str], bool] = supervisor_alive,
) -> Observation:
    """Measure the §10 inputs for one lease: supervisor, membership, claim holder."""
    sup = lease.supervisor
    ident = lease.ownership()
    if ident is None:
        membership = Membership.EMPTY  # I1: an unregistered start launched nothing
    else:
        membership = procutil.session_scan(
            ident.sid, ident.owner_start, ident.exclude, table=table
        ).state
    return Observation(
        supervisor_alive=sup is not None and alive_fn(sup, key),
        membership=membership,
        claim_holder_alive=lease.recovery is not None and _ref_alive(lease.recovery.ref),
    )


class _Recoverer:
    """One recovery run. Holds the actors and the event log; not reusable."""

    def __init__(
        self,
        key: str,
        generation: str,
        paths: DevctlPaths,
        log: EventLog,
        me: ProcessRef,
        now_fn: Callable[[], datetime],
        actor_label: str,
        table: ProcessTable | None,
        alive_fn: Callable[[SupervisorRef, str], bool],
    ) -> None:
        self.key = key
        self.generation = generation
        self.paths = paths
        self.log = log
        self.me = me
        self.now = now_fn
        self.actor_label = actor_label
        self.table = table
        self.alive_fn = alive_fn
        self.path = paths.lease_file(key)
        self.lock = paths.lock_file(project_from_key(key))
        self.cli = Actor(ActorKind.CLI, generation, me)
        self.reconciler = Actor(ActorKind.RECONCILER, generation, me)
        self.recoverer = Actor(ActorKind.RECOVERER, generation, me)

    def read(self) -> Lease | None:
        try:
            lease = Lease.read_if_exists(self.path)
        except DevctlError:
            return None  # corrupt: not provably this generation, so not ours to act on
        if lease is None or generation_of(lease) != self.generation:
            return None
        return lease

    def event(self, kind: EventKind, obs: Observation, reason: str | None, **kw: Any) -> Event:
        return Event(
            kind,
            self.now(),
            reason=reason,
            supervisor_alive=obs.supervisor_alive,
            membership=obs.membership,
            claim_holder_alive=obs.claim_holder_alive,
            **kw,
        )

    def store(self, lease: Lease) -> None:
        """Write, or remove a record whose terminal state says it goes (§7).

        ``startup_failed`` is written, not removed: its waiter may still come
        for the error, and reconcilers remove it once nobody has (§7)."""
        if state_of(lease) in REMOVED_ON_REACH:
            self.path.unlink(missing_ok=True)
        else:
            lease.write(self.path)

    # --- under L: decide, claim ------------------------------------------------

    def claim(
        self, reason: str | None, reason_source: str, op: str | None
    ) -> tuple[Lease | None, RecoveryResult | None]:
        """Apply the §10 decision. Returns the claimed lease, or a final result."""
        lease = self.read()
        if lease is None:
            return None, RecoveryResult(RECOVERED_GONE, "no lease at this generation")
        if reason is not None:
            req = transition(
                lease,
                Event(EventKind.STOP_REQUEST, self.now(), reason=reason,
                      reason_source=reason_source, op=op or ev.DOWN),
                self.cli,
            )
            if isinstance(req, Lease) and req is not lease:
                req.write(self.path)
                record_stop_requested(self.log, req, self.me)
                lease = req
        for _ in range(3):
            obs = observe(lease, self.key, table=self.table, alive_fn=self.alive_fn)
            d = decide(lease, obs, self.now())
            if d.action is not LifecycleAction.MARK_UNSUPERVISED:
                break
            lost = transition(lease, self.event(EventKind.SUPERVISOR_LOST, obs, None), self.reconciler)
            if not isinstance(lost, Lease):
                return None, RecoveryResult(RECOVERED_KEPT, lost.detail)
            lost.write(self.path)
            record_supervisor_lost(self.log, lease, state_before=state_of(lease),
                                   membership=obs.membership, detected_by=self.actor_label)
            lease = lost
        return self._apply(lease, obs, d)

    def _apply(self, lease, obs, d) -> tuple[Lease | None, RecoveryResult | None]:
        action = d.action
        if action is LifecycleAction.REMOVE:
            self.path.unlink(missing_ok=True)
            return None, RecoveryResult(RECOVERED_CLEANED, d.reason)
        if action is LifecycleAction.CLEAN:
            return None, self._clean(lease, obs, d)
        if action is LifecycleAction.KEEP_AMBIGUOUS:
            if d.events:
                flagged = transition(lease, self.event(EventKind.IDENTITY_AMBIGUOUS, obs, None),
                                     self.reconciler)
                if isinstance(flagged, Lease) and flagged is not lease:
                    flagged.write(self.path)
                    self.log.record_cleanup_incomplete(
                        lease.project, op=self.actor_label, reason=ev.SWEEP_EXPIRED,
                        reason_source=ev.INFERRED, survivors=[], identity_ambiguous=True,
                        escalated=False, port=lease.port, generation=lease.generation,
                    )
            return None, RecoveryResult(RECOVERED_KEPT, d.reason)
        if action is LifecycleAction.NUDGE_EXPIRY:
            # A live supervisor past its expiry: ask it on disk and wake it. The
            # workload is never signalled from here (§10 row 1).
            req = transition(
                lease,
                self.event(EventKind.STOP_REQUEST, obs, d.stop_reason,
                           reason_source=ev.DECLARED, op=self.actor_label),
                self.reconciler,
            )
            if isinstance(req, Lease) and req is not lease:
                req.write(self.path)
                record_stop_requested(self.log, req, self.me)
                if req.supervisor is not None:
                    wake_supervisor(req.supervisor, self.key)
            return None, RecoveryResult(RECOVERED_KEPT, d.reason)
        if action is not LifecycleAction.RECOVER_STOP:
            return None, RecoveryResult(RECOVERED_KEPT, d.reason)
        for kind in d.events:
            actor = self.reconciler if kind is EventKind.SUPERVISOR_LOST else self.recoverer
            nxt = transition(
                lease,
                self.event(kind, obs, d.stop_reason, reason_source=ev.DECLARED, op=self.actor_label),
                actor,
            )
            if not isinstance(nxt, Lease):
                return None, RecoveryResult(RECOVERED_KEPT, nxt.detail)
            if kind is EventKind.SUPERVISOR_LOST or (
                kind is EventKind.RECOVERY_CLAIM and state_of(lease) is State.STARTING
            ):
                record_supervisor_lost(self.log, lease, state_before=state_of(lease),
                                       membership=obs.membership, detected_by=self.actor_label)
            lease = nxt
        lease.write(self.path)
        return lease, None

    def _clean(self, lease: Lease, obs: Observation, d) -> RecoveryResult:
        before = lease
        for kind in d.events:
            nxt = transition(lease, self.event(kind, obs, None), self.reconciler)
            if not isinstance(nxt, Lease):
                return RecoveryResult(RECOVERED_KEPT, nxt.detail)
            lease = nxt
        after = state_of(lease)
        registered = before.supervisor is not None and before.supervisor.registered
        if EventKind.FOUND_EMPTY in d.events and registered:
            record_supervisor_lost(self.log, before, state_before=state_of(before),
                                   membership=obs.membership, detected_by=self.actor_label)
        if after is State.ABANDONED:
            self.log.record_up_failed(
                lease.project, profile=lease.profile, error=SUPERVISOR_START_FAILED, port=lease.port,
                generation=lease.generation, phase=ev.PHASE_REGISTRATION,
            )
        elif after is State.STARTUP_FAILED:
            record_up_failed(self.log, lease, error=lease.error or {}, cleanup=ev.CLEANUP_VERIFIED)
        else:
            stop = before.stop
            record_down(
                self.log, lease, actor=self.actor_label, outcome=None,
                reason=stop.reason if stop else ev.SWEEP_DEAD,
                reason_source=stop.reason_source if stop else ev.DECLARED,
                op=stop.op if stop else self.actor_label,
                supervisor_lost=registered,
            )
        self.store(lease)
        return RecoveryResult(RECOVERED_CLEANED, d.reason)

    # --- under L again: record the outcome ------------------------------------

    def settle(self, outcome: StopOutcome) -> RecoveryResult:
        cur = self.read()
        if cur is None or cur.recovery is None or cur.recovery.ref != self.me:
            # Our claim lapsed and another recoverer took over, or the record
            # went away. Its new owner records the outcome; writing ours too
            # would give one teardown two events.
            return RecoveryResult(RECOVERED_SUPERSEDED, "the recovery claim is no longer ours", outcome)
        if outcome.identity_ambiguous:
            # Nothing was signalled. The claim is left to lapse; the next
            # reconciler sees the ambiguity itself and keeps the record.
            self.log.record_cleanup_incomplete(
                cur.project, op=self.actor_label, reason=_reason(cur), reason_source=ev.DECLARED,
                survivors=[], identity_ambiguous=True, escalated=False, port=cur.port,
                generation=cur.generation, detail=outcome.detail,
            )
            return RecoveryResult(RECOVERED_KEPT, outcome.detail, outcome)
        kind = EventKind.STOP_VERIFIED if outcome.verified else EventKind.STOP_INCOMPLETE
        nxt = transition(cur, Event(kind, self.now(), survivors=outcome.survivors), self.recoverer)
        if not isinstance(nxt, Lease):
            return RecoveryResult(RECOVERED_KEPT, nxt.detail, outcome)
        target = state_of(nxt)
        stop = cur.stop
        if target is State.STOPPED:
            escaped = escaped_listener(cur.port, cur.ownership().sid) if cur.ownership() else None
            record_down(
                self.log, nxt, actor=self.actor_label, outcome=outcome, reason=_reason(cur),
                reason_source=stop.reason_source if stop else ev.DECLARED,
                op=stop.op if stop else self.actor_label, mode=ev.MODE_RECOVERY,
                supervisor_lost=cur.supervisor is not None, escaped=escaped,
            )
            self.path.unlink(missing_ok=True)
            return RecoveryResult(RECOVERED_STOPPED, "verified", outcome)
        nxt.write(self.path)
        if target is State.STARTUP_FAILED:
            record_up_failed(self.log, nxt, error=nxt.error or {}, cleanup=ev.CLEANUP_VERIFIED)
            return RecoveryResult(RECOVERED_STARTUP_FAILED, "verified", outcome)
        if state_of(cur) is State.STARTING:
            record_up_failed(self.log, nxt, error=nxt.error or {},
                             cleanup=ev.CLEANUP_INCOMPLETE_VALUE)
        record_cleanup_incomplete(
            self.log, nxt, outcome, reason=_reason(cur),
            reason_source=stop.reason_source if stop else ev.DECLARED,
            op=stop.op if stop else self.actor_label,
        )
        return RecoveryResult(RECOVERED_INCOMPLETE, outcome.detail, outcome)


def _reason(lease: Lease) -> str:
    return lease.stop.reason if lease.stop is not None else ev.PHASE_SUPERVISOR_LOST


def recover_lease(
    key: str,
    generation: str,
    *,
    paths: DevctlPaths | None = None,
    reason: str | None = None,
    reason_source: str = ev.DECLARED,
    op: str | None = None,
    actor_label: str = ev.ACTOR_CLI,
    events: EventLog | None = None,
    now_fn: Callable[[], datetime] = _now,
    me: ProcessRef | None = None,
    term_grace_s: float = TERM_GRACE_S,
    kill_grace_s: float = KILL_GRACE_S,
    table: ProcessTable | None = None,
    alive_fn: Callable[[SupervisorRef, str], bool] = supervisor_alive,
) -> RecoveryResult:
    """Run §10 for one lease nobody supervises, and stop it if the table says so.

    1. **Under L:** if ``reason`` is given, write that stop request first (a
       repeat is acknowledged, the first reason wins). Then apply the §10
       decision: mark ``unsupervised``, clean an empty or abandoned record, or
       take a **recovery claim** for a populated one. Anything else — a live
       supervisor, a live claim, an ambiguous identity, an unexpired lease with
       no stop — is left alone.
    2. **Without L:** ``stop_workload`` in recovery mode: every member gets its
       own verified signal, no group is signalled (§3).
    3. **Under L:** record the outcome if the claim is still ours.

    This is what R0's detached recovery supervisor runs, so a SessionEnd hook
    can hand a legacy/unsupervised lease to it and return inside its budget.
    """
    paths = paths or DevctlPaths.default()
    r = _Recoverer(
        key, generation, paths, events or EventLog(paths.events_file), me or self_ref(),
        now_fn, actor_label, table, alive_fn,
    )
    with project_lock(r.lock):
        lease, final = r.claim(reason, reason_source, op)
    if final is not None:
        return final
    assert lease is not None
    ident = lease.ownership()
    if ident is None:  # pragma: no cover - a claimed lease always names a session
        return RecoveryResult(RECOVERED_KEPT, "the claimed lease names no session")
    outcome = stop_workload(
        ident, RECOVERY, term_grace_s=term_grace_s, kill_grace_s=kill_grace_s, table=table
    )
    with project_lock(r.lock):
        return r.settle(outcome)


__all__ = [
    "RECOVER_FLAG",
    "SUPERVISOR_MODULE",
    "WAKE_SIGNAL",
    "RecoveryResult",
    "escaped_listener",
    "is_supervisor",
    "observe",
    "recover_lease",
    "self_ref",
    "spawn_supervisor",
    "supervisor_alive",
    "supervisor_argv",
    "wake_supervisor",
]
