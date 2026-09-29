# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Drake

"""The lease lifecycle — ADR-0016 §7, §8, §10 as pure functions.

Everything a supervisor, a waiting ``up``/``down``, or a reconciler may do to a
lease is one call to :func:`transition` under the project lock L. The function
takes the lease as read, one :class:`Event` and the :class:`Actor` performing
it, and returns either the lease to write or a :class:`Refused` saying why not.
No I/O, no signals, no clock except the ``now`` the event carries, no uuid
minting — the caller supplies all of those. That is what lets the races in the
ADR's acceptance scenarios #9–#11 be tested as every interleaving of writers
rather than as a timing lottery with real processes.

**The rules, and where they come from**

* One actor kind and one event kind per row of :data:`TABLE`, which is §7's
  transition table. A pair with no row is refused; nothing is inferred.
* Every actor names the generation it is acting for. A mismatch is refused —
  that single check is how a stale stop request, a stale supervisor argv, or a
  recoverer whose lease was replaced can never touch a newer generation (§8).
* **Acknowledgements return the input lease object itself.** A repeated stop
  request on a lease already stopping is accepted but changes nothing, and the
  caller tells the two apart by identity (``result is lease``): write nothing,
  record nothing. That is what makes repeated stops idempotent without a
  second event.
* The first stop reason written wins (§8). Later requests never overwrite it.
* Membership is an *input* (:class:`Membership`, produced by procutil's
  ``owner_reused``/``session_members`` in the parallel lane). Transitions that
  lead to signals demand ``MEMBERS``; ``AMBIGUOUS`` never leads to a signal.

**Legacy (1.0.x) leases** are read as ``unsupervised`` (§14) with a derived
generation (:func:`generation_of`). ``RENEW`` keeps them in legacy format, so
a 1.0.x watchdog still babysitting one keeps working. Any other transition
upgrades the record to schema 2 — which also makes that old watchdog read the
file as corrupt and exit without touching it (the §14 poison), exactly when a
recovering 1.1 CLI starts acting on it.

:func:`decide` is the §10 reconcile table for ``ls``/``sweep``/``up``/``down``;
the service applies it through ``supervision.recover_lease`` (plan step 7).
``reconcile.decide`` remains only for the legacy watchdog.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from . import events as ev
from .leases import (
    SCHEMA_SUPERVISED,
    SID_OWNER_SUPERVISOR,
    SID_OWNER_WORKLOAD,
    CleanupRecord,
    Lease,
    ProcessRef,
    RecoveryClaim,
    StopRequest,
    SupervisorRef,
    Survivor,
)

# procutil's tri-state answer for a lease's session. Imported, not redeclared:
# the two lanes each wrote one, and two enums with equal values still compare
# unequal by identity (`is`), which is how every check here reads them.
# ``AMBIGUOUS`` is never a licence to signal (§2, §10).
from .procutil import Membership

# --- timings (ADR-0016 §6, §10) ------------------------------------------------

REGISTRATION_TIMEOUT = timedelta(seconds=30)   # a `starting` lease with no registered supervisor
RECOVERY_CLAIM_LAPSE = timedelta(seconds=60)   # a claim older than this can be taken over
EXPIRY_NUDGE_GRACE = timedelta(seconds=5)      # a live supervisor this far past `expires` is hung
# How long a reconciler leaves an unconsumed `startup_failed` for its waiter.
# The ADR says "if the waiter died, any reconciler deletes it" without a number;
# the waiter polls every 100 ms, so anything this long means nobody is reading.
STARTUP_FAILED_CONSUME_GRACE = timedelta(seconds=30)
# Supervisor retry backoff for `cleanup_incomplete`: 5 s, 15 s, 60 s, then every 60 s (§7, R3).
CLEANUP_RETRY_BACKOFF = (timedelta(seconds=5), timedelta(seconds=15), timedelta(seconds=60))


class State(str, Enum):
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"                        # ⊗ removed by the actor that reaches it
    CLEANUP_INCOMPLETE = "cleanup_incomplete"
    UNSUPERVISED = "unsupervised"
    EXITED = "exited"                          # ⊗
    STARTUP_FAILED = "startup_failed"          # ◉ kept until its waiter consumes it
    ABANDONED = "abandoned"                    # ⊗


NON_TERMINAL = frozenset(
    {State.STARTING, State.RUNNING, State.STOPPING, State.CLEANUP_INCOMPLETE, State.UNSUPERVISED}
)
REMOVED_ON_REACH = frozenset({State.STOPPED, State.EXITED, State.ABANDONED})
CONSUMABLE = frozenset({State.STARTUP_FAILED})
TERMINAL = REMOVED_ON_REACH | CONSUMABLE


class ActorKind(str, Enum):
    CLI = "cli"                # `up`/`down`/MCP: creates, renews, requests stops, consumes
    SUPERVISOR = "supervisor"  # the lease's own supervisor
    RECONCILER = "reconciler"  # ls/sweep/up/down applying the §10 table
    RECOVERER = "recoverer"    # a CLI (or recovery supervisor, R0) holding a recovery claim


@dataclass(frozen=True)
class Actor:
    """Who performs a transition.

    ``generation`` is the generation this actor believes it is acting on: a
    supervisor's argv, the generation a requester or waiter read. It is always
    required, because the generation check is the whole of the stale-actor
    defence. ``process`` is the actor's own identity (pid + start time), written
    into stop requests, registrations and recovery claims.
    """

    kind: ActorKind
    generation: str | None
    process: ProcessRef | None = None


class EventKind(str, Enum):
    SPAWNED = "spawned"                    # CLI recorded the supervisor it spawned (§6 up step 2)
    REGISTER = "register"                  # supervisor registered itself (§6 supervisor step 1)
    LAUNCHED = "launched"                  # supervisor recorded the leader + session (§6 step 2)
    READY = "ready"                        # readiness answered and attributed by SID
    RENEW = "renew"                        # `up` again: rewrite `expires`
    STOP_REQUEST = "stop_request"          # a requester wrote `lease.stop` (§8)
    BEGIN_STOP = "begin_stop"              # the supervisor applies a request / expiry / SIGTERM
    STOP_VERIFIED = "stop_verified"        # stop_workload returned `verified`
    STOP_INCOMPLETE = "stop_incomplete"    # stop_workload returned `incomplete`
    SESSION_EMPTIED = "session_emptied"    # the session emptied on its own, no stop
    ABANDON = "abandon"                    # never registered, past its timeout
    SUPERVISOR_LOST = "supervisor_lost"    # registered supervisor dead, members present
    FOUND_EMPTY = "found_empty"            # supervisor dead (or none) and the session is empty
    RECOVERY_CLAIM = "recovery_claim"      # a recoverer claims a lease to stop it without L
    IDENTITY_AMBIGUOUS = "identity_ambiguous"  # PID S held by a different process


@dataclass(frozen=True)
class Event:
    """One thing that happened, with the payload its transition writes.

    Only the fields a given kind reads are consulted; the rest are ignored.
    ``supervisor_alive``/``membership``/``claim_holder_alive`` are the
    reconciler's observations, re-checked here so a stale plan cannot push a
    transition the evidence no longer supports.
    """

    kind: EventKind
    now: datetime
    reason: str | None = None
    reason_source: str = ev.DECLARED
    op: str | None = None
    expires: datetime | None = None
    supervisor: SupervisorRef | None = None
    handle: dict[str, Any] | None = None
    readiness: str | None = None
    survivors: tuple[Survivor, ...] = ()
    error: dict[str, Any] | None = None
    supervisor_alive: bool = False
    membership: Membership | None = None
    claim_holder_alive: bool = False


class RefusalCode(str, Enum):
    NO_RULE = "no_rule"                        # no §7 row for (state, event)
    WRONG_ACTOR = "wrong_actor"                # a row exists, but not for this actor kind
    STALE_GENERATION = "stale_generation"      # the actor names another generation
    NOT_OWNER = "not_owner"                    # a supervisor that is not this lease's
    NOT_REGISTERED = "not_registered"
    ALREADY_REGISTERED = "already_registered"
    NOT_CLAIM_HOLDER = "not_claim_holder"
    CLAIM_HELD = "claim_held"                  # a live, unlapsed recovery claim exists
    SUPERVISOR_ALIVE = "supervisor_alive"      # reconcilers never act over a live supervisor
    MEMBERSHIP = "membership"                  # the membership evidence does not fit
    NOT_EXPIRED = "not_expired"                # an expiry stop before `expires` (a stale timer)
    TOO_EARLY = "too_early"                    # abandon before the registration timeout
    STOP_PENDING = "stop_pending"              # renew/ready/exit while a stop is recorded
    NOTHING_TO_APPLY = "nothing_to_apply"      # BEGIN_STOP with no request and no reason
    MISSING_PAYLOAD = "missing_payload"
    LEGACY_ONLY = "legacy_only"


@dataclass(frozen=True)
class Refused:
    code: RefusalCode
    detail: str

    def __bool__(self) -> bool:
        # So `if result:` cannot be misread as "applied" — a refusal is falsy.
        return False


@dataclass(frozen=True)
class Rule:
    source: State
    kind: EventKind
    actors: frozenset[ActorKind]
    target: State
    emits: tuple[str, ...]  # the §13 event kinds this transition is recorded with


def _rule(source, kind, actors, target, emits=()) -> Rule:
    return Rule(source, kind, frozenset(actors), target, tuple(emits))


_S, _E, _A = State, EventKind, ActorKind
_STOPPERS = (_A.SUPERVISOR, _A.RECOVERER)
_REQUESTERS = (_A.CLI, _A.RECONCILER)

# ADR-0016 §7, one row per (from, event). The requester of a stop is the CLI
# (explicit/session-end/startup-abandoned) or a reconciler nudging a hung
# supervisor past expiry (§10 row 1). A startup teardown reuses the stop
# outcomes: from `starting`, "verified" is `startup_failed` and "survivors" is
# `cleanup_incomplete{phase: startup}`.
TABLE: tuple[Rule, ...] = (
    # starting
    _rule(_S.STARTING, _E.SPAWNED, [_A.CLI], _S.STARTING),
    _rule(_S.STARTING, _E.REGISTER, [_A.SUPERVISOR], _S.STARTING),
    _rule(_S.STARTING, _E.LAUNCHED, [_A.SUPERVISOR], _S.STARTING),
    _rule(_S.STARTING, _E.READY, [_A.SUPERVISOR], _S.RUNNING, [ev.UP]),
    _rule(_S.STARTING, _E.STOP_REQUEST, _REQUESTERS, _S.STARTING, [ev.STOP_REQUESTED]),
    _rule(_S.STARTING, _E.STOP_VERIFIED, _STOPPERS, _S.STARTUP_FAILED, [ev.UP_FAILED]),
    _rule(
        _S.STARTING, _E.STOP_INCOMPLETE, _STOPPERS, _S.CLEANUP_INCOMPLETE,
        [ev.UP_FAILED, ev.CLEANUP_INCOMPLETE],
    ),
    _rule(_S.STARTING, _E.ABANDON, [_A.RECONCILER], _S.ABANDONED, [ev.UP_FAILED]),
    _rule(_S.STARTING, _E.RECOVERY_CLAIM, [_A.RECOVERER], _S.STARTING, [ev.SUPERVISOR_LOST]),
    _rule(
        _S.STARTING, _E.FOUND_EMPTY, [_A.RECONCILER], _S.STARTUP_FAILED,
        [ev.SUPERVISOR_LOST, ev.UP_FAILED],
    ),
    # running
    _rule(_S.RUNNING, _E.RENEW, [_A.CLI], _S.RUNNING, [ev.UP]),
    _rule(_S.RUNNING, _E.STOP_REQUEST, _REQUESTERS, _S.RUNNING, [ev.STOP_REQUESTED]),
    _rule(_S.RUNNING, _E.BEGIN_STOP, [_A.SUPERVISOR], _S.STOPPING),
    _rule(_S.RUNNING, _E.SESSION_EMPTIED, [_A.SUPERVISOR], _S.EXITED, [ev.DOWN]),
    _rule(_S.RUNNING, _E.SUPERVISOR_LOST, [_A.RECONCILER], _S.UNSUPERVISED, [ev.SUPERVISOR_LOST]),
    _rule(_S.RUNNING, _E.FOUND_EMPTY, [_A.RECONCILER], _S.EXITED, [ev.SUPERVISOR_LOST, ev.DOWN]),
    # unsupervised (and every legacy lease)
    _rule(_S.UNSUPERVISED, _E.RENEW, [_A.CLI], _S.UNSUPERVISED, [ev.UP]),
    _rule(_S.UNSUPERVISED, _E.STOP_REQUEST, _REQUESTERS, _S.UNSUPERVISED, [ev.STOP_REQUESTED]),
    _rule(_S.UNSUPERVISED, _E.RECOVERY_CLAIM, [_A.RECOVERER], _S.STOPPING),
    _rule(_S.UNSUPERVISED, _E.FOUND_EMPTY, [_A.RECONCILER], _S.EXITED, [ev.DOWN]),
    # stopping
    _rule(_S.STOPPING, _E.STOP_REQUEST, _REQUESTERS, _S.STOPPING),  # acknowledged, unchanged
    _rule(_S.STOPPING, _E.STOP_VERIFIED, _STOPPERS, _S.STOPPED, [ev.DOWN]),
    _rule(_S.STOPPING, _E.STOP_INCOMPLETE, _STOPPERS, _S.CLEANUP_INCOMPLETE, [ev.CLEANUP_INCOMPLETE]),
    _rule(_S.STOPPING, _E.RECOVERY_CLAIM, [_A.RECOVERER], _S.STOPPING),
    _rule(_S.STOPPING, _E.FOUND_EMPTY, [_A.RECONCILER], _S.STOPPED, [ev.SUPERVISOR_LOST, ev.DOWN]),
    # cleanup_incomplete
    _rule(_S.CLEANUP_INCOMPLETE, _E.STOP_REQUEST, _REQUESTERS, _S.CLEANUP_INCOMPLETE),  # → retry
    _rule(_S.CLEANUP_INCOMPLETE, _E.STOP_VERIFIED, _STOPPERS, _S.STOPPED, [ev.DOWN]),
    _rule(_S.CLEANUP_INCOMPLETE, _E.STOP_INCOMPLETE, _STOPPERS, _S.CLEANUP_INCOMPLETE),
    _rule(_S.CLEANUP_INCOMPLETE, _E.RECOVERY_CLAIM, [_A.RECOVERER], _S.CLEANUP_INCOMPLETE),
    _rule(
        _S.CLEANUP_INCOMPLETE, _E.FOUND_EMPTY, [_A.RECONCILER], _S.STOPPED,
        [ev.SUPERVISOR_LOST, ev.DOWN],
    ),
    _rule(
        _S.CLEANUP_INCOMPLETE, _E.IDENTITY_AMBIGUOUS, [_A.RECONCILER], _S.CLEANUP_INCOMPLETE,
        [ev.CLEANUP_INCOMPLETE],
    ),
)

RULES: dict[tuple[State, EventKind], Rule] = {(r.source, r.kind): r for r in TABLE}
assert len(RULES) == len(TABLE), "duplicate (state, event) row in the lifecycle table"


# --- reading a lease's lifecycle ---------------------------------------------------

def state_of(lease: Lease) -> State:
    """A legacy lease is ``unsupervised``: no supervisor owns it (§14)."""
    if lease.is_legacy:
        return State.UNSUPERVISED
    return State(lease.state)


def generation_of(lease: Lease) -> str:
    """The lease's generation. A legacy lease has none on disk, so one is
    derived from its leader's identity — unique per incarnation for the same
    reason the PID-recycle guard is (spec §5.2)."""
    if lease.generation is not None:
        return lease.generation
    h = lease.handle or {}
    return f"legacy-{h.get('pid')}-{h.get('pid_start_time')}"


def holds_port(lease: Lease) -> bool:
    """Terminal states never hold a port in the draw (§7); everything else does."""
    return state_of(lease) not in TERMINAL


def _since(lease: Lease) -> datetime:
    return lease.state_since or lease.created


def claim_lapsed(claim: RecoveryClaim | None, *, holder_alive: bool, now: datetime) -> bool:
    """A claim whose holder is dead, or that is older than 60 s, can be taken over (§10)."""
    if claim is None:
        return True
    return (not holder_alive) or now - claim.since >= RECOVERY_CLAIM_LAPSE


def cleanup_retry_delay(attempts: int) -> timedelta:
    """The supervisor's backoff after ``attempts`` failed stops: 5 s, 15 s, 60 s, 60 s…"""
    idx = min(max(attempts, 1), len(CLEANUP_RETRY_BACKOFF)) - 1
    return CLEANUP_RETRY_BACKOFF[idx]


def retry_due(lease: Lease, now: datetime) -> bool:
    """Should the supervisor retry a ``cleanup_incomplete`` stop now?

    Immediately when a later stop request asked for it (§8), otherwise on the
    backoff schedule. Never for any other state.
    """
    if state_of(lease) is not State.CLEANUP_INCOMPLETE:
        return False
    c = lease.cleanup or CleanupRecord()
    if c.retry_requested is not None:
        return True
    if c.last_attempt is None:
        return True
    return now >= c.last_attempt + cleanup_retry_delay(c.attempts)


# --- creating a lease ------------------------------------------------------------------

def new_starting_lease(
    *,
    generation: str,
    project: str,
    profile: str,
    runner: str,
    port: int,
    session: str,
    cwd: str,
    spawn_cwd: str | None,
    log: str,
    plan: dict[str, Any],
    now: datetime,
    expires: datetime,
) -> Lease:
    """The §6 step-1 record: ``starting``, a fresh generation, no supervisor.

    ``generation`` is minted by the caller (uuid4 hex) so this stays pure.
    """
    return Lease(
        project=project,
        profile=profile,
        runner=runner,
        handle={},
        port=port,
        session=session,
        cwd=cwd,
        created=now,
        expires=expires,
        log=log,
        spawn_cwd=spawn_cwd,
        schema=SCHEMA_SUPERVISED,
        generation=generation,
        state=State.STARTING.value,
        state_since=now,
        plan=dict(plan),
    )


def supervisor_owns(lease: Lease | None, generation: str) -> bool:
    """A supervisor keeps going only while its lease exists at its generation.

    False is §7's last row: the lease is missing or replaced, so the supervisor
    stops **its own** session and exits (``down{reason: lease-lost}``). A
    supervisor started with a stale argv therefore launches nothing (#11).
    """
    return lease is not None and not lease.is_legacy and lease.generation == generation


# --- the transition function -------------------------------------------------------------

def transition(lease: Lease, event: Event, actor: Actor) -> Lease | Refused:
    """Apply one §7 transition. Returns the lease to write, or why not.

    A returned lease that ``is`` the input is an acknowledgement: nothing to
    write and nothing to record.
    """
    source = state_of(lease)
    rule = RULES.get((source, event.kind))
    if rule is None:
        return Refused(RefusalCode.NO_RULE, f"no transition for {event.kind.value} from {source.value}")
    if actor.kind not in rule.actors:
        return Refused(
            RefusalCode.WRONG_ACTOR,
            f"{event.kind.value} from {source.value} is not a {actor.kind.value}'s to make",
        )
    if actor.generation != generation_of(lease):
        return Refused(
            RefusalCode.STALE_GENERATION,
            f"actor is acting for generation {actor.generation!r}, "
            f"the lease is {generation_of(lease)!r}",
        )
    if actor.kind is ActorKind.SUPERVISOR and event.kind is not EventKind.REGISTER:
        refusal = _check_registered_supervisor(lease, actor)
        if refusal is not None:
            return refusal
    if actor.kind is ActorKind.RECOVERER and event.kind is not EventKind.RECOVERY_CLAIM:
        if lease.recovery is None or actor.process != lease.recovery.ref:
            return Refused(RefusalCode.NOT_CLAIM_HOLDER, "the recovery claim is not this actor's")

    handler = _HANDLERS[event.kind]
    result = handler(lease, event, actor, rule)
    if isinstance(result, Refused) or result is lease:
        return result
    return _settle(lease, result, rule.target, event.now)


def _settle(before: Lease, after: Lease, target: State, now: datetime) -> Lease:
    """Stamp the target state; a legacy lease is upgraded unless it stays legacy."""
    if after.is_legacy:
        return after  # only RENEW keeps legacy format, and it never changes state
    changed = state_of(before) is not target or before.is_legacy
    return replace(
        after,
        state=target.value,
        state_since=now if changed else after.state_since,
    )


def _upgrade(lease: Lease) -> Lease:
    """Carry a legacy lease into schema 2, preserving its identity (§2, §14).

    The handle gains the ``sid`` fields the legacy mapping implies, so the
    upgraded record still names the same session. The watchdog fields are not
    written by a schema-2 file; the 1.0.x watchdog then reads the poison, gets
    CORRUPT and exits without touching anything.
    """
    if not lease.is_legacy:
        return lease
    owner = lease.ownership()
    handle = dict(lease.handle)
    if owner is not None:
        handle.update(
            sid=owner.sid,
            sid_owner_start_time=owner.sid_owner_start_time,
            sid_owner=SID_OWNER_WORKLOAD,
        )
    return replace(
        lease,
        schema=SCHEMA_SUPERVISED,
        generation=generation_of(lease),
        state=State.UNSUPERVISED.value,
        state_since=lease.created,
        handle=handle,
    )


def _check_registered_supervisor(lease: Lease, actor: Actor) -> Refused | None:
    sup = lease.supervisor
    if sup is None or not sup.registered:
        return Refused(RefusalCode.NOT_REGISTERED, "no registered supervisor on this lease")
    if actor.process != sup.ref:
        return Refused(RefusalCode.NOT_OWNER, "this supervisor is not the lease's registered one")
    return None


def _on_spawned(lease, event, actor, rule):
    if event.supervisor is None:
        return Refused(RefusalCode.MISSING_PAYLOAD, "SPAWNED needs the supervisor's pid and start time")
    if lease.supervisor is not None:
        return Refused(RefusalCode.ALREADY_REGISTERED, "a supervisor is already recorded")
    return replace(lease, supervisor=replace(event.supervisor, registered=False))


def _on_register(lease, event, actor, rule):
    if actor.process is None:
        return Refused(RefusalCode.MISSING_PAYLOAD, "a supervisor registers with its own identity")
    sup = lease.supervisor
    if sup is not None and sup.registered:
        return Refused(RefusalCode.ALREADY_REGISTERED, "already registered")
    if sup is not None and sup.ref != actor.process:
        # The CLI recorded a different process as this generation's supervisor.
        return Refused(RefusalCode.NOT_OWNER, "the CLI spawned a different supervisor")
    if lease.stop is not None:
        # Abandoned by its waiter before it ever launched: I1 says exit clean.
        return Refused(RefusalCode.STOP_PENDING, "a stop was requested before registration")
    # The supervisor may state the guarantee it established (plan step 9); only
    # that field of the payload is read — its identity is the actor's own.
    supervision = event.supervisor.supervision if event.supervisor is not None else None
    return replace(
        lease,
        supervisor=SupervisorRef(
            actor.process.pid, actor.process.start_time, registered=True, supervision=supervision
        ),
    )


def _on_launched(lease, event, actor, rule):
    if event.handle is None:
        return Refused(RefusalCode.MISSING_PAYLOAD, "LAUNCHED needs the leader's handle")
    sup = lease.supervisor
    handle = dict(event.handle)
    # Session S *is* the supervisor (arrangement A), so the identity is derived
    # here rather than trusted from the payload.
    handle.update(sid=sup.pid, sid_owner_start_time=sup.start_time, sid_owner=SID_OWNER_SUPERVISOR)
    return replace(lease, handle=handle)


def _on_ready(lease, event, actor, rule):
    if lease.stop is not None:
        return Refused(RefusalCode.STOP_PENDING, "a stop was requested during startup; stop instead")
    if "sid" not in (lease.handle or {}):
        return Refused(RefusalCode.MISSING_PAYLOAD, "running before the launch was recorded")
    return replace(lease, readiness=event.readiness)


def _on_renew(lease, event, actor, rule):
    if event.expires is None:
        return Refused(RefusalCode.MISSING_PAYLOAD, "RENEW needs the new expiry")
    if lease.stop is not None:
        # The stop was decided first; `up` must wait for it and start fresh (#10).
        return Refused(RefusalCode.STOP_PENDING, "a stop is recorded for this generation")
    if state_of(lease) is State.UNSUPERVISED and not lease.is_legacy:
        # §14 renews *legacy* leases in place; §7 has no renew row for a
        # schema-2 unsupervised lease, whose supervisor is gone.
        return Refused(RefusalCode.LEGACY_ONLY, "only a legacy lease is renewed while unsupervised")
    return lease.renewed(event.expires)


def _on_stop_request(lease, event, actor, rule):
    if event.reason is None or event.op is None or actor.process is None:
        return Refused(RefusalCode.MISSING_PAYLOAD, "a stop request names its reason, op and requester")
    source = state_of(lease)
    if source is State.CLEANUP_INCOMPLETE:
        cleanup = lease.cleanup or CleanupRecord()
        return replace(lease, cleanup=replace(cleanup, retry_requested=event.now))
    if lease.stop is not None or source is State.STOPPING:
        return lease  # first request wins; a repeat is acknowledged, unchanged
    if event.reason in _EXPIRY_REASONS and event.now < lease.expires:
        # An expiry nudge planned from a read that a renew has since overtaken (#10).
        return Refused(RefusalCode.NOT_EXPIRED, f"lease runs until {lease.expires.isoformat()}")
    lease = _upgrade(lease)
    return replace(lease, stop=_stop_record(lease, event, actor.process, event.reason))


_EXPIRY_REASONS = frozenset({ev.EXPIRY, ev.SWEEP_EXPIRED})


def _stop_record(lease: Lease, event: Event, by: ProcessRef, reason: str) -> StopRequest:
    return StopRequest(
        generation=generation_of(lease),
        reason=reason,
        reason_source=event.reason_source,
        op=event.op or ev.DOWN,
        requested_at=event.now,
        requested_by=by,
    )


def _on_begin_stop(lease, event, actor, rule):
    if lease.stop is not None:
        # Apply the recorded request. Its reason is the teardown's, whatever
        # made the supervisor look (a wake, its expiry timer, a SIGTERM).
        return replace(lease)
    if event.reason is None:
        return Refused(RefusalCode.NOTHING_TO_APPLY, "no stop request recorded and no reason given")
    if event.reason in _EXPIRY_REASONS and event.now < lease.expires:
        # A timer armed for an expiry that a renew has since pushed out (#10).
        return Refused(RefusalCode.NOT_EXPIRED, f"lease runs until {lease.expires.isoformat()}")
    return replace(lease, stop=_stop_record(lease, event, actor.process, event.reason))


def _on_stop_verified(lease, event, actor, rule):
    cleanup = lease.cleanup or CleanupRecord()
    cleanup = replace(
        cleanup, attempts=cleanup.attempts + 1, last_attempt=event.now, survivors=(),
        retry_requested=None,
    )
    if rule.target is State.STARTUP_FAILED:
        error = event.error
        if error is None:
            if actor.kind is ActorKind.SUPERVISOR:
                return Refused(RefusalCode.MISSING_PAYLOAD, "a startup failure carries its error")
            error = _supervisor_lost_error()
        return replace(lease, cleanup=cleanup, error=dict(error), recovery=None)
    return replace(lease, cleanup=cleanup, recovery=None)


def _on_stop_incomplete(lease, event, actor, rule):
    if not event.survivors:
        return Refused(RefusalCode.MISSING_PAYLOAD, "an incomplete stop names its survivors")
    cleanup = lease.cleanup or CleanupRecord()
    phase = "startup" if state_of(lease) is State.STARTING else cleanup.phase
    cleanup = replace(
        cleanup,
        attempts=cleanup.attempts + 1,
        last_attempt=event.now,
        survivors=tuple(event.survivors),
        phase=phase,
        retry_requested=None,
    )
    error = lease.error
    if state_of(lease) is State.STARTING and event.error is not None:
        error = dict(event.error)
    # The recoverer's claim ends with its attempt: the record stays for the next
    # retry (supervisor backoff, or the next down/sweep), not for this holder.
    return replace(lease, cleanup=cleanup, error=error, recovery=None)


def _on_session_emptied(lease, event, actor, rule):
    if lease.stop is not None:
        return Refused(RefusalCode.STOP_PENDING, "a stop is recorded; finish it as a stop")
    return replace(lease)


def _on_abandon(lease, event, actor, rule):
    sup = lease.supervisor
    if sup is not None and sup.registered:
        return Refused(RefusalCode.ALREADY_REGISTERED, "a registered start is recovered, not abandoned")
    timed_out = event.now - _since(lease) >= REGISTRATION_TIMEOUT
    pid_dead = sup is not None and not event.supervisor_alive
    if not (timed_out or pid_dead):
        return Refused(RefusalCode.TOO_EARLY, "still inside the registration timeout")
    return replace(lease)


def _require_dead_supervisor(lease: Lease, event: Event) -> Refused | None:
    if lease.supervisor is not None and event.supervisor_alive:
        return Refused(RefusalCode.SUPERVISOR_ALIVE, "the supervisor is alive and owns this lease")
    return None


def _require_membership(event: Event, wanted: Membership) -> Refused | None:
    if event.membership is not wanted:
        got = None if event.membership is None else event.membership.value
        return Refused(RefusalCode.MEMBERSHIP, f"needs membership {wanted.value}, observed {got}")
    return None


def _first_refusal(*checks: Refused | None) -> Refused | None:
    # Not `a or b`: a Refused is falsy by design, so `or` would skip it.
    return next((c for c in checks if c is not None), None)


def _on_supervisor_lost(lease, event, actor, rule):
    if lease.supervisor is None or not lease.supervisor.registered:
        return Refused(RefusalCode.NOT_REGISTERED, "no registered supervisor to have lost")
    refusal = _first_refusal(
        _require_dead_supervisor(lease, event), _require_membership(event, Membership.MEMBERS)
    )
    return refusal if refusal is not None else replace(lease)


def _on_found_empty(lease, event, actor, rule):
    source = state_of(lease)
    if source is State.STARTING and (lease.supervisor is None or not lease.supervisor.registered):
        return Refused(RefusalCode.NOT_REGISTERED, "an unregistered start is abandoned, not emptied")
    refusal = _first_refusal(
        _require_dead_supervisor(lease, event), _require_membership(event, Membership.EMPTY)
    )
    if refusal is not None:
        return refusal
    lease = _upgrade(lease)
    if rule.target is State.STARTUP_FAILED:
        return replace(lease, error=_supervisor_lost_error(), recovery=None)
    return replace(lease, recovery=None)


def _on_recovery_claim(lease, event, actor, rule):
    if actor.process is None:
        return Refused(RefusalCode.MISSING_PAYLOAD, "a recovery claim names its holder")
    source = state_of(lease)
    if source is State.STARTING and (lease.supervisor is None or not lease.supervisor.registered):
        return Refused(RefusalCode.NOT_REGISTERED, "an unregistered start is abandoned, not recovered")
    refusal = _first_refusal(
        _require_dead_supervisor(lease, event), _require_membership(event, Membership.MEMBERS)
    )
    if refusal is not None:
        return refusal
    held = lease.recovery
    if held is not None and held.ref != actor.process and not claim_lapsed(
        held, holder_alive=event.claim_holder_alive, now=event.now
    ):
        return Refused(RefusalCode.CLAIM_HELD, f"recovery claimed by pid {held.pid}")
    stop = lease.stop
    if source is State.UNSUPERVISED and stop is None:
        if event.reason is None:
            return Refused(RefusalCode.MISSING_PAYLOAD, "recovering an unsupervised lease needs a reason")
        if event.reason in _EXPIRY_REASONS and event.now < lease.expires:
            return Refused(RefusalCode.NOT_EXPIRED, f"lease runs until {lease.expires.isoformat()}")
        stop =_stop_record(lease, event, actor.process, event.reason)
    lease = _upgrade(lease)  # keeps generation_of(lease), so `stop.generation` still matches
    claim =RecoveryClaim(actor.process.pid, actor.process.start_time, event.now)
    return replace(lease, recovery=claim, stop=stop)


def _on_identity_ambiguous(lease, event, actor, rule):
    refusal = _require_membership(event, Membership.AMBIGUOUS)
    if refusal is not None:
        return refusal
    cleanup = lease.cleanup or CleanupRecord()
    if cleanup.identity_ambiguous:
        return lease  # already surfaced; nothing new to record
    return replace(lease, cleanup=replace(cleanup, identity_ambiguous=True))


def _supervisor_lost_error() -> dict[str, Any]:
    return {
        "code": None,
        "message": "the supervisor died during startup; its session was recovered",
        "phase": ev.PHASE_SUPERVISOR_LOST,
        "log_tail": [],
    }


_HANDLERS = {
    EventKind.SPAWNED: _on_spawned,
    EventKind.REGISTER: _on_register,
    EventKind.LAUNCHED: _on_launched,
    EventKind.READY: _on_ready,
    EventKind.RENEW: _on_renew,
    EventKind.STOP_REQUEST: _on_stop_request,
    EventKind.BEGIN_STOP: _on_begin_stop,
    EventKind.STOP_VERIFIED: _on_stop_verified,
    EventKind.STOP_INCOMPLETE: _on_stop_incomplete,
    EventKind.SESSION_EMPTIED: _on_session_emptied,
    EventKind.ABANDON: _on_abandon,
    EventKind.SUPERVISOR_LOST: _on_supervisor_lost,
    EventKind.FOUND_EMPTY: _on_found_empty,
    EventKind.RECOVERY_CLAIM: _on_recovery_claim,
    EventKind.IDENTITY_AMBIGUOUS: _on_identity_ambiguous,
}
assert set(_HANDLERS) == set(EventKind), "every event kind needs a handler"


# --- terminal-record removal (§7 "Record removal") ----------------------------------------

def may_remove(lease: Lease, actor: Actor, now: datetime) -> bool:
    """May ``actor`` delete this record now?

    * ``stopped``/``exited``/``abandoned``: yes — the actor that reaches one
      removes it right after appending its event, and a reconciler finding one
      left by a crash between the write and the unlink finishes the job.
    * ``startup_failed``: by its waiter (a CLI at the same generation) once it
      has read the error; by a reconciler once nobody has consumed it for
      :data:`STARTUP_FAILED_CONSUME_GRACE`.
    * anything else, never: ``cleanup_incomplete`` and ``unsupervised`` are the
      only records naming their survivors.
    """
    state = state_of(lease)
    if state in REMOVED_ON_REACH:
        return True
    if state is State.STARTUP_FAILED:
        if actor.kind is ActorKind.CLI:
            return actor.generation == generation_of(lease)
        if actor.kind is ActorKind.RECONCILER:
            return now - _since(lease) >= STARTUP_FAILED_CONSUME_GRACE
    return False


# --- waiting (§8): the waiter reads the outcome, never infers one -----------------------------

class StopWait(str, Enum):
    STOPPED = "stopped"
    CLEANUP_INCOMPLETE = "cleanup_incomplete"
    PENDING = "pending"


def stop_wait_outcome(lease: Lease | None, generation: str) -> StopWait:
    """What a ``down`` waiter polling the lease has seen so far.

    Gone, or replaced by another generation, is ``stopped``: the generation it
    asked to stop no longer exists. A waiter must never read a *new*
    generation's state as its own (#11).

    ``startup_failed`` is ``stopped`` too. A stop requested while the lease was
    still ``starting`` ends there: the supervisor verified its session empty and
    kept the record only so the waiting ``up`` can read the error (§7). Reading
    it as pending would hold a ``down`` for its whole budget over nothing.
    """
    if lease is None or generation_of(lease) != generation:
        return StopWait.STOPPED
    state = state_of(lease)
    if state in REMOVED_ON_REACH or state is State.STARTUP_FAILED:
        return StopWait.STOPPED
    if state is State.CLEANUP_INCOMPLETE:
        return StopWait.CLEANUP_INCOMPLETE
    return StopWait.PENDING


class StartWait(str, Enum):
    RUNNING = "running"
    FAILED = "failed"                          # startup_failed: read `error`, then consume it
    CLEANUP_INCOMPLETE = "cleanup_incomplete"
    GONE = "gone"                              # removed or replaced — this start is over
    PENDING = "pending"


def start_wait_outcome(lease: Lease | None, generation: str) -> StartWait:
    """What an ``up`` waiter polling the lease has seen so far (§6)."""
    if lease is None or generation_of(lease) != generation:
        return StartWait.GONE
    state = state_of(lease)
    if state is State.RUNNING:
        return StartWait.RUNNING
    if state is State.STARTUP_FAILED:
        return StartWait.FAILED
    if state is State.CLEANUP_INCOMPLETE:
        return StartWait.CLEANUP_INCOMPLETE
    if state is State.STARTING:
        return StartWait.PENDING
    return StartWait.GONE


# --- the §10 reconcile table ----------------------------------------------------------------

class LifecycleAction(str, Enum):
    KEEP = "keep"                            # owned, or nothing safe to do
    NUDGE_EXPIRY = "nudge_expiry"            # live but hung supervisor past expiry: request a stop
    MARK_UNSUPERVISED = "mark_unsupervised"  # supervisor dead, workload serving, not expired
    RECOVER_STOP = "recover_stop"            # claim, then stop_workload(recovery)
    CLEAN = "clean"                          # apply the event(s); remove the record if allowed
    REMOVE = "remove"                        # a terminal record left behind: delete it
    KEEP_AMBIGUOUS = "keep_ambiguous"        # identity ambiguous: keep, surface, never signal


@dataclass(frozen=True)
class Observation:
    """What the impure caller measured for one lease, just before deciding.

    ``supervisor_alive`` is PID + start time (+ the cmdline marker) of
    ``lease.supervisor``; it is ignored when the lease records none.
    """

    supervisor_alive: bool
    membership: Membership
    claim_holder_alive: bool = False


@dataclass(frozen=True)
class Decision:
    lease: Lease
    action: LifecycleAction
    reason: str
    events: tuple[EventKind, ...] = ()   # to apply in order, as a RECONCILER then a RECOVERER
    stop_reason: str | None = None       # for a NUDGE_EXPIRY request or a fresh recovery stop

    @property
    def project(self) -> str:
        return self.lease.project


def decide(lease: Lease, obs: Observation, now: datetime) -> Decision:
    """ADR-0016 §10 for one lease. Row order matters and follows the table:
    ownership by a live supervisor first, then ambiguity (no signals), then
    the empty session, then the populated one."""
    state = state_of(lease)
    sup = lease.supervisor
    sup_alive = sup is not None and obs.supervisor_alive

    if state in REMOVED_ON_REACH:
        return Decision(lease, LifecycleAction.REMOVE, f"terminal {state.value} record left behind")

    if state is State.STARTUP_FAILED:
        reconciler = Actor(ActorKind.RECONCILER, generation_of(lease))
        if not sup_alive and obs.membership is Membership.EMPTY and may_remove(lease, reconciler, now):
            return Decision(lease, LifecycleAction.REMOVE, "startup_failed was never consumed")
        return Decision(lease, LifecycleAction.KEEP, "startup_failed awaiting its waiter")

    if state is State.STARTING and (sup is None or not sup.registered):
        timed_out = now - _since(lease) >= REGISTRATION_TIMEOUT
        if timed_out or (sup is not None and not obs.supervisor_alive):
            # I1: nothing was launched, so there is nothing to stop.
            return Decision(
                lease, LifecycleAction.CLEAN, "start never registered a supervisor",
                (EventKind.ABANDON,),
            )
        return Decision(lease, LifecycleAction.KEEP, "starting, awaiting registration")

    if sup_alive:
        if (
            state is State.RUNNING
            and lease.stop is None
            and now >= lease.expires + EXPIRY_NUDGE_GRACE
        ):
            # A hung or SIGSTOPped supervisor. Ask it on disk; never signal the workload.
            return Decision(
                lease, LifecycleAction.NUDGE_EXPIRY, "supervisor alive but past expiry",
                (EventKind.STOP_REQUEST,), ev.EXPIRY,
            )
        return Decision(lease, LifecycleAction.KEEP, "owned by a live supervisor")

    if obs.membership is Membership.AMBIGUOUS:
        flag = (
            (EventKind.IDENTITY_AMBIGUOUS,)
            if state is State.CLEANUP_INCOMPLETE
            and not (lease.cleanup and lease.cleanup.identity_ambiguous)
            else ()
        )
        return Decision(lease, LifecycleAction.KEEP_AMBIGUOUS, "PID S is held by another process", flag)

    if obs.membership is Membership.EMPTY:
        return Decision(
            lease, LifecycleAction.CLEAN, "supervisor gone and the session is empty",
            (EventKind.FOUND_EMPTY,),
        )

    # Members present, nobody supervising.
    if lease.recovery is not None and not claim_lapsed(
        lease.recovery, holder_alive=obs.claim_holder_alive, now=now
    ):
        return Decision(lease, LifecycleAction.KEEP, "another recoverer holds a live claim")

    expired = lease.is_expired(now)
    if state is State.STARTING:
        return Decision(
            lease, LifecycleAction.RECOVER_STOP, "supervisor died during startup",
            (EventKind.RECOVERY_CLAIM,),
        )
    if state is State.RUNNING:
        if expired:
            return Decision(
                lease, LifecycleAction.RECOVER_STOP, "supervisor gone and the lease expired",
                (EventKind.SUPERVISOR_LOST, EventKind.RECOVERY_CLAIM), ev.SWEEP_EXPIRED,
            )
        # Not killed early: a helper crash must not become a dev-server outage.
        return Decision(
            lease, LifecycleAction.MARK_UNSUPERVISED, "supervisor gone; workload still serving",
            (EventKind.SUPERVISOR_LOST,),
        )
    if state is State.UNSUPERVISED:
        if lease.stop is not None:
            return Decision(
                lease, LifecycleAction.RECOVER_STOP, "a stop is recorded and nobody supervises it",
                (EventKind.RECOVERY_CLAIM,),
            )
        if expired:
            return Decision(
                lease, LifecycleAction.RECOVER_STOP, "unsupervised and expired",
                (EventKind.RECOVERY_CLAIM,), ev.SWEEP_EXPIRED,
            )
        return Decision(lease, LifecycleAction.KEEP, "unsupervised, within its lease")
    # stopping / cleanup_incomplete: resume the teardown with its original reason.
    return Decision(
        lease, LifecycleAction.RECOVER_STOP, f"{state.value} with its supervisor gone",
        (EventKind.RECOVERY_CLAIM,),
    )
