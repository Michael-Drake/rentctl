"""The pure lease lifecycle (ADR-0016 §7, §8, §10).

Three layers:

* the exhaustive table test — every (state × event × actor kind) against §7;
* guard tests — the refusals that carry the safety rules;
* deterministic interleaving seams for acceptance scenarios #9, #10 and #11,
  where a simulated project lock applies one transition at a time and every
  ordering of the competing writers is enumerated.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

import pytest

from rentctl.core import events as ev
from rentctl.core.leases import (
    KNOWN_STATES,
    SCHEMA_SUPERVISED,
    SID_OWNER_SUPERVISOR,
    SID_OWNER_WORKLOAD,
    CleanupRecord,
    Lease,
    OwnershipIdentity,
    ProcessRef,
    RecoveryClaim,
    SupervisorRef,
    Survivor,
)
from rentctl.core.lifecycle import (
    CLEANUP_RETRY_BACKOFF,
    EXPIRY_NUDGE_GRACE,
    RECOVERY_CLAIM_LAPSE,
    REGISTRATION_TIMEOUT,
    REMOVED_ON_REACH,
    RULES,
    STARTUP_FAILED_CONSUME_GRACE,
    TABLE,
    TERMINAL,
    Actor,
    ActorKind,
    Event,
    EventKind,
    LifecycleAction,
    Membership,
    Observation,
    Refused,
    RefusalCode,
    StartWait,
    State,
    StopWait,
    claim_lapsed,
    cleanup_retry_delay,
    decide,
    generation_of,
    holds_port,
    may_remove,
    new_starting_lease,
    retry_due,
    start_wait_outcome,
    state_of,
    stop_wait_outcome,
    supervisor_owns,
    transition,
)

CDT = timezone(timedelta(hours=-5))
T0 = datetime(2026, 9, 28, 9, 0, tzinfo=CDT)
NOW = T0 + timedelta(hours=1)
EXPIRES = T0 + timedelta(hours=2)

GEN_A = "a" * 32
GEN_B = "b" * 32

SUP = ProcessRef(pid=123, start_time=1000.0)
SUP_B = ProcessRef(pid=223, start_time=1100.0)
CLI_P = ProcessRef(pid=777, start_time=2000.0)
CLI_P2 = ProcessRef(pid=778, start_time=2001.0)
REC_P = ProcessRef(pid=888, start_time=3000.0)
SWEEP_P = ProcessRef(pid=999, start_time=4000.0)

SURVIVOR = Survivor(pid=130, start_time=1002.0, name="node", status="running")
ERROR = {"code": "START_TIMEOUT", "message": "no answer", "log_tail": [], "phase": "readiness"}
PLAN = {"cmd": "npm run dev", "cwd": "/work/app", "port_env": "PORT"}


def fresh(generation: str = GEN_A, *, now: datetime = T0, expires: datetime = EXPIRES) -> Lease:
    return new_starting_lease(
        generation=generation,
        project="webapp",
        profile="default",
        runner="process",
        port=5183,
        session="sess",
        cwd="/work/app",
        spawn_cwd="/work/app",
        log="/logs/webapp.log",
        plan=PLAN,
        now=now,
        expires=expires,
    )


def registered(lease: Lease, sup: ProcessRef = SUP) -> Lease:
    return replace(lease, supervisor=SupervisorRef(sup.pid, sup.start_time, registered=True))


def launched(lease: Lease) -> Lease:
    sup = lease.supervisor
    return replace(
        lease,
        handle={
            "pid": sup.pid + 1, "pid_start_time": sup.start_time + 1,
            "sid": sup.pid, "sid_owner_start_time": sup.start_time, "sid_owner": SID_OWNER_SUPERVISOR,
        },
    )


def in_state(state: State, generation: str = GEN_A, **over) -> Lease:
    """A schema-2 lease in ``state`` with a registered, launched supervisor."""
    lease = launched(registered(fresh(generation)))
    lease = replace(lease, state=state.value, state_since=T0, **over)
    return lease


def legacy() -> Lease:
    return Lease(
        project="webapp",
        profile="default",
        runner="process",
        handle={"pid": 4242, "pid_start_time": 1784080000.12},
        port=5180,
        session="s",
        cwd="/work/app",
        created=T0,
        expires=EXPIRES,
        log="/l",
        watchdog_pid=4250,
        watchdog_pid_start_time=1784080001.5,
    )


def actor(kind: ActorKind, generation: str = GEN_A) -> Actor:
    process = {
        ActorKind.CLI: CLI_P,
        ActorKind.SUPERVISOR: SUP,
        ActorKind.RECONCILER: SWEEP_P,
        ActorKind.RECOVERER: REC_P,
    }[kind]
    return Actor(kind, generation, process)


def ok(result) -> Lease:
    assert isinstance(result, Lease), result
    return result


def refused(result, code: RefusalCode) -> None:
    assert isinstance(result, Refused), result
    assert result.code is code, result


# --- the state set ------------------------------------------------------------------------

def test_state_set_matches_the_lease_readers_known_states():
    assert {s.value for s in State} == KNOWN_STATES


def test_terminal_partition():
    assert TERMINAL == {State.STOPPED, State.EXITED, State.ABANDONED, State.STARTUP_FAILED}
    assert REMOVED_ON_REACH == {State.STOPPED, State.EXITED, State.ABANDONED}


def test_table_has_no_rows_out_of_terminal_states():
    assert not [r for r in TABLE if r.source in TERMINAL]


def test_refused_is_falsy():
    assert not Refused(RefusalCode.NO_RULE, "x")


# --- exhaustive (state × event × actor) ---------------------------------------------------------

def _canonical_event(kind: EventKind) -> Event:
    payload = {
        EventKind.SPAWNED: dict(supervisor=SupervisorRef(SUP.pid, SUP.start_time)),
        EventKind.LAUNCHED: dict(handle={"pid": 124, "pid_start_time": 1001.0}),
        EventKind.READY: dict(readiness="answered"),
        EventKind.RENEW: dict(expires=EXPIRES + timedelta(hours=1)),
        EventKind.STOP_REQUEST: dict(reason=ev.EXPLICIT, op="down"),
        EventKind.BEGIN_STOP: dict(reason=ev.SUPERVISOR_TERMINATED, op="down"),
        EventKind.STOP_VERIFIED: dict(error=ERROR),
        EventKind.STOP_INCOMPLETE: dict(survivors=(SURVIVOR,)),
        EventKind.SUPERVISOR_LOST: dict(membership=Membership.MEMBERS),
        EventKind.FOUND_EMPTY: dict(membership=Membership.EMPTY),
        EventKind.RECOVERY_CLAIM: dict(membership=Membership.MEMBERS, reason=ev.EXPLICIT, op="down"),
        EventKind.IDENTITY_AMBIGUOUS: dict(membership=Membership.AMBIGUOUS),
    }.get(kind, {})
    return Event(kind, NOW, **payload)


def _canonical_lease(state: State, kind: EventKind) -> Lease:
    """The lease each §7 row expects to find, so a refusal can only mean 'no row'."""
    if state is State.UNSUPERVISED and kind is EventKind.RENEW:
        return legacy()  # §14: only a legacy lease is renewed while unsupervised
    lease = in_state(state, recovery=RecoveryClaim(REC_P.pid, REC_P.start_time, NOW))
    if state is State.STARTING:
        if kind in (EventKind.SPAWNED, EventKind.REGISTER):
            lease = replace(lease, supervisor=None, handle={})
        elif kind is EventKind.LAUNCHED:
            lease = replace(lease, handle={})
        elif kind is EventKind.ABANDON:
            lease = replace(lease, supervisor=SupervisorRef(SUP.pid, SUP.start_time), handle={})
    return lease


CASES = list(itertools.product(State, EventKind, ActorKind))


@pytest.mark.parametrize(
    "state,kind,actor_kind", CASES, ids=[f"{s.value}-{k.value}-{a.value}" for s, k, a in CASES]
)
def test_transition_table_exhaustive(state, kind, actor_kind):
    lease = _canonical_lease(state, kind)
    who = actor(actor_kind, generation_of(lease))
    result = transition(lease, _canonical_event(kind), who)
    rule = RULES.get((state, kind))
    if rule is None:
        refused(result, RefusalCode.NO_RULE)
    elif actor_kind not in rule.actors:
        refused(result, RefusalCode.WRONG_ACTOR)
    else:
        after = ok(result)
        assert state_of(after) is rule.target
        assert generation_of(after) == generation_of(lease)  # no transition mints a generation


def test_every_rule_names_exactly_one_target_and_known_actors():
    assert len(RULES) == len(TABLE)
    for r in TABLE:
        assert r.actors and r.actors <= set(ActorKind)


def test_emitted_event_kinds_are_the_section_13_vocabulary():
    vocab = {ev.UP, ev.UP_FAILED, ev.DOWN, ev.STOP_REQUESTED, ev.CLEANUP_INCOMPLETE, ev.SUPERVISOR_LOST}
    assert {e for r in TABLE for e in r.emits} <= vocab
    # One `down` per terminal teardown: only rows reaching stopped/exited emit it.
    for r in TABLE:
        if ev.DOWN in r.emits:
            assert r.target in (State.STOPPED, State.EXITED)


def test_state_since_moves_only_on_a_state_change():
    lease = in_state(State.RUNNING)
    renewed = ok(transition(lease, _canonical_event(EventKind.RENEW), actor(ActorKind.CLI)))
    assert renewed.state_since == lease.state_since
    stopping = ok(transition(lease, _canonical_event(EventKind.BEGIN_STOP), actor(ActorKind.SUPERVISOR)))
    assert stopping.state_since == NOW


# --- startup (§6) ------------------------------------------------------------------------------

def test_new_starting_lease_is_schema2_starting_with_no_supervisor():
    lease = fresh()
    assert (lease.schema, lease.state, lease.generation) == (SCHEMA_SUPERVISED, "starting", GEN_A)
    assert lease.supervisor is None and lease.ownership() is None
    assert Lease.from_dict(lease.to_dict()) == lease


def test_startup_happy_path():
    lease = fresh()
    lease = ok(transition(lease, Event(EventKind.SPAWNED, T0, supervisor=SupervisorRef(123, 1000.0)),
                          Actor(ActorKind.CLI, GEN_A, CLI_P)))
    assert lease.supervisor == SupervisorRef(123, 1000.0, registered=False)
    lease = ok(transition(lease, Event(EventKind.REGISTER, T0), actor(ActorKind.SUPERVISOR)))
    assert lease.supervisor.registered
    # Registered, not yet launched: the session is already identifiable (§6 step 2).
    assert lease.ownership() == OwnershipIdentity(123, 1000.0, SID_OWNER_SUPERVISOR)
    lease = ok(transition(lease, Event(EventKind.LAUNCHED, T0, handle={"pid": 124, "pid_start_time": 1001.0}),
                          actor(ActorKind.SUPERVISOR)))
    # The identity is derived from the supervisor, not taken from the payload.
    assert lease.handle["sid"] == 123 and lease.handle["sid_owner"] == SID_OWNER_SUPERVISOR
    lease = ok(transition(lease, Event(EventKind.READY, T0, readiness="answered"), actor(ActorKind.SUPERVISOR)))
    assert state_of(lease) is State.RUNNING and lease.readiness == "answered"


def test_register_refusals():
    sup = actor(ActorKind.SUPERVISOR)
    base = fresh()
    refused(transition(base, Event(EventKind.REGISTER, T0), Actor(ActorKind.SUPERVISOR, GEN_A)),
            RefusalCode.MISSING_PAYLOAD)
    refused(transition(registered(base), Event(EventKind.REGISTER, T0), sup), RefusalCode.ALREADY_REGISTERED)
    spawned_other = replace(base, supervisor=SupervisorRef(SUP_B.pid, SUP_B.start_time))
    refused(transition(spawned_other, Event(EventKind.REGISTER, T0), sup), RefusalCode.NOT_OWNER)
    abandoned = replace(base, stop=_stop_for(base, ev.STARTUP_ABANDONED))
    refused(transition(abandoned, Event(EventKind.REGISTER, T0), sup), RefusalCode.STOP_PENDING)
    # A supervisor the CLI recorded may register itself.
    spawned = replace(base, supervisor=SupervisorRef(SUP.pid, SUP.start_time))
    assert ok(transition(spawned, Event(EventKind.REGISTER, T0), sup)).supervisor.registered


def test_spawned_refusals():
    cli = actor(ActorKind.CLI)
    refused(transition(fresh(), Event(EventKind.SPAWNED, T0), cli), RefusalCode.MISSING_PAYLOAD)
    refused(transition(registered(fresh()), _canonical_event(EventKind.SPAWNED), cli),
            RefusalCode.ALREADY_REGISTERED)


def test_supervisor_actions_need_the_registered_supervisor():
    unreg = replace(fresh(), supervisor=SupervisorRef(SUP.pid, SUP.start_time))
    refused(transition(unreg, _canonical_event(EventKind.LAUNCHED), actor(ActorKind.SUPERVISOR)),
            RefusalCode.NOT_REGISTERED)
    other = Actor(ActorKind.SUPERVISOR, GEN_A, SUP_B)
    refused(transition(registered(fresh()), _canonical_event(EventKind.LAUNCHED), other), RefusalCode.NOT_OWNER)


def test_launched_and_ready_payloads():
    sup = actor(ActorKind.SUPERVISOR)
    reg = registered(fresh())
    refused(transition(reg, Event(EventKind.LAUNCHED, T0), sup), RefusalCode.MISSING_PAYLOAD)
    refused(transition(reg, Event(EventKind.READY, T0), sup), RefusalCode.MISSING_PAYLOAD)


def test_ready_refused_when_a_stop_was_requested_during_startup():
    lease = launched(registered(fresh()))
    lease = replace(lease, stop=_stop_for(lease, ev.STARTUP_ABANDONED))
    refused(transition(lease, _canonical_event(EventKind.READY), actor(ActorKind.SUPERVISOR)),
            RefusalCode.STOP_PENDING)


def test_startup_failure_verified_is_startup_failed_with_error():
    lease = launched(registered(fresh()))
    after = ok(transition(lease, _canonical_event(EventKind.STOP_VERIFIED), actor(ActorKind.SUPERVISOR)))
    assert state_of(after) is State.STARTUP_FAILED and after.error == ERROR
    refused(transition(lease, Event(EventKind.STOP_VERIFIED, NOW), actor(ActorKind.SUPERVISOR)),
            RefusalCode.MISSING_PAYLOAD)


def test_startup_failure_with_survivors_is_cleanup_incomplete_phase_startup():
    lease = launched(registered(fresh()))
    after = ok(transition(lease, Event(EventKind.STOP_INCOMPLETE, NOW, survivors=(SURVIVOR,), error=ERROR),
                          actor(ActorKind.SUPERVISOR)))
    assert state_of(after) is State.CLEANUP_INCOMPLETE
    assert after.cleanup.phase == "startup" and after.cleanup.survivors == (SURVIVOR,)
    assert after.cleanup.attempts == 1 and after.error == ERROR


def test_stop_incomplete_needs_survivors():
    refused(transition(in_state(State.STOPPING), Event(EventKind.STOP_INCOMPLETE, NOW),
                       actor(ActorKind.SUPERVISOR)), RefusalCode.MISSING_PAYLOAD)


def test_starting_lease_without_supervisor_abandoned_after_timeout():
    """#8 (seam): the CLI died before spawning a supervisor; I1 says nothing ran."""
    lease = fresh(now=T0)
    rec = actor(ActorKind.RECONCILER)
    young = Observation(supervisor_alive=False, membership=Membership.EMPTY)
    assert decide(lease, young, T0 + timedelta(seconds=29)).action is LifecycleAction.KEEP
    refused(transition(lease, Event(EventKind.ABANDON, T0 + timedelta(seconds=29)), rec), RefusalCode.TOO_EARLY)
    later = T0 + REGISTRATION_TIMEOUT
    d = decide(lease, young, later)
    assert (d.action, d.events) == (LifecycleAction.CLEAN, (EventKind.ABANDON,))
    after = ok(transition(lease, Event(EventKind.ABANDON, later), rec))
    assert state_of(after) is State.ABANDONED
    assert RULES[(State.STARTING, EventKind.ABANDON)].emits == (ev.UP_FAILED,)
    assert may_remove(after, rec, later)


def test_supervisor_killed_before_register_is_abandoned():
    """#7 (seam clock): the recorded supervisor pid is dead and never registered —
    abandoned at once, without waiting out the timeout."""
    lease = replace(fresh(now=T0), supervisor=SupervisorRef(SUP.pid, SUP.start_time))
    at = T0 + timedelta(seconds=2)
    d = decide(lease, Observation(supervisor_alive=False, membership=Membership.EMPTY), at)
    assert (d.action, d.events) == (LifecycleAction.CLEAN, (EventKind.ABANDON,))
    after = ok(transition(lease, Event(EventKind.ABANDON, at, supervisor_alive=False), actor(ActorKind.RECONCILER)))
    assert state_of(after) is State.ABANDONED
    # Alive but unregistered and young: keep waiting.
    alive = Observation(supervisor_alive=True, membership=Membership.EMPTY)
    assert decide(lease, alive, at).action is LifecycleAction.KEEP
    refused(transition(lease, Event(EventKind.ABANDON, at, supervisor_alive=True), actor(ActorKind.RECONCILER)),
            RefusalCode.TOO_EARLY)


def test_registered_start_is_never_abandoned():
    lease = registered(fresh())
    refused(transition(lease, Event(EventKind.ABANDON, T0 + timedelta(hours=1)), actor(ActorKind.RECONCILER)),
            RefusalCode.ALREADY_REGISTERED)


# --- renewal ---------------------------------------------------------------------------------

def test_renew_rewrites_expires_only():
    lease = in_state(State.RUNNING)
    new = EXPIRES + timedelta(hours=1)
    after = ok(transition(lease, Event(EventKind.RENEW, NOW, expires=new), actor(ActorKind.CLI)))
    assert after == replace(lease, expires=new)


def test_renew_refusals():
    cli = actor(ActorKind.CLI)
    running = in_state(State.RUNNING)
    refused(transition(running, Event(EventKind.RENEW, NOW), cli), RefusalCode.MISSING_PAYLOAD)
    pending = replace(running, stop=_stop_for(running, ev.EXPLICIT))
    refused(transition(pending, _canonical_event(EventKind.RENEW), cli), RefusalCode.STOP_PENDING)
    refused(transition(in_state(State.UNSUPERVISED), _canonical_event(EventKind.RENEW), cli),
            RefusalCode.LEGACY_ONLY)
    refused(transition(in_state(State.STOPPING), _canonical_event(EventKind.RENEW), cli), RefusalCode.NO_RULE)


def test_legacy_lease_renewed_in_legacy_format():
    lease = legacy()
    cli = actor(ActorKind.CLI, generation_of(lease))
    after = ok(transition(lease, _canonical_event(EventKind.RENEW), cli))
    assert after.is_legacy and after.expires == EXPIRES + timedelta(hours=1)
    assert "schema" not in after.to_dict() and after.watchdog_pid == 4250


# --- legacy mapping (§14) ------------------------------------------------------------------------

def test_legacy_lease_reads_as_unsupervised_with_derived_generation():
    lease = legacy()
    assert state_of(lease) is State.UNSUPERVISED
    assert generation_of(lease) == "legacy-4242-1784080000.12"
    assert holds_port(lease)


def test_legacy_stop_request_upgrades_to_schema2_naming_the_same_session():
    lease = legacy()
    gen = generation_of(lease)
    after = ok(transition(lease, _canonical_event(EventKind.STOP_REQUEST), Actor(ActorKind.CLI, gen, CLI_P)))
    assert not after.is_legacy and after.generation == gen and state_of(after) is State.UNSUPERVISED
    assert after.ownership() == OwnershipIdentity(4242, 1784080000.12, SID_OWNER_WORKLOAD)
    assert after.stop.generation == gen
    # The upgraded file carries the poison: the 1.0.x watchdog now reads CORRUPT.
    assert after.to_dict()["watchdog_pid"] == "supervised"
    assert Lease.from_dict(after.to_dict()) == replace(after, watchdog_pid=None, watchdog_pid_start_time=None)


def test_legacy_upgrade_never_invents_a_session():
    lease = replace(legacy(), handle={})
    after = ok(transition(lease, _canonical_event(EventKind.STOP_REQUEST),
                          Actor(ActorKind.CLI, generation_of(lease), CLI_P)))
    assert not after.is_legacy and after.ownership() is None


def test_decision_carries_project():
    assert decide(in_state(State.RUNNING), ALIVE_M, NOW).project == "webapp"


def test_legacy_recovery_claim_upgrades_and_stops():
    lease = legacy()
    gen = generation_of(lease)
    rec = Actor(ActorKind.RECOVERER, gen, REC_P)
    ev_ = Event(EventKind.RECOVERY_CLAIM, EXPIRES, membership=Membership.MEMBERS, reason=ev.SWEEP_EXPIRED, op="sweep")
    after = ok(transition(lease, ev_, rec))
    assert state_of(after) is State.STOPPING and after.stop.reason == ev.SWEEP_EXPIRED
    assert after.recovery == RecoveryClaim(REC_P.pid, REC_P.start_time, EXPIRES)
    assert after.ownership().sid == 4242
    done = ok(transition(after, Event(EventKind.STOP_VERIFIED, EXPIRES), rec))
    assert state_of(done) is State.STOPPED and done.recovery is None


# --- stop requests (§8) -------------------------------------------------------------------------

def _stop_for(lease: Lease, reason: str):
    """The stop record a CLI request would write on ``lease``."""
    request = Event(EventKind.STOP_REQUEST, EXPIRES, reason=reason, op="down")
    return ok(transition(lease, request, actor(ActorKind.CLI, generation_of(lease)))).stop


def test_stop_request_records_requester_and_generation():
    lease = in_state(State.RUNNING)
    after = ok(transition(lease, Event(EventKind.STOP_REQUEST, NOW, reason=ev.SESSION_END,
                                       reason_source=ev.DECLARED, op="down"), actor(ActorKind.CLI)))
    assert state_of(after) is State.RUNNING  # the supervisor applies it, not the requester
    s = after.stop
    assert (s.generation, s.reason, s.reason_source, s.op, s.requested_at, s.requested_by) == (
        GEN_A, ev.SESSION_END, ev.DECLARED, "down", NOW, CLI_P
    )


def test_stop_request_needs_reason_op_and_requester():
    lease = in_state(State.RUNNING)
    refused(transition(lease, Event(EventKind.STOP_REQUEST, NOW, op="down"), actor(ActorKind.CLI)),
            RefusalCode.MISSING_PAYLOAD)
    refused(transition(lease, Event(EventKind.STOP_REQUEST, NOW, reason="explicit"), actor(ActorKind.CLI)),
            RefusalCode.MISSING_PAYLOAD)
    refused(transition(lease, _canonical_event(EventKind.STOP_REQUEST), Actor(ActorKind.CLI, GEN_A)),
            RefusalCode.MISSING_PAYLOAD)


def test_repeated_stop_requests_are_acknowledged_unchanged():
    lease = ok(transition(in_state(State.RUNNING), _canonical_event(EventKind.STOP_REQUEST), actor(ActorKind.CLI)))
    again = transition(lease, Event(EventKind.STOP_REQUEST, NOW, reason=ev.SESSION_END, op="down"),
                       Actor(ActorKind.CLI, GEN_A, CLI_P2))
    assert again is lease  # identity: write nothing, record nothing
    stopping = in_state(State.STOPPING)
    assert transition(stopping, _canonical_event(EventKind.STOP_REQUEST), actor(ActorKind.CLI)) is stopping


def test_stop_request_on_cleanup_incomplete_triggers_retry():
    lease = in_state(State.CLEANUP_INCOMPLETE,
                     cleanup=CleanupRecord(attempts=1, last_attempt=NOW, survivors=(SURVIVOR,)))
    assert not retry_due(lease, NOW + timedelta(seconds=1))
    after = ok(transition(lease, _canonical_event(EventKind.STOP_REQUEST), actor(ActorKind.CLI)))
    assert after.cleanup.retry_requested == NOW and state_of(after) is State.CLEANUP_INCOMPLETE
    assert retry_due(after, NOW + timedelta(seconds=1))
    # The retry's outcome clears the request.
    failed_again = ok(transition(after, Event(EventKind.STOP_INCOMPLETE, NOW, survivors=(SURVIVOR,)),
                                 actor(ActorKind.SUPERVISOR)))
    assert failed_again.cleanup.retry_requested is None and failed_again.cleanup.attempts == 2
    assert not retry_due(failed_again, NOW + timedelta(seconds=5))


def test_cleanup_retry_backoff_schedule():
    assert [cleanup_retry_delay(n).total_seconds() for n in (0, 1, 2, 3, 4, 10)] == [5, 5, 15, 60, 60, 60]
    assert CLEANUP_RETRY_BACKOFF[-1] == timedelta(seconds=60)
    ci = in_state(State.CLEANUP_INCOMPLETE, cleanup=CleanupRecord(attempts=2, last_attempt=NOW))
    assert not retry_due(ci, NOW + timedelta(seconds=14))
    assert retry_due(ci, NOW + timedelta(seconds=15))
    assert retry_due(in_state(State.CLEANUP_INCOMPLETE), NOW)  # never attempted
    assert not retry_due(in_state(State.RUNNING), NOW)


def test_begin_stop_applies_the_recorded_request_reason():
    lease = ok(transition(in_state(State.RUNNING), Event(EventKind.STOP_REQUEST, NOW, reason=ev.SESSION_END,
                                                          op="down"), actor(ActorKind.CLI)))
    after = ok(transition(lease, Event(EventKind.BEGIN_STOP, NOW, reason=ev.EXPIRY), actor(ActorKind.SUPERVISOR)))
    assert state_of(after) is State.STOPPING and after.stop.reason == ev.SESSION_END


def test_begin_stop_refusals():
    sup = actor(ActorKind.SUPERVISOR)
    running = in_state(State.RUNNING)
    refused(transition(running, Event(EventKind.BEGIN_STOP, NOW), sup), RefusalCode.NOTHING_TO_APPLY)
    refused(transition(running, Event(EventKind.BEGIN_STOP, NOW, reason=ev.EXPIRY), sup), RefusalCode.NOT_EXPIRED)
    after = ok(transition(running, Event(EventKind.BEGIN_STOP, EXPIRES, reason=ev.EXPIRY), sup))
    assert after.stop.reason == ev.EXPIRY and after.stop.requested_by == SUP


def test_session_emptied_refused_while_a_stop_is_recorded():
    lease = ok(transition(in_state(State.RUNNING), _canonical_event(EventKind.STOP_REQUEST), actor(ActorKind.CLI)))
    refused(transition(lease, Event(EventKind.SESSION_EMPTIED, NOW), actor(ActorKind.SUPERVISOR)),
            RefusalCode.STOP_PENDING)


def test_expiry_nudge_refused_before_expiry():
    refused(transition(in_state(State.RUNNING), Event(EventKind.STOP_REQUEST, NOW, reason=ev.EXPIRY, op="sweep"),
                       actor(ActorKind.RECONCILER)), RefusalCode.NOT_EXPIRED)


def test_stop_verified_counts_the_attempt_for_the_down_event():
    lease = in_state(State.CLEANUP_INCOMPLETE, cleanup=CleanupRecord(attempts=2, survivors=(SURVIVOR,)))
    after = ok(transition(lease, Event(EventKind.STOP_VERIFIED, NOW), actor(ActorKind.SUPERVISOR)))
    assert state_of(after) is State.STOPPED
    assert after.cleanup.attempts == 3 and after.cleanup.survivors == ()


# --- reconciler transitions and recovery (§10) -----------------------------------------------------

def test_reconciler_guards_on_supervisor_and_membership():
    rec = actor(ActorKind.RECONCILER)
    running = in_state(State.RUNNING)
    refused(transition(running, Event(EventKind.SUPERVISOR_LOST, NOW, supervisor_alive=True,
                                      membership=Membership.MEMBERS), rec), RefusalCode.SUPERVISOR_ALIVE)
    refused(transition(running, Event(EventKind.SUPERVISOR_LOST, NOW, membership=Membership.AMBIGUOUS), rec),
            RefusalCode.MEMBERSHIP)
    refused(transition(running, Event(EventKind.FOUND_EMPTY, NOW, membership=Membership.MEMBERS), rec),
            RefusalCode.MEMBERSHIP)
    refused(transition(running, Event(EventKind.FOUND_EMPTY, NOW), rec), RefusalCode.MEMBERSHIP)
    unreg = replace(running, supervisor=SupervisorRef(SUP.pid, SUP.start_time))
    refused(transition(unreg, _canonical_event(EventKind.SUPERVISOR_LOST), rec), RefusalCode.NOT_REGISTERED)
    start_unreg = replace(fresh(), supervisor=None)
    refused(transition(start_unreg, _canonical_event(EventKind.FOUND_EMPTY), rec), RefusalCode.NOT_REGISTERED)


def test_found_empty_during_startup_is_startup_failed_supervisor_lost():
    after = ok(transition(launched(registered(fresh())), _canonical_event(EventKind.FOUND_EMPTY),
                          actor(ActorKind.RECONCILER)))
    assert state_of(after) is State.STARTUP_FAILED
    assert after.error["phase"] == ev.PHASE_SUPERVISOR_LOST
    assert RULES[(State.STARTING, EventKind.FOUND_EMPTY)].emits == (ev.SUPERVISOR_LOST, ev.UP_FAILED)


def test_recovery_claim_guards():
    rec = actor(ActorKind.RECOVERER)
    unsup = in_state(State.UNSUPERVISED)
    claim = Event(EventKind.RECOVERY_CLAIM, NOW, membership=Membership.MEMBERS, reason=ev.EXPLICIT, op="down")
    refused(transition(unsup, claim, Actor(ActorKind.RECOVERER, GEN_A)), RefusalCode.MISSING_PAYLOAD)
    refused(transition(unsup, replace(claim, reason=None), rec), RefusalCode.MISSING_PAYLOAD)
    refused(transition(unsup, replace(claim, reason=ev.SWEEP_EXPIRED), rec), RefusalCode.NOT_EXPIRED)
    refused(transition(unsup, replace(claim, membership=Membership.AMBIGUOUS), rec), RefusalCode.MEMBERSHIP)
    refused(transition(unsup, replace(claim, supervisor_alive=True), rec), RefusalCode.SUPERVISOR_ALIVE)
    start_unreg = replace(fresh(), supervisor=None)
    refused(transition(start_unreg, claim, rec), RefusalCode.NOT_REGISTERED)


def test_recovery_claim_is_exclusive_until_it_lapses():
    held = in_state(State.STOPPING, recovery=RecoveryClaim(CLI_P.pid, CLI_P.start_time, NOW))
    rec = actor(ActorKind.RECOVERER)
    claim = Event(EventKind.RECOVERY_CLAIM, NOW + timedelta(seconds=10), membership=Membership.MEMBERS,
                  claim_holder_alive=True)
    refused(transition(held, claim, rec), RefusalCode.CLAIM_HELD)
    # Holder dead → lapsed at once; holder alive but 60 s old → lapsed too.
    assert ok(transition(held, replace(claim, claim_holder_alive=False), rec)).recovery.pid == REC_P.pid
    late = replace(claim, now=NOW + RECOVERY_CLAIM_LAPSE)
    assert ok(transition(held, late, rec)).recovery.since == NOW + RECOVERY_CLAIM_LAPSE
    # A stale recoverer whose claim was taken over cannot write an outcome.
    taken = ok(transition(held, replace(claim, claim_holder_alive=False), rec))
    stale = Actor(ActorKind.RECOVERER, GEN_A, CLI_P)
    refused(transition(taken, Event(EventKind.STOP_VERIFIED, NOW), stale), RefusalCode.NOT_CLAIM_HOLDER)
    refused(transition(in_state(State.STOPPING), Event(EventKind.STOP_VERIFIED, NOW), rec),
            RefusalCode.NOT_CLAIM_HOLDER)


def test_claim_lapsed():
    c = RecoveryClaim(1, 1.0, NOW)
    assert claim_lapsed(None, holder_alive=True, now=NOW)
    assert not claim_lapsed(c, holder_alive=True, now=NOW + timedelta(seconds=59))
    assert claim_lapsed(c, holder_alive=True, now=NOW + timedelta(seconds=60))
    assert claim_lapsed(c, holder_alive=False, now=NOW)


def test_recovering_a_dead_start_ends_startup_failed_or_cleanup_incomplete():
    lease = launched(registered(fresh()))
    rec = actor(ActorKind.RECOVERER)
    claimed = ok(transition(lease, _canonical_event(EventKind.RECOVERY_CLAIM), rec))
    assert state_of(claimed) is State.STARTING and claimed.recovery.pid == REC_P.pid
    failed = ok(transition(claimed, Event(EventKind.STOP_VERIFIED, NOW), rec))
    assert state_of(failed) is State.STARTUP_FAILED and failed.error["phase"] == ev.PHASE_SUPERVISOR_LOST
    stuck = ok(transition(claimed, Event(EventKind.STOP_INCOMPLETE, NOW, survivors=(SURVIVOR,)), rec))
    assert state_of(stuck) is State.CLEANUP_INCOMPLETE and stuck.cleanup.phase == "startup"
    assert stuck.recovery is None  # the next retry claims afresh


def test_owner_pid_reused_marks_identity_ambiguous():
    """#12 (seam): PID S is held by a different process — keep, flag, never signal."""
    lease = in_state(State.CLEANUP_INCOMPLETE, cleanup=CleanupRecord(attempts=1, survivors=(SURVIVOR,)))
    obs = Observation(supervisor_alive=False, membership=Membership.AMBIGUOUS)
    d = decide(lease, obs, NOW)
    assert (d.action, d.events) == (LifecycleAction.KEEP_AMBIGUOUS, (EventKind.IDENTITY_AMBIGUOUS,))
    rec = actor(ActorKind.RECONCILER)
    flagged = ok(transition(lease, Event(EventKind.IDENTITY_AMBIGUOUS, NOW, membership=Membership.AMBIGUOUS), rec))
    assert flagged.cleanup.identity_ambiguous and state_of(flagged) is State.CLEANUP_INCOMPLETE
    # Already flagged: acknowledged, nothing new to record; decide stops asking.
    assert transition(flagged, Event(EventKind.IDENTITY_AMBIGUOUS, NOW, membership=Membership.AMBIGUOUS),
                      rec) is flagged
    assert decide(flagged, obs, NOW).events == ()
    refused(transition(lease, Event(EventKind.IDENTITY_AMBIGUOUS, NOW, membership=Membership.MEMBERS), rec),
            RefusalCode.MEMBERSHIP)
    # No signal path is reachable from ambiguous: neither a claim nor a stop.
    for state in (State.RUNNING, State.UNSUPERVISED, State.STOPPING):
        d = decide(in_state(state), obs, EXPIRES + timedelta(hours=1))
        assert d.action is LifecycleAction.KEEP_AMBIGUOUS and d.events == ()


# --- decide(): the §10 table, row by row --------------------------------------------------------------

ALIVE_M = Observation(supervisor_alive=True, membership=Membership.MEMBERS)
DEAD_M = Observation(supervisor_alive=False, membership=Membership.MEMBERS)
DEAD_E = Observation(supervisor_alive=False, membership=Membership.EMPTY)
LATE = EXPIRES + EXPIRY_NUDGE_GRACE


@pytest.mark.parametrize(
    "state", [State.STARTING, State.RUNNING, State.STOPPING, State.CLEANUP_INCOMPLETE]
)
def test_decide_live_supervisor_keeps(state):
    assert decide(in_state(state), ALIVE_M, NOW).action is LifecycleAction.KEEP


def test_decide_hung_supervisor_past_expiry_is_nudged_not_signalled():
    lease = in_state(State.RUNNING)
    assert decide(lease, ALIVE_M, LATE - timedelta(seconds=1)).action is LifecycleAction.KEEP
    d = decide(lease, ALIVE_M, LATE)
    assert (d.action, d.events, d.stop_reason) == (
        LifecycleAction.NUDGE_EXPIRY, (EventKind.STOP_REQUEST,), ev.EXPIRY
    )
    nudged = ok(transition(lease, Event(EventKind.STOP_REQUEST, LATE, reason=d.stop_reason, op="sweep"),
                           actor(ActorKind.RECONCILER)))
    assert decide(nudged, ALIVE_M, LATE).action is LifecycleAction.KEEP  # asked once


@pytest.mark.parametrize(
    "state,target",
    [
        (State.STARTING, State.STARTUP_FAILED),
        (State.RUNNING, State.EXITED),
        (State.UNSUPERVISED, State.EXITED),
        (State.STOPPING, State.STOPPED),
        (State.CLEANUP_INCOMPLETE, State.STOPPED),
    ],
)
def test_decide_dead_supervisor_empty_session_cleans(state, target):
    lease = in_state(state)
    d = decide(lease, DEAD_E, NOW)
    assert (d.action, d.events) == (LifecycleAction.CLEAN, (EventKind.FOUND_EMPTY,))
    after = ok(transition(lease, Event(EventKind.FOUND_EMPTY, NOW, membership=Membership.EMPTY),
                          actor(ActorKind.RECONCILER)))
    assert state_of(after) is target


def test_decide_legacy_dead_leader_empty_session_cleans():
    lease = legacy()
    d = decide(lease, DEAD_E, NOW)
    assert d.action is LifecycleAction.CLEAN
    after = ok(transition(lease, Event(EventKind.FOUND_EMPTY, NOW, membership=Membership.EMPTY),
                          actor(ActorKind.RECONCILER, generation_of(lease))))
    assert state_of(after) is State.EXITED


def test_decide_dead_start_with_members_is_recover_stop():
    d = decide(in_state(State.STARTING), DEAD_M, NOW)
    assert (d.action, d.events) == (LifecycleAction.RECOVER_STOP, (EventKind.RECOVERY_CLAIM,))


def test_decide_running_dead_supervisor_is_unsupervised_not_killed():
    """#7 recovery half (seam): the server keeps serving its session."""
    lease = in_state(State.RUNNING)
    d = decide(lease, DEAD_M, NOW)
    assert (d.action, d.events) == (LifecycleAction.MARK_UNSUPERVISED, (EventKind.SUPERVISOR_LOST,))
    after = ok(transition(lease, Event(EventKind.SUPERVISOR_LOST, NOW, membership=Membership.MEMBERS),
                          actor(ActorKind.RECONCILER)))
    assert state_of(after) is State.UNSUPERVISED and holds_port(after)
    assert decide(after, DEAD_M, NOW).action is LifecycleAction.KEEP


def test_decide_running_dead_supervisor_expired_is_lost_then_recovered():
    lease = in_state(State.RUNNING)
    d = decide(lease, DEAD_M, EXPIRES)
    assert d.action is LifecycleAction.RECOVER_STOP
    assert d.events == (EventKind.SUPERVISOR_LOST, EventKind.RECOVERY_CLAIM)
    assert d.stop_reason == ev.SWEEP_EXPIRED
    lease = ok(transition(lease, Event(EventKind.SUPERVISOR_LOST, EXPIRES, membership=Membership.MEMBERS),
                          actor(ActorKind.RECONCILER)))
    lease = ok(transition(lease, Event(EventKind.RECOVERY_CLAIM, EXPIRES, membership=Membership.MEMBERS,
                                       reason=d.stop_reason, op="sweep"), actor(ActorKind.RECOVERER)))
    assert state_of(lease) is State.STOPPING and lease.stop.reason == ev.SWEEP_EXPIRED


@pytest.mark.parametrize("make", [lambda: in_state(State.UNSUPERVISED), legacy], ids=["unsupervised", "legacy"])
def test_decide_unsupervised_expiry(make):
    lease = make()
    assert decide(lease, DEAD_M, EXPIRES - timedelta(seconds=1)).action is LifecycleAction.KEEP
    d = decide(lease, DEAD_M, EXPIRES)
    assert (d.action, d.events, d.stop_reason) == (
        LifecycleAction.RECOVER_STOP, (EventKind.RECOVERY_CLAIM,), ev.SWEEP_EXPIRED
    )


def test_decide_unsupervised_with_recorded_stop_is_recovered():
    """R0: SessionEnd recorded a stop on an unsupervised lease; the next reconciler carries it out."""
    lease = ok(transition(in_state(State.UNSUPERVISED), _canonical_event(EventKind.STOP_REQUEST),
                          actor(ActorKind.CLI)))
    d = decide(lease, DEAD_M, NOW)
    assert d.action is LifecycleAction.RECOVER_STOP and d.stop_reason is None
    claimed = ok(transition(lease, Event(EventKind.RECOVERY_CLAIM, NOW, membership=Membership.MEMBERS),
                            actor(ActorKind.RECOVERER)))
    assert claimed.stop.reason == ev.EXPLICIT  # the original reason, not a new one


@pytest.mark.parametrize("state", [State.STOPPING, State.CLEANUP_INCOMPLETE])
def test_decide_dead_supervisor_mid_teardown_resumes_with_original_reason(state):
    lease = in_state(state)
    lease = replace(lease, stop=_stop_for(in_state(State.RUNNING), ev.SESSION_END))
    d = decide(lease, DEAD_M, NOW)
    assert (d.action, d.events) == (LifecycleAction.RECOVER_STOP, (EventKind.RECOVERY_CLAIM,))
    claimed = ok(transition(lease, Event(EventKind.RECOVERY_CLAIM, NOW, membership=Membership.MEMBERS),
                            actor(ActorKind.RECOVERER)))
    assert claimed.stop.reason == ev.SESSION_END and state_of(claimed) is state


def test_decide_respects_a_live_recovery_claim():
    lease = in_state(State.STOPPING, recovery=RecoveryClaim(CLI_P.pid, CLI_P.start_time, NOW))
    live = Observation(supervisor_alive=False, membership=Membership.MEMBERS, claim_holder_alive=True)
    assert decide(lease, live, NOW + timedelta(seconds=5)).action is LifecycleAction.KEEP
    assert decide(lease, live, NOW + RECOVERY_CLAIM_LAPSE).action is LifecycleAction.RECOVER_STOP
    assert decide(lease, DEAD_M, NOW).action is LifecycleAction.RECOVER_STOP


@pytest.mark.parametrize("state", sorted(REMOVED_ON_REACH, key=lambda s: s.value))
def test_decide_removes_left_behind_terminal_records(state):
    assert decide(in_state(state), DEAD_E, NOW).action is LifecycleAction.REMOVE


def test_decide_startup_failed_waits_for_its_waiter_then_cleans():
    lease = in_state(State.STARTUP_FAILED, error=ERROR)
    assert decide(lease, DEAD_E, T0 + timedelta(seconds=5)).action is LifecycleAction.KEEP
    assert decide(lease, ALIVE_M, T0 + STARTUP_FAILED_CONSUME_GRACE).action is LifecycleAction.KEEP
    assert decide(lease, DEAD_M, T0 + STARTUP_FAILED_CONSUME_GRACE).action is LifecycleAction.KEEP
    assert decide(lease, DEAD_E, T0 + STARTUP_FAILED_CONSUME_GRACE).action is LifecycleAction.REMOVE


# --- removal and the port draw ----------------------------------------------------------------------

def test_may_remove_rules():
    now = T0 + timedelta(seconds=1)
    for state in REMOVED_ON_REACH:
        assert may_remove(in_state(state), actor(ActorKind.SUPERVISOR), now)
    failed = in_state(State.STARTUP_FAILED, error=ERROR)
    assert may_remove(failed, actor(ActorKind.CLI), now)                    # its waiter
    assert not may_remove(failed, actor(ActorKind.CLI, GEN_B), now)         # another generation's waiter
    assert not may_remove(failed, actor(ActorKind.RECONCILER), now)         # too soon for a reconciler
    assert may_remove(failed, actor(ActorKind.RECONCILER), T0 + STARTUP_FAILED_CONSUME_GRACE)
    assert not may_remove(failed, actor(ActorKind.SUPERVISOR), T0 + timedelta(days=1))
    for state in NON_TERMINAL_STATES:
        assert not may_remove(in_state(state), actor(ActorKind.RECONCILER), T0 + timedelta(days=1))


NON_TERMINAL_STATES = [s for s in State if s not in TERMINAL]


def test_holds_port():
    assert all(holds_port(in_state(s)) for s in NON_TERMINAL_STATES)
    assert not any(holds_port(in_state(s)) for s in TERMINAL)


# --- waiters (§8) ------------------------------------------------------------------------------------

def test_stop_wait_outcomes():
    assert stop_wait_outcome(None, GEN_A) is StopWait.STOPPED
    assert stop_wait_outcome(in_state(State.STOPPED), GEN_A) is StopWait.STOPPED
    assert stop_wait_outcome(in_state(State.CLEANUP_INCOMPLETE), GEN_A) is StopWait.CLEANUP_INCOMPLETE
    assert stop_wait_outcome(in_state(State.STOPPING), GEN_A) is StopWait.PENDING
    assert stop_wait_outcome(in_state(State.RUNNING), GEN_A) is StopWait.PENDING
    # A stop requested during startup ends in startup_failed: verified empty.
    assert stop_wait_outcome(in_state(State.STARTUP_FAILED, error=ERROR), GEN_A) is StopWait.STOPPED


def test_start_wait_outcomes():
    assert start_wait_outcome(None, GEN_A) is StartWait.GONE
    assert start_wait_outcome(in_state(State.STARTING), GEN_A) is StartWait.PENDING
    assert start_wait_outcome(in_state(State.RUNNING), GEN_A) is StartWait.RUNNING
    assert start_wait_outcome(in_state(State.STARTUP_FAILED), GEN_A) is StartWait.FAILED
    assert start_wait_outcome(in_state(State.CLEANUP_INCOMPLETE), GEN_A) is StartWait.CLEANUP_INCOMPLETE
    assert start_wait_outcome(in_state(State.ABANDONED), GEN_A) is StartWait.GONE


def test_waiter_distinguishes_generations():
    """#11: a waiter for gen A never reads gen B's state as its own."""
    b = in_state(State.CLEANUP_INCOMPLETE, GEN_B)
    assert stop_wait_outcome(b, GEN_A) is StopWait.STOPPED
    assert start_wait_outcome(in_state(State.RUNNING, GEN_B), GEN_A) is StartWait.GONE
    assert start_wait_outcome(in_state(State.STARTUP_FAILED, GEN_B), GEN_A) is StartWait.GONE


# --- the interleaving seam ---------------------------------------------------------------------------------

@dataclass
class LockedStore:
    """One lease file under a simulated project lock L.

    ``apply`` is one critical section: read, transition, write — and, for a
    terminal state its actor reaches, append the event then unlink (§7). The
    tests enumerate every order of these sections, which is every interleaving
    the real flock admits, since no section overlaps another.
    """

    lease: Lease | None
    history: list[tuple[State, State, EventKind, str]] = field(default_factory=list)
    emitted: list[tuple[str, str]] = field(default_factory=list)  # (§13 kind, generation)

    def apply(self, event: Event, who: Actor):
        if self.lease is None:
            return None  # lease gone: a supervisor exits (lease-lost), a waiter reads "stopped"
        before = self.lease
        result = transition(before, event, who)
        if isinstance(result, Refused) or result is before:
            return result
        rule = RULES[(state_of(before), event.kind)]
        self.history.append((state_of(before), state_of(result), event.kind, generation_of(result)))
        self.emitted.extend((kind, generation_of(result)) for kind in rule.emits)
        self.lease = None if (
            state_of(result) in REMOVED_ON_REACH and may_remove(result, who, event.now)
        ) else result
        return result

    def stops_of(self, generation: str) -> list:
        return [h for h in self.history if h[1] is State.STOPPING and h[3] == generation]


def running_store(generation: str = GEN_A, sup: ProcessRef = SUP) -> LockedStore:
    lease = in_state(State.RUNNING, generation)
    lease = replace(lease, supervisor=SupervisorRef(sup.pid, sup.start_time, registered=True))
    return LockedStore(lease)


def test_stop_vs_expiry_all_interleavings():
    """#9: explicit stops, a session-end stop, a reconciler's expiry nudge, the
    supervisor's own expiry timer and its wake race in every order. Exactly one
    `stopping` transition, exactly one `down`, and the first reason written is
    the teardown's reason."""
    at = LATE  # past expiry + grace, so every writer is entitled to act

    def request(reason, proc, kind=ActorKind.CLI):
        return (reason, lambda s: s.apply(Event(EventKind.STOP_REQUEST, at, reason=reason, op="down"),
                                          Actor(kind, GEN_A, proc)))

    writers = [
        request(ev.EXPLICIT, CLI_P),
        request(ev.SESSION_END, CLI_P2),
        request(ev.EXPIRY, SWEEP_P, ActorKind.RECONCILER),
        (ev.EXPIRY, lambda s: s.apply(Event(EventKind.BEGIN_STOP, at, reason=ev.EXPIRY),
                                      Actor(ActorKind.SUPERVISOR, GEN_A, SUP))),
    ]
    wake = (None, lambda s: s.apply(Event(EventKind.BEGIN_STOP, at), Actor(ActorKind.SUPERVISOR, GEN_A, SUP)))
    finish = (None, lambda s: s.apply(Event(EventKind.STOP_VERIFIED, at), Actor(ActorKind.SUPERVISOR, GEN_A, SUP)))
    repeat = request(ev.EXPLICIT, CLI_P)  # a second `down` from the same caller

    steps = writers + [wake, finish, repeat]
    orders = 0
    for order in itertools.permutations(range(len(steps))):
        orders += 1
        store = running_store()
        recorded_reason = None
        for i in order:
            steps[i][1](store)
            if store.lease is not None and store.lease.stop is not None and recorded_reason is None:
                recorded_reason = store.lease.stop.reason
        # The supervisor keeps going until its own work is done.
        if store.lease is not None and state_of(store.lease) is State.RUNNING:
            wake[1](store)
        if store.lease is not None:
            finish[1](store)

        first_writer = min((i for i in range(len(steps)) if steps[i] in writers or steps[i] is repeat),
                           key=order.index)
        assert recorded_reason == steps[first_writer][0], order
        assert len(store.stops_of(GEN_A)) == 1, order
        assert store.emitted.count((ev.DOWN, GEN_A)) == 1, order
        assert store.emitted.count((ev.STOP_REQUESTED, GEN_A)) <= 1, order
        assert store.lease is None, order
    assert orders == 5040


def test_renew_vs_expiry_interleavings():
    """#10: renew and expiry race; a stale expiry never stops the renewed or the
    replacement generation."""
    expired_at = EXPIRES + timedelta(seconds=1)
    renewed_to = EXPIRES + timedelta(hours=2)
    late = EXPIRES + EXPIRY_NUDGE_GRACE

    def renew(s):
        return s.apply(Event(EventKind.RENEW, expired_at, expires=renewed_to), Actor(ActorKind.CLI, GEN_A, CLI_P))

    def timer(s):
        return s.apply(Event(EventKind.BEGIN_STOP, expired_at, reason=ev.EXPIRY), Actor(ActorKind.SUPERVISOR, GEN_A, SUP))

    def nudge(s):
        return s.apply(Event(EventKind.STOP_REQUEST, late, reason=ev.EXPIRY, op="sweep"),
                       Actor(ActorKind.RECONCILER, GEN_A, SWEEP_P))

    steps = {"renew": renew, "timer": timer, "nudge": nudge}
    for order in itertools.permutations(steps):
        store = running_store()
        results = {name: steps[name](store) for name in order}
        if order[0] == "renew":
            # Renew first: expiry moved out, every expiry path is refused, it keeps running.
            assert state_of(store.lease) is State.RUNNING and store.lease.stop is None, order
            assert store.lease.expires == renewed_to
            refused(results["timer"], RefusalCode.NOT_EXPIRED)
            refused(results["nudge"], RefusalCode.NOT_EXPIRED)
            assert store.stops_of(GEN_A) == []
            continue
        # Expiry first: `up` finds a stop decided, so it waits for the teardown...
        assert isinstance(results["renew"], Refused), order
        assert results["renew"].code in (RefusalCode.STOP_PENDING, RefusalCode.NO_RULE)
        if state_of(store.lease) is State.RUNNING:
            # Only the nudge has written so far: the supervisor's wake applies it.
            ok(store.apply(Event(EventKind.BEGIN_STOP, late), Actor(ActorKind.SUPERVISOR, GEN_A, SUP)))
        store.apply(Event(EventKind.STOP_VERIFIED, late), Actor(ActorKind.SUPERVISOR, GEN_A, SUP))
        assert store.lease is None and len(store.stops_of(GEN_A)) == 1
        # ...then starts a fresh generation.
        store.lease = _started(GEN_B, late)
        # Every stale gen-A writer now finds gen B — and is refused.
        for name, step in steps.items():
            refused(step(store), RefusalCode.STALE_GENERATION)
        assert state_of(store.lease) is State.RUNNING and store.lease.stop is None
        assert store.stops_of(GEN_B) == []


def _started(generation: str, now: datetime) -> Lease:
    lease = fresh(generation, now=now, expires=now + timedelta(hours=2))
    sup = Actor(ActorKind.SUPERVISOR, generation, SUP_B)
    lease = ok(transition(lease, Event(EventKind.SPAWNED, now, supervisor=SupervisorRef(SUP_B.pid, SUP_B.start_time)),
                          Actor(ActorKind.CLI, generation, CLI_P)))
    lease = ok(transition(lease, Event(EventKind.REGISTER, now), sup))
    lease = ok(transition(lease, Event(EventKind.LAUNCHED, now, handle={"pid": 224, "pid_start_time": 1101.0}), sup))
    return ok(transition(lease, Event(EventKind.READY, now, readiness="answered"), sup))


def test_stale_stop_request_ignored():
    """#11: a request stamped with gen A, reaching L after gen B replaced A, records nothing."""
    b = _started(GEN_B, NOW)
    for kind in (ActorKind.CLI, ActorKind.RECONCILER):
        refused(transition(b, _canonical_event(EventKind.STOP_REQUEST), Actor(kind, GEN_A, CLI_P)),
                RefusalCode.STALE_GENERATION)
    refused(transition(b, _canonical_event(EventKind.RENEW), Actor(ActorKind.CLI, GEN_A, CLI_P)),
            RefusalCode.STALE_GENERATION)
    # An actor that names no generation at all is not trusted either.
    refused(transition(b, _canonical_event(EventKind.STOP_REQUEST), Actor(ActorKind.CLI, None, CLI_P)),
            RefusalCode.STALE_GENERATION)


@pytest.mark.parametrize("kind", list(EventKind), ids=lambda k: k.value)
def test_stale_supervisor_generation_refused(kind):
    """#11: a supervisor started for gen A can change nothing on gen B, whatever it tries."""
    b = _started(GEN_B, NOW)
    stale = Actor(ActorKind.SUPERVISOR, GEN_A, SUP)
    result = transition(b, _canonical_event(kind), stale)
    assert isinstance(result, Refused)
    assert result.code in (RefusalCode.STALE_GENERATION, RefusalCode.NO_RULE, RefusalCode.WRONG_ACTOR)
    assert not supervisor_owns(b, GEN_A)


def test_supervisor_owns():
    b = _started(GEN_B, NOW)
    assert supervisor_owns(b, GEN_B)
    assert not supervisor_owns(None, GEN_B)          # lease-lost: stop own session, exit
    assert not supervisor_owns(legacy(), GEN_B)
