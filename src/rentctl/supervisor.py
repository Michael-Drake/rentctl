# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Drake

"""The per-lease supervisor: it *is* the workload's session (ADR-0016).

``python -m rentctl.supervisor <lease-key> <generation>``

``rent up`` writes a ``starting`` lease and spawns this, detached. From then on
this process owns the environment until its session is verified empty:

1. **Register** under the project lock L: the lease must exist at this
   generation and be ``starting``, or nothing is launched (§6, acceptance #11).
2. **Launch** the approved command with ``shell=True`` and *no* new session,
   so the shell and everything it spawns are born in session S = this pid.
   Ownership is on disk before the first workload process exists (I1).
3. **Readiness**, attributed by session id: a listener in S is ours; anything
   else is foreign and is never signalled (§5).
4. **Run**: reap the leader, stat the lease every second, wake on SIGWINCH,
   honour renewed ``expires``, trim the log. A stop request, expiry, SIGTERM,
   or a lost lease ends it.
5. **Stop** with ``stop_workload(mode=OWNER)``: ``killpg`` of its own group,
   verified per-pid escalation, verified empty. It survives its own TERM
   through a Python handler, and never exits while its session has members
   (R3): ``cleanup_incomplete`` retries on the lifecycle's backoff.

**Linux (plan step 9).** Before registering, the supervisor tries to become a
child subreaper (``prctl(PR_SET_CHILD_SUBREAPER)``, feature-detected). If it
can, orphans in its subtree — including a double-forked, ``setsid``'d daemon —
reparent to it instead of init, so its descendants are members whatever their
SID, it reaps the orphans it inherits, and the lease records
``supervision: session+subreaper``. Otherwise it is ``session``: the macOS
guarantee, where a ``setsid`` escaper is outside supervision.

``python -m rentctl.supervisor --recover <lease-key> <generation>`` is R0's
recovery supervisor: it adopts an unsupervised or legacy lease's session under
a recovery claim and stops it (``core.supervision.recover_lease``). It never
calls ``setsid`` into anything it signals: recovery mode signals per pid only.

Every lease write is one ``lifecycle.transition`` under L. A transition whose
result *is* the input lease is an acknowledgement: nothing is written and
nothing is recorded. L is never held while waiting for anything (§9).
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .core import events as ev
from .core import procutil
from .core.errors import (
    INTERNAL,
    START_TIMEOUT,
    STATE_WRITE_FAILED,
    UNSUPERVISABLE,
    DevctlError,
)
from .core.events import EventLog
from .core.leases import Lease, ProcessRef, SupervisorRef
from .core.lifecycle import (
    REMOVED_ON_REACH,
    Actor,
    ActorKind,
    Event,
    EventKind,
    Refused,
    State,
    cleanup_retry_delay,
    retry_due,
    state_of,
    supervisor_owns,
    transition,
)
from .core.locking import project_lock
from .core.logcap import trim_log_if_large
from .core.models import SID_OWNER_SUPERVISOR, Readiness, StopOutcome, WorkloadIdentity
from .core.paths import DevctlPaths, project_from_key
from .core.procutil import Membership, OsProcessTable, ProcessTable, SignalResult
from .core.service import _port_answering
from .core.supervision import (
    RECOVER_FLAG,
    RECOVERED_SUPERSEDED,
    WAKE_SIGNAL,
    escaped_listener,
    recover_lease,
    record_cleanup_incomplete,
    record_down,
    record_up_failed,
    self_ref,
)
from .core.workload import KILL_GRACE_S, OWNER, TERM_GRACE_S, stop_workload

# Exit codes. Only EXIT_NOT_LAUNCHED is load-bearing: it is how a test (and a
# human reading `ps` history) tells "refused to start" from "ran and finished".
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_LAUNCHED = 3
EXIT_NOT_SESSION_LEADER = 4
EXIT_FAULT = 5

READINESS_TIMEOUT_S = 30.0       # spec default, unchanged (ADR-0016 §14)
LEASE_STAT_S = 1.0               # a lost wake costs at most this (§8)
READY_POLL_S = 0.1
OWNER_PROBE_S = 1.0              # lsof is a subprocess on macOS: 1 Hz, as in service.py
# How long an emptied session is watched for a late listener before startup is
# called a plain failure. A daemonizer's shell exits first and its detached child
# binds a moment later; without this window it reads as "exited during startup"
# instead of the UNSUPERVISABLE it is (§5, R2).
EMPTY_LISTENER_GRACE_S = 2.0
LOG_TRIM_S = 60.0

# --- test-only seams: honoured only when RENTCTL_TESTING=1 -------------------
FAULT_POINTS = ("after_register", "after_launch", "before_running_write", "mid_stop")
_FAULT_PARK_S = 120.0  # a parked supervisor the test forgot still ends


def _testing() -> bool:
    return os.environ.get("RENTCTL_TESTING") == "1"


def _now() -> datetime:
    return datetime.now().astimezone()


class _RefusingTable(OsProcessTable):
    """Test seam (#6): while the named file exists, every per-pid signal is
    refused as EPERM would refuse it — the shape of a UID-changed child. The
    owner-mode ``killpg`` still goes out; it is not a per-pid signal."""

    def __init__(self, flag: Path) -> None:
        self.flag = flag

    def send(self, pid, sig, verify):
        if self.flag.exists():
            refused = verify()
            return refused if refused is not None else SignalResult.DENIED
        return super().send(pid, sig, verify)


def _test_table() -> ProcessTable | None:
    flag = os.environ.get("RENTCTL_TEST_REFUSE_SIGNALS")
    if _testing() and flag:
        return _RefusingTable(Path(flag))
    return None


_LOST = object()  # the lease is gone, replaced, or unreadable: not ours any more


@dataclass
class _Failure:
    code: str
    phase: str
    message: str

    def error(self, log_tail: list[str]) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "phase": self.phase, "log_tail": log_tail}


class Supervisor:
    """One supervisor run. Built by :func:`main` after ``setsid``; not reusable."""

    def __init__(
        self,
        key: str,
        generation: str,
        paths: DevctlPaths,
        *,
        table: ProcessTable | None = None,
        now_fn: Callable[[], datetime] = _now,
        supervision: str = procutil.SUPERVISION_SESSION,
    ) -> None:
        self.key = key
        self.generation = generation
        self.paths = paths
        self.project = project_from_key(key)
        self.lease_path = paths.lease_file(key)
        self.lock_path = paths.lock_file(self.project)
        self.log = EventLog(paths.events_file)
        self.table = table
        self.now = now_fn
        self.me: ProcessRef = self_ref()
        self.sid = os.getpid()
        # What main() established before this object existed (plan step 9).
        # With the subreaper, the supervisor's whole subtree is the workload.
        self.supervision = supervision
        self.subreaper = supervision == procutil.SUPERVISION_SUBREAPER
        self.actor = Actor(ActorKind.SUPERVISOR, generation, self.me)
        self.wake = threading.Event()
        self.terminated = False
        self.leader: subprocess.Popen | None = None
        self.lease: Lease | None = None       # the last one read or written
        self.log_path: Path | None = None
        self.term_grace_s = TERM_GRACE_S
        self.kill_grace_s = KILL_GRACE_S
        self.readiness_timeout_s = READINESS_TIMEOUT_S
        # Set once startup has failed: the `error` a startup_failed record carries.
        self.startup_error: dict[str, Any] | None = None

    # --- signals ---------------------------------------------------------------

    def install_handlers(self) -> None:
        # A Python handler, never SIG_IGN: SIG_IGN survives exec, so the workload
        # would inherit "ignore SIGTERM" and every stop would need SIGKILL. A
        # handler is reset to the default by exec (§1, E3/E4). This handler is
        # also how the supervisor survives its own killpg(S, SIGTERM).
        signal.signal(signal.SIGTERM, self._on_term)
        signal.signal(WAKE_SIGNAL, self._on_wake)

    def _on_term(self, signum, frame) -> None:
        self.terminated = True
        self.wake.set()

    def _on_wake(self, signum, frame) -> None:
        self.wake.set()

    # --- diagnostics -------------------------------------------------------------

    def say(self, message: str) -> None:
        """One line into the environment's own log, where a human looks first."""
        if self.log_path is None:
            return
        line = f"[rentctl supervisor {self.sid}] {_now().isoformat(timespec='seconds')} {message}\n"
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass

    def fault(self, point: str) -> None:
        """Test-only: park here so a test can SIGKILL the supervisor at a known step."""
        if not _testing() or os.environ.get("RENTCTL_TEST_FAULT") != point:
            return
        self.say(f"test fault {point}: parked")
        deadline = time.monotonic() + _FAULT_PARK_S
        while time.monotonic() < deadline:
            time.sleep(0.1)
        os._exit(EXIT_FAULT)

    # --- the lease -----------------------------------------------------------------

    def read(self):
        """The lease if it is still ours at this generation, else ``_LOST``.

        Read without L: writes are atomic replaces, so a read sees one whole
        version. A corrupt file counts as lost. rentctl never writes one, so
        someone else did, and the supervisor can no longer prove it owns
        anything named there. Stopping its *own* session is still always safe.
        """
        try:
            lease = Lease.read_if_exists(self.lease_path)
        except (DevctlError, OSError):
            return _LOST
        if not supervisor_owns(lease, self.generation):
            return _LOST
        self.lease = lease
        return lease

    def commit(self, event: Event, record: Callable[[Lease, Lease], None] | None = None):
        """Apply one transition under L. Returns the new lease, the unchanged
        lease (acknowledged), a :class:`Refused`, or ``_LOST``.

        The event is appended *before* a terminal record is unlinked (§7), and
        after a non-terminal one is written. A failed write raises ``OSError``
        to the caller, which knows what the failure means at that step.
        """
        with project_lock(self.lock_path):
            try:
                lease = Lease.read_if_exists(self.lease_path)
            except DevctlError:
                return _LOST
            if not supervisor_owns(lease, self.generation):
                return _LOST
            result = transition(lease, event, self.actor)
            if not isinstance(result, Lease) or result is lease:
                self.lease = lease
                return result
            if state_of(result) in REMOVED_ON_REACH:
                if record is not None:
                    record(lease, result)
                self.lease_path.unlink(missing_ok=True)
            else:
                result.write(self.lease_path)
                if record is not None:
                    record(lease, result)
            self.lease = result
            return result

    # --- the workload ----------------------------------------------------------------

    def identity(self) -> WorkloadIdentity:
        return WorkloadIdentity(
            self.sid, self.me.start_time, SID_OWNER_SUPERVISOR,
            tree_root=self.sid if self.subreaper else None,
        )

    def scan(self) -> Membership:
        ident = self.identity()
        return procutil.session_scan(
            ident.sid, ident.owner_start, ident.exclude, table=self.table, tree_root=ident.tree_root
        ).state

    def _is_ours_pid(self, pid: int) -> bool:
        """Is ``pid`` part of this workload: in S, or (subreaper) our descendant?"""
        if self._is_ours(pid):
            return True
        if not self.subreaper:
            return False
        row = (self.table or procutil.OsProcessTable()).row(pid)
        return row is not None and procutil.in_tree(row, self.sid, table=self.table)

    def reap(self) -> None:
        # Without the subreaper the leader is our only direct child: orphaned
        # grandchildren reparent to launchd/init, which reaps them. With it,
        # they reparent to *us*, and nobody else will wait for them. The
        # leader's status stays its Popen's (keep), so poll() still reads it.
        if self.leader is not None:
            self.leader.poll()
        if self.subreaper:
            procutil.reap_orphans(keep=() if self.leader is None else (self.leader.pid,))

    def stop(self) -> StopOutcome:
        self.fault("mid_stop")
        return stop_workload(
            self.identity(),
            OWNER,
            term_grace_s=self.term_grace_s,
            kill_grace_s=self.kill_grace_s,
            table=self.table,
            reap=self.reap,
        )

    def _log_tail(self, n: int = 40) -> list[str]:
        if self.log_path is None:
            return []
        try:
            return self.log_path.read_text(errors="replace").splitlines()[-n:]
        except OSError:
            return []

    def wait(self, timeout: float) -> None:
        self.wake.wait(max(0.0, timeout))

    # ==================================================================================
    # 1. registration
    # ==================================================================================

    def register(self) -> Lease | None:
        """§6 supervisor step 1, under L. ``None`` means: launch nothing, exit."""
        with project_lock(self.lock_path):
            try:
                lease = Lease.read_if_exists(self.lease_path)
            except DevctlError:
                return None
            if not supervisor_owns(lease, self.generation):
                return None
            if state_of(lease) is not State.STARTING:
                return None
            # The level rides on the registration write, so the lease says which
            # guarantee is in effect before anything is launched (plan step 9).
            level = SupervisorRef(self.me.pid, self.me.start_time, supervision=self.supervision)
            result = transition(
                lease, Event(EventKind.REGISTER, self.now(), supervisor=level), self.actor
            )
            if not isinstance(result, Lease):
                return None
            try:
                result.write(self.lease_path)
            except OSError:
                return None  # reconcilers treat it as unregistered: nothing launched (I1)
        self.lease = result
        return result

    def configure(self, lease: Lease) -> None:
        plan = lease.plan or {}
        # Timings ride in the plan the CLI wrote, so an injected readiness
        # timeout (Service.readiness_timeout) reaches the process that probes.
        self.readiness_timeout_s = float(plan.get("readiness_timeout_s", READINESS_TIMEOUT_S))
        self.term_grace_s = float(plan.get("term_grace_s", TERM_GRACE_S))
        self.kill_grace_s = float(plan.get("kill_grace_s", KILL_GRACE_S))
        self.log_path = Path(plan.get("log") or lease.log)

    # ==================================================================================
    # 2. launch
    # ==================================================================================

    def launch(self, lease: Lease) -> None:
        """Spawn the plan's command inside S. Raises ``OSError`` if it cannot."""
        plan = lease.plan or {}
        cmd = plan["cmd"]
        cwd = plan.get("cwd") or lease.spawn_cwd or lease.cwd
        port_env = plan.get("port_env") or "PORT"
        env = {**os.environ, port_env: str(lease.port)}
        for name in ("RENTCTL_TESTING", "RENTCTL_TEST_FAULT", "RENTCTL_TEST_REFUSE_SIGNALS",
                     "RENTCTL_TEST_NO_SUBREAPER"):
            env.pop(name, None)  # the seams are the supervisor's, never the workload's
        assert self.log_path is not None
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "ab", buffering=0) as log_f:
            # No start_new_session and no process_group: the workload is born in
            # S and group S (arrangement A). Membership is found by SID, so even
            # a child that setpgid()s stays ours.
            self.leader = subprocess.Popen(
                cmd,
                shell=True,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_f,
                stderr=subprocess.STDOUT,
            )

    def record_launch(self):
        assert self.leader is not None
        start = procutil.observe_start_time(self.leader.pid) or 0.0
        handle = {"pid": self.leader.pid, "pid_start_time": start}
        return self.commit(Event(EventKind.LAUNCHED, self.now(), handle=handle))

    # ==================================================================================
    # 3. readiness, attributed by SID
    # ==================================================================================

    def _listener(self) -> tuple[Any, bool]:
        """``(owner, probed)``: who listens on the port, and whether we could tell."""
        assert self.lease is not None
        try:
            return procutil.port_owner(self.lease.port), True
        except procutil.ProbeUnavailable:
            return None, False

    def _is_ours(self, pid: int) -> bool:
        try:
            return os.getsid(pid) == self.sid
        except OSError:
            return False

    def _born_after_launch(self, pid: int) -> bool:
        start = procutil.observe_start_time(pid)
        return start is not None and start >= self.me.start_time - 1.0

    def await_ready(self) -> Readiness | _Failure | object:
        """Poll until the workload is up and attributed, or it has failed.

        Returns a ``Readiness``, a ``_Failure``, or ``_LOST``. No lock is held.
        """
        assert self.lease is not None
        port = self.lease.port
        deadline = time.monotonic() + self.readiness_timeout_s
        next_owner = 0.0
        probe_ran = False
        foreign = None
        empty_since: float | None = None
        while True:
            self.reap()
            self.wake.clear()
            lease = self.read()
            if lease is _LOST:
                return _LOST
            if lease.stop is not None:
                return _Failure(
                    START_TIMEOUT, ev.PHASE_READINESS,
                    f"startup abandoned: a stop was requested ({lease.stop.reason})",
                )
            if self.terminated:
                return _Failure(
                    START_TIMEOUT, ev.PHASE_READINESS, "the supervisor was sent SIGTERM during startup"
                )
            answered = _port_answering(port)
            now = time.monotonic()
            membership = self.scan()
            if answered or now >= next_owner or membership is Membership.EMPTY:
                next_owner = now + OWNER_PROBE_S
                owner, probed = self._listener()
                probe_ran = probe_ran or probed
                if owner is not None:
                    # On a Linux subreaper a daemonizer still in our subtree is
                    # ours (§5): it is captured, stopped, and never UNSUPERVISABLE.
                    if self._is_ours_pid(owner.pid):
                        # `answered` is from BEFORE the owner probe, and the probe
                        # is an lsof subprocess that can take long enough for the
                        # server to start listening in between. Deciding on the
                        # stale value reported a server that was simply late as
                        # "not on loopback — http://localhost will not reach it",
                        # which is false. Found under load in CI-like runs; the
                        # 1.0.x readiness loop had the same race. Ask again now
                        # that we know the listener is ours.
                        if not answered:
                            answered = _port_answering(port)
                        return Readiness.ANSWERED if answered else Readiness.LISTENING
                    foreign = owner
                    if membership is Membership.EMPTY and self._born_after_launch(owner.pid):
                        # Positive evidence only (R2): our session is empty and the
                        # port is answered from outside it by a process born after
                        # we launched. The command daemonized. Not signalled.
                        return _Failure(
                            UNSUPERVISABLE, ev.PHASE_UNSUPERVISABLE,
                            f"the command's listener (pid {owner.pid}, {owner.name}) left the "
                            "supervised session (it daemonized); rentctl cannot own it",
                        )
                elif answered and not probed:
                    # Something answers and the owner probe cannot run. The
                    # 1.0.x rule stands: "cannot tell" is up, and the lease we
                    # write keeps whatever it is tracked (Readiness.is_up).
                    return Readiness.UNKNOWN
            if membership is Membership.EMPTY:
                empty_since = empty_since if empty_since is not None else now
                if now - empty_since >= EMPTY_LISTENER_GRACE_S or now >= deadline:
                    return _Failure(
                        START_TIMEOUT, ev.PHASE_READINESS,
                        f"{self.project!r} exited during startup on port {port}",
                    )
            else:
                empty_since = None
            if now >= deadline:
                if foreign is not None:
                    return _Failure(
                        START_TIMEOUT, ev.PHASE_FOREIGN_LISTENER,
                        f"port {port} answered by a process outside this environment "
                        f"(pid {foreign.pid}, {foreign.name})",
                    )
                if not probe_ran:
                    return Readiness.UNKNOWN
                return _Failure(
                    START_TIMEOUT, ev.PHASE_READINESS,
                    f"{self.project!r} did not answer on port {port} within "
                    f"{self.readiness_timeout_s:.0f}s, and nothing of ours is listening on it",
                )
            self.wait(READY_POLL_S)

    # ==================================================================================
    # the run
    # ==================================================================================

    def run(self) -> int:
        lease = self.register()
        if lease is None:
            return EXIT_NOT_LAUNCHED
        self.configure(lease)
        self.say(f"registered for generation {self.generation} (supervision: {self.supervision})")
        self.fault("after_register")
        if self.terminated:
            return self.startup_failed(
                _Failure(START_TIMEOUT, ev.PHASE_SPAWN, "the supervisor was sent SIGTERM before launch")
            )
        try:
            self.launch(lease)
        except (OSError, KeyError) as e:
            return self.startup_failed(_Failure(INTERNAL, ev.PHASE_SPAWN, f"could not launch: {e}"))
        try:
            launched = self.record_launch()
        except OSError as e:
            # S = supervisor.pid is on disk already, so the workload is still
            # identifiable; but an environment that cannot be recorded is not run.
            return self.startup_failed(
                _Failure(STATE_WRITE_FAILED, ev.PHASE_STATE_WRITE, f"could not record the launch: {e}")
            )
        if launched is _LOST:
            return self.lease_lost()
        if not isinstance(launched, Lease):
            return self.startup_failed(_Failure(INTERNAL, ev.PHASE_STATE_WRITE, launched.detail))
        self.fault("after_launch")

        verdict = self.await_ready()
        if verdict is _LOST:
            return self.lease_lost()
        if isinstance(verdict, _Failure):
            return self.startup_failed(verdict)
        self.fault("before_running_write")
        try:
            running = self.commit(
                Event(EventKind.READY, self.now(), readiness=verdict.value), record=self._record_up
            )
        except OSError as e:
            return self.startup_failed(
                _Failure(STATE_WRITE_FAILED, ev.PHASE_STATE_WRITE, f"could not record running: {e}")
            )
        if running is _LOST:
            return self.lease_lost()
        if not isinstance(running, Lease):
            # STOP_PENDING: a stop landed between the probe and this write.
            reason = self.lease.stop.reason if self.lease and self.lease.stop else "refused"
            return self.startup_failed(
                _Failure(START_TIMEOUT, ev.PHASE_READINESS, f"startup abandoned ({reason})")
            )
        self.say(f"running on port {running.port} ({verdict.value})")
        return self.supervise()

    def _record_up(self, before: Lease, after: Lease) -> None:
        self.log.record_up(
            after.project,
            profile=after.profile,
            port=after.port,
            pid=self.leader.pid if self.leader else self.sid,
            session=after.session,
            cwd=after.cwd,
            lease_expires=after.expires.isoformat(),
            already_running=False,
            spawn_cwd=after.spawn_cwd if after.spawn_cwd != after.cwd else None,
            generation=after.generation,
            supervisor_pid=self.sid,
            readiness=after.readiness,
        )

    # --- 4. running ------------------------------------------------------------------------

    def supervise(self) -> int:
        next_trim = time.monotonic() + LOG_TRIM_S
        while True:
            self.reap()
            self.wake.clear()
            lease = self.read()
            if lease is _LOST:
                return self.lease_lost()
            now = self.now()
            if lease.stop is not None:
                return self.teardown(None)
            if self.terminated:
                return self.teardown(ev.SUPERVISOR_TERMINATED)
            if now >= lease.expires:
                result = self.teardown(ev.EXPIRY)
                if result is not None:
                    return result
                continue  # renewed between the read and the write: keep running
            if self.scan() is Membership.EMPTY:
                done = self.commit(Event(EventKind.SESSION_EMPTIED, now), record=self._record_exited)
                if isinstance(done, Lease) or done is _LOST:
                    self.say("the workload exited on its own")
                    return EXIT_OK
                continue  # a stop landed first: take it next pass
            if time.monotonic() >= next_trim:
                next_trim = time.monotonic() + LOG_TRIM_S
                trim_log_if_large(Path(lease.log))
            self.wait(min(LEASE_STAT_S, (lease.expires - now).total_seconds()))

    def _record_exited(self, before: Lease, after: Lease) -> None:
        record_down(
            self.log, after, actor=ev.ACTOR_SUPERVISOR, outcome=None, reason=ev.PROCESS_GONE,
            reason_source=ev.DECLARED, op=ev.ACTOR_SUPERVISOR,
        )

    # --- 5. stopping -------------------------------------------------------------------------

    def teardown(self, reason: str | None) -> int | None:
        """``running`` → ``stopping`` → stop → ``stopped`` | ``cleanup_incomplete``.

        ``reason`` is the supervisor's own (expiry, SIGTERM) and is used only
        when no request is recorded: the first reason written wins (§8).
        Returns ``None`` when the begin-stop was refused as not yet expired.
        """
        try:
            begun = self.commit(
                Event(EventKind.BEGIN_STOP, self.now(), reason=reason, reason_source=ev.DECLARED,
                      op=ev.ACTOR_SUPERVISOR)
            )
        except OSError as e:
            # `stopping` could not be written, but the stop was decided: carry it
            # out. The outcome write that follows either lands or leaves a
            # `running` record whose session is empty, which reconcilers clean.
            self.say(f"could not record stopping: {e}")
            begun = None
        if begun is _LOST:
            return self.lease_lost()
        if isinstance(begun, Refused):
            if reason == ev.EXPIRY:
                return None
            self.say(f"begin-stop refused: {begun.detail}")
        outcome = self.stop()
        return self.settle(outcome)

    def record_outcome(self, outcome: StopOutcome):
        """Write a stop's outcome as one transition, with its one event.

        ``stopped`` gets the teardown's single ``down``, carrying the first
        requester's op/reason/reason_source (§8); anything else gets a
        ``cleanup_incomplete`` event — one per attempt. Returns the commit's
        result, or ``None`` if the write itself failed.
        """
        lease = self.lease
        stop = lease.stop if lease is not None else None
        reason = stop.reason if stop else ev.SUPERVISOR_TERMINATED
        reason_source = stop.reason_source if stop else ev.DECLARED
        op = stop.op if stop else ev.ACTOR_SUPERVISOR
        escaped = (
            escaped_listener(lease.port, self.sid) if outcome.verified and lease is not None else None
        )
        startup = lease is not None and state_of(lease) is State.STARTING

        def record(before: Lease, after: Lease) -> None:
            if startup:
                record_up_failed(
                    self.log, after, error=after.error or {},
                    cleanup=ev.CLEANUP_VERIFIED if outcome.verified else ev.CLEANUP_INCOMPLETE_VALUE,
                )
            if state_of(after) is State.STOPPED:
                record_down(
                    self.log, after, actor=ev.ACTOR_SUPERVISOR, outcome=outcome, reason=reason,
                    reason_source=reason_source, op=op, escaped=escaped,
                )
            elif not outcome.verified:
                record_cleanup_incomplete(
                    self.log, after, outcome,
                    reason=self.startup_error.get("phase", reason) if startup and self.startup_error
                    else reason,
                    reason_source=reason_source, op=ev.UP if startup else op,
                )

        kind = EventKind.STOP_VERIFIED if outcome.verified else EventKind.STOP_INCOMPLETE
        try:
            return self.commit(
                Event(kind, self.now(), survivors=outcome.survivors, error=self.startup_error),
                record=record,
            )
        except OSError as e:
            # For a startup failure this is §6's last arm: neither `running` nor
            # `startup_failed` could be written, and a reconciler finds
            # `starting`, registered, supervisor gone, and recovers.
            self.say(f"could not record the stop outcome: {e}")
            return None

    def settle(self, outcome: StopOutcome) -> int:
        """Record a stop's outcome; hold on while members remain (R3)."""
        result = self.record_outcome(outcome)
        if result is _LOST:
            return self.lease_lost(outcome)
        if outcome.verified:
            self.say("stopped; session verified empty")
            return EXIT_OK
        self.say(f"cleanup incomplete: {outcome.detail}")
        return self.hold()

    def startup_failed(self, failure: _Failure) -> int:
        """§6 step 4 failure arm: stop our own session, then ``startup_failed``
        (verified empty) or ``cleanup_incomplete{phase: startup}`` (survivors)."""
        self.say(f"startup failed ({failure.code}, {failure.phase}): {failure.message}")
        outcome = self.stop()
        self.startup_error = failure.error(self._log_tail())
        return self.settle(outcome)

    def hold(self) -> int:
        """``cleanup_incomplete``: retry on the backoff until verified (R3).

        The supervisor stays alive, mostly idle, while its session has members.
        That keeps S reserved (§2) and keeps the lease honest. Retries follow
        ``lifecycle.retry_due``: 5 s, 15 s, 60 s, then every 60 s — at most one
        per minute after the first minute — or at once when a later stop
        request asks for one (§8). Each attempt records one event.
        """
        attempts = 1
        last = time.monotonic()
        while True:
            self.reap()
            self.wake.clear()
            lease = self.read()
            if lease is _LOST:
                return self.lease_lost()
            now = self.now()
            c = lease.cleanup
            if state_of(lease) is State.CLEANUP_INCOMPLETE and c is not None and c.last_attempt:
                due = retry_due(lease, now)
                wait_s = (c.last_attempt + cleanup_retry_delay(c.attempts) - now).total_seconds()
            else:
                # The outcome never reached the lease (its write failed), so the
                # on-disk schedule is not ours to read: keep our own.
                wait_s = cleanup_retry_delay(attempts).total_seconds() - (time.monotonic() - last)
                due = wait_s <= 0
            if not due:
                self.wait(min(LEASE_STAT_S, wait_s))
                continue
            attempts += 1
            last = time.monotonic()
            outcome = self.stop()
            result = self.record_outcome(outcome)
            if result is _LOST:
                return self.lease_lost(outcome)
            if outcome.verified:
                self.say(f"retry {attempts} verified; session empty")
                return EXIT_OK

    # --- the lease went away --------------------------------------------------------------------

    def lease_lost(self, outcome: StopOutcome | None = None) -> int:
        """§7 last row: stop **our own** session, record ``lease-lost``, exit.

        Nothing is written to the lease path: it is not ours any more, and it
        may already hold a newer generation. Survivors are retried on the
        backoff with no lease to record them in, because the supervisor still
        does not exit while its session has members (R3).
        """
        last = self.lease
        attempt = 0
        while True:
            if outcome is None or not outcome.verified:
                outcome = self.stop()  # an empty session returns verified, signalling nothing
            attempt += 1
            if last is not None:
                if outcome.verified:
                    record_down(
                        self.log, last, actor=ev.ACTOR_SUPERVISOR, outcome=outcome,
                        reason=ev.LEASE_LOST, reason_source=ev.DECLARED, op=ev.ACTOR_SUPERVISOR,
                    )
                else:
                    record_cleanup_incomplete(
                        self.log, last, outcome, reason=ev.LEASE_LOST, reason_source=ev.DECLARED,
                        op=ev.ACTOR_SUPERVISOR,
                    )
            if outcome.verified:
                self.say("lease lost; own session stopped")
                return EXIT_OK
            self.wait(cleanup_retry_delay(attempt).total_seconds())


def _become_session_leader() -> bool:
    """S must be this pid. Spawned with ``start_new_session`` it already is,
    and ``setsid`` then fails with EPERM; run by hand it is not, and ``setsid``
    makes it so. Either way the check afterwards is what decides."""
    try:
        os.setsid()
    except OSError:
        pass
    return os.getsid(0) == os.getpid() and os.getpgid(0) == os.getpid()


def establish_supervision(enable: Callable[[], bool] = procutil.enable_child_subreaper) -> str:
    """Take the strongest guarantee this host allows, before any child exists.

    On Linux, ``prctl(PR_SET_CHILD_SUBREAPER, 1)``: orphaned descendants —
    including a daemonizer's double-forked, ``setsid``'d child — then reparent
    to this supervisor instead of init, so they stay in its subtree and are
    members. Anywhere the call is missing or refused (macOS, a seccomp'd
    container), the result is the macOS guarantee, and the level says so.
    Test seam: ``RENTCTL_TEST_NO_SUBREAPER=1`` forces the degraded level.
    """
    if _testing() and os.environ.get("RENTCTL_TEST_NO_SUBREAPER") == "1":
        return procutil.SUPERVISION_SESSION
    return procutil.SUPERVISION_SUBREAPER if enable() else procutil.SUPERVISION_SESSION


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="rentctl per-lease supervisor (ADR-0016)")
    parser.add_argument(RECOVER_FLAG, action="store_true", help="adopt and stop an unsupervised lease (R0)")
    parser.add_argument("--reason", default=None)
    parser.add_argument("--reason-source", default=ev.DECLARED)
    parser.add_argument("--op", default=None)
    parser.add_argument("key", help="lease key: <project>--<cwd-hash>")
    parser.add_argument("generation", help="the lease generation this supervisor serves")
    args = parser.parse_args(argv)
    paths = DevctlPaths.default()

    if args.recover:
        # Recovery mode signals per pid from outside the recorded session, so
        # it needs no session of its own; the detached spawn gave it one anyway,
        # which keeps the SessionEnd hook's process group out of its way.
        result = recover_lease(
            args.key, args.generation, paths=paths, reason=args.reason,
            reason_source=args.reason_source, op=args.op, table=_test_table(),
        )
        return EXIT_ERROR if result.outcome == RECOVERED_SUPERSEDED else EXIT_OK

    if not _become_session_leader():
        return EXIT_NOT_SESSION_LEADER
    sup = Supervisor(
        args.key, args.generation, paths, table=_test_table(), supervision=establish_supervision()
    )
    sup.install_handlers()
    try:
        return sup.run()
    except Exception:  # pragma: no cover - a supervisor bug; recovery (§10) takes over
        sup.say("crashed:\n" + traceback.format_exc())
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
