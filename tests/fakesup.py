"""A fake supervision seam for Service tests (ADR-0016 plan steps 5–7).

The service no longer starts or stops anything itself: it writes lease
transitions, spawns and wakes supervisors, and waits for what they write. To
test its decisions fast and deterministically, :class:`FakeSupervision` stands
in for ``service.OsSupervision`` end to end:

* ``spawn`` creates a fake supervisor that, when ticked, runs the §6 protocol
  through the REAL ``lifecycle.transition`` under the REAL project lock —
  register, launch (a fake leader in a fake session), readiness, ``running`` —
  and later applies stop requests, expiry and a workload exiting on its own.
* the process table is ``fakeproc.FakeProcessTable``: no real process is ever
  scanned or signalled. Supervisor pids sit far above macOS's 99999 wrap.
* ``sleep`` advances a fake monotonic clock and ticks every supervisor, which
  is how a lock-free waiter in the service sees the outcome appear. A tick
  under a held project lock would deadlock on the flock — which is Rule 2 of
  §9 enforced by the harness itself.

Everything a fake supervisor records goes through the same
``supervision.record_*`` helpers the real one uses, so event shapes agree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from fakeproc import FakeClock, FakeProcessTable
from rentctl.core import events as ev
from rentctl.core import procutil
from rentctl.core.errors import START_TIMEOUT, DevctlError
from rentctl.core.events import EventLog
from rentctl.core.leases import Lease, ProcessRef, SupervisorRef
from rentctl.core.lifecycle import (
    Actor,
    ActorKind,
    Event,
    EventKind,
    State,
    retry_due,
    state_of,
    supervisor_owns,
    transition,
)
from rentctl.core.locking import project_lock
from rentctl.core.models import SID_OWNER_SUPERVISOR, SID_OWNER_WORKLOAD, WorkloadIdentity
from rentctl.core.paths import DevctlPaths, project_from_key
from rentctl.core.procutil import Membership
from rentctl.core.supervision import (
    record_cleanup_incomplete,
    record_down,
    record_up_failed,
    recover_lease,
)
from rentctl.core.workload import RECOVERY, stop_workload

SUP_BASE = 8_000_000

# readiness outcomes a test can ask for
READY_OUTCOMES = ("answered", "listening", "unknown")


@dataclass
class FakeSup:
    world: "FakeSupervision"
    key: str
    generation: str
    pid: int
    start: float
    alive: bool = True
    phase: str = "spawned"          # spawned | running | holding
    leader: int | None = None
    last: Lease | None = None

    @property
    def ref(self) -> ProcessRef:
        return ProcessRef(self.pid, self.start)

    @property
    def actor(self) -> Actor:
        return Actor(ActorKind.SUPERVISOR, self.generation, self.ref)

    @property
    def path(self) -> Path:
        return self.world.paths.lease_file(self.key)

    def now(self) -> datetime:
        return self.world.clock()

    # --- one pass --------------------------------------------------------------

    def step(self) -> None:
        with project_lock(self.world.paths.lock_file(project_from_key(self.key))):
            try:
                lease = Lease.read_if_exists(self.path)
            except DevctlError:
                lease = None
            if not supervisor_owns(lease, self.generation):
                self._lost()
                return
            self.last = lease
            if self.phase == "spawned":
                self._start(lease)
            elif self.phase == "running":
                self._run(lease)
            else:
                self._hold(lease)

    def _commit(self, lease: Lease, event: Event) -> Lease | None:
        nxt = transition(lease, event, self.actor)
        if not isinstance(nxt, Lease):
            return None
        if nxt is not lease:
            nxt.write(self.path)
        self.last = nxt
        return nxt

    def _start(self, lease: Lease) -> None:
        w = self.world
        if state_of(lease) is not State.STARTING:
            self._exit()
            return
        reg = self._commit(lease, Event(EventKind.REGISTER, self.now()))
        if reg is None:
            self._exit()  # e.g. a stop was requested first: launch nothing (I1)
            return
        plan = reg.plan or {}
        self.leader = w._next_leader
        w._next_leader += 1
        if w.readiness != "dies":
            w.table.add(self.leader, self.pid, self.start + 1, name="sh",
                        ignores_term=w.stubborn, unkillable=w.stubborn)
        w.started.append(self.leader)
        w.start_cwds.append(plan.get("cwd"))
        log = Path(plan.get("log") or reg.log)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(f"started {plan.get('cmd')} on {reg.port}\nline2\n")
        launched = self._commit(
            reg, Event(EventKind.LAUNCHED, self.now(),
                       handle={"pid": self.leader, "pid_start_time": self.start + 1}),
        )
        assert launched is not None
        failure: tuple[str, str, str] | None = None
        if w.readiness == "dies":
            failure = (START_TIMEOUT, ev.PHASE_READINESS,
                       f"{launched.project!r} exited during startup on port {launched.port}")
        elif w.readiness not in READY_OUTCOMES:
            failure = (w.readiness_code, w.readiness_phase,
                       f"{launched.project!r} did not answer on port {launched.port}")
        elif launched.stop is not None:
            failure = (START_TIMEOUT, ev.PHASE_READINESS,
                       f"startup abandoned: a stop was requested ({launched.stop.reason})")
        if failure is not None:
            self._teardown(launched, error={
                "code": failure[0], "phase": failure[1], "message": failure[2],
                "log_tail": log.read_text().splitlines(),
            })
            return
        running = self._commit(launched, Event(EventKind.READY, self.now(), readiness=w.readiness))
        assert running is not None
        w.log.record_up(
            running.project, profile=running.profile, port=running.port, pid=self.leader,
            session=running.session, cwd=running.cwd, lease_expires=running.expires.isoformat(),
            already_running=False,
            spawn_cwd=running.spawn_cwd if running.spawn_cwd != running.cwd else None,
            generation=running.generation, supervisor_pid=self.pid, readiness=running.readiness,
        )
        self.phase = "running"

    def _run(self, lease: Lease) -> None:
        now = self.now()
        if lease.stop is not None or now >= lease.expires:
            begun = self._commit(
                lease,
                Event(EventKind.BEGIN_STOP, now, reason=None if lease.stop else ev.EXPIRY,
                      reason_source=ev.DECLARED, op=ev.ACTOR_SUPERVISOR),
            )
            self._teardown(begun or lease)
            return
        if self._scan() is Membership.EMPTY:
            done = transition(lease, Event(EventKind.SESSION_EMPTIED, now), self.actor)
            if isinstance(done, Lease):
                record_down(self.world.log, done, actor=ev.ACTOR_SUPERVISOR, outcome=None,
                            reason=ev.PROCESS_GONE, reason_source=ev.DECLARED, op=ev.ACTOR_SUPERVISOR)
                self.path.unlink(missing_ok=True)
                self._exit()

    def _hold(self, lease: Lease) -> None:
        if retry_due(lease, self.now()):
            self._teardown(lease)

    def _teardown(self, lease: Lease, error: dict[str, Any] | None = None) -> None:
        """Stop the fake session and record the outcome, as the supervisor does."""
        w = self.world
        startup = state_of(lease) is State.STARTING
        outcome = self._stop()
        stop = lease.stop
        reason = stop.reason if stop else ev.SUPERVISOR_TERMINATED
        source = stop.reason_source if stop else ev.DECLARED
        op = stop.op if stop else ev.ACTOR_SUPERVISOR
        if startup and error is None:
            error = {"code": START_TIMEOUT, "phase": ev.PHASE_READINESS,
                     "message": f"startup abandoned: a stop was requested ({reason})", "log_tail": []}
        kind = EventKind.STOP_VERIFIED if outcome.verified else EventKind.STOP_INCOMPLETE
        membership = Membership.AMBIGUOUS if outcome.identity_ambiguous else None
        nxt = transition(lease, Event(kind, self.now(), survivors=outcome.survivors, error=error,
                                      membership=membership), self.actor)
        assert isinstance(nxt, Lease), nxt
        if startup:
            record_up_failed(w.log, nxt, error=nxt.error or {},
                             cleanup=ev.CLEANUP_VERIFIED if outcome.verified else ev.CLEANUP_INCOMPLETE_VALUE)
        if state_of(nxt) is State.STOPPED:
            record_down(w.log, nxt, actor=ev.ACTOR_SUPERVISOR, outcome=outcome, reason=reason,
                        reason_source=source, op=op)
            self.path.unlink(missing_ok=True)
            self._exit()
            return
        nxt.write(self.path)
        self.last = nxt
        if outcome.verified:  # startup_failed: kept for its waiter
            self._exit()
            return
        record_cleanup_incomplete(w.log, nxt, outcome, reason=reason, reason_source=source,
                                  op=ev.UP if startup else op)
        self.phase = "holding"

    def _stop(self):
        w = self.world
        clock = FakeClock()
        outcome = stop_workload(
            self.identity(), RECOVERY, term_grace_s=0.01, kill_grace_s=0.01,
            table=w.table, clock=clock, sleep=clock.sleep,
        )
        if self.leader is not None and self.leader not in w.stopped:
            w.stopped.append(self.leader)
        return outcome

    def _lost(self) -> None:
        """§7 last row: stop our own session, record lease-lost, exit."""
        outcome = self._stop()
        if self.last is not None:
            record_down(self.world.log, self.last, actor=ev.ACTOR_SUPERVISOR, outcome=outcome,
                        reason=ev.LEASE_LOST, reason_source=ev.DECLARED, op=ev.ACTOR_SUPERVISOR)
        self._exit()

    def identity(self) -> WorkloadIdentity:
        return WorkloadIdentity(self.pid, self.start, SID_OWNER_SUPERVISOR)

    def _scan(self) -> Membership:
        ident = self.identity()
        return procutil.session_scan(ident.sid, ident.owner_start, ident.exclude,
                                     table=self.world.table).state

    def _exit(self) -> None:
        self.alive = False
        self.world.table.kill_now(self.pid)


@dataclass
class FakeSupervision:
    """``service.OsSupervision``, faked. See the module docstring."""

    paths: DevctlPaths
    clock: Any
    readiness: str = "answered"
    readiness_code: str = START_TIMEOUT
    readiness_phase: str = ev.PHASE_READINESS
    stubborn: bool = False          # new workloads survive TERM and KILL
    hung: bool = False              # live supervisors that never act
    table: FakeProcessTable = field(default_factory=FakeProcessTable)
    sups: dict[int, FakeSup] = field(default_factory=dict)
    recoveries: list[tuple] = field(default_factory=list)
    recovered: list[tuple] = field(default_factory=list)
    started: list[int] = field(default_factory=list)
    stopped: list[int] = field(default_factory=list)
    start_cwds: list[Any] = field(default_factory=list)
    woken: list[int] = field(default_factory=list)
    spawn_error: DevctlError | None = None
    mono: float = 0.0
    _next_sup: int = SUP_BASE
    _next_leader: int = 1000

    def __post_init__(self) -> None:
        self.log = EventLog(self.paths.events_file, now_fn=self.clock)

    # --- the seam --------------------------------------------------------------------

    def spawn(self, key: str, generation: str, *, paths: DevctlPaths) -> SupervisorRef:
        if self.spawn_error is not None:
            raise self.spawn_error
        pid = self._new_pid()
        self.table.add(pid, pid, float(pid), name="python")  # S: the session leader
        self.sups[pid] = FakeSup(self, key, generation, pid, float(pid))
        return SupervisorRef(pid, float(pid))

    def spawn_recovery(self, key, generation, *, paths, reason, reason_source, op) -> SupervisorRef:
        if self.spawn_error is not None:
            raise self.spawn_error
        pid = self._new_pid()
        ref = SupervisorRef(pid, float(pid))
        self.recoveries.append((ref, key, generation, reason, reason_source, op))
        return ref

    def alive(self, ref, key: str) -> bool:
        sup = self.sups.get(ref.pid)
        if sup is not None:
            return sup.alive and sup.start == ref.start_time and sup.key == key
        return any(r[0].pid == ref.pid and r[1] == key for r in self.recoveries)

    def wake(self, ref, key: str) -> bool:
        self.woken.append(ref.pid)
        return self.alive(ref, key)

    def sleep(self, seconds: float) -> None:
        self.mono += seconds
        self.tick()

    def monotonic(self) -> float:
        return self.mono

    # --- driving ------------------------------------------------------------------------

    def tick(self) -> None:
        """One pass of every live supervisor, then any detached recovery."""
        if not self.hung:
            for sup in list(self.sups.values()):
                if sup.alive:
                    sup.step()
        pending, self.recoveries = self.recoveries, []
        for ref, key, gen, reason, source, op in pending:
            result = recover_lease(
                key, gen, paths=self.paths, reason=reason, reason_source=source, op=op,
                events=self.log, now_fn=self.clock, me=ProcessRef(ref.pid, ref.start_time),
                term_grace_s=0.01, kill_grace_s=0.01, table=self.table, alive_fn=self.alive,
            )
            self.recovered.append((key, gen, reason, result.outcome))

    def settle(self, rounds: int = 3) -> None:
        for _ in range(rounds):
            self.tick()

    # --- test controls -------------------------------------------------------------------

    def sup_for(self, key: str) -> FakeSup:
        live = [s for s in self.sups.values() if s.key == key]
        assert live, f"no supervisor was spawned for {key}"
        return live[-1]

    def kill_supervisor(self, key: str) -> FakeSup:
        sup = self.sup_for(key)
        sup.alive = False
        self.table.kill_now(sup.pid)
        return sup

    def kill_workload(self, leader: int) -> None:
        """Every member of the leader's session exits on its own."""
        proc = self.table.procs.get(leader)
        assert proc is not None, f"no workload with leader {leader}"
        for p in list(self.table.procs.values()):
            if p.sid == proc.sid and p.pid != p.sid:
                self.table.kill_now(p.pid)
        if leader == proc.sid:  # a legacy leader is its own session
            self.table.kill_now(leader)

    def add_legacy_workload(self, pid: int, start: float, **kw) -> WorkloadIdentity:
        """A 1.0.x workload: the spawned shell was its own session leader (§2)."""
        self.table.add(pid, pid, start, name="sh", **kw)
        return WorkloadIdentity(pid, start, SID_OWNER_WORKLOAD)

    def _new_pid(self) -> int:
        pid = self._next_sup
        self._next_sup += 1
        return pid
