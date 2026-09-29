"""``core/supervision.py`` against the fake process table (ADR-0016 §3, §10, R0).

The real-process tests in ``test_supervisor.py`` drive the supervisor and the
recovery path end to end. These drive the branches real processes cannot hit
on demand — a supervisor that is alive but hung, a session number held by a
stranger, a claim taken over mid-stop — with the fake table from step 1, and
no process is signalled. Pids are far above anything real (macOS wraps at
99999), so a mistake here could not reach a live process either.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import psutil
import pytest

from fakeproc import FakeProcessTable
from rentctl.core import events as ev
from rentctl.core import leases as leases_mod
from rentctl.core import lifecycle, models, procutil, supervision
from rentctl.core.errors import SUPERVISOR_START_FAILED, DevctlError
from rentctl.core.events import EventLog
from rentctl.core.leases import (
    CleanupRecord,
    Lease,
    ProcessRef,
    RecoveryClaim,
    StopRequest,
    SupervisorRef,
)
from rentctl.core.models import SID_OWNER_SUPERVISOR, ProcInfo, Survivor, WorkloadIdentity
from rentctl.core.paths import lease_key
from rentctl.core.supervision import (
    RECOVERED_CLEANED,
    RECOVERED_GONE,
    RECOVERED_INCOMPLETE,
    RECOVERED_KEPT,
    RECOVERED_STOPPED,
    RECOVERED_SUPERSEDED,
    escaped_listener,
    is_supervisor,
    recover_lease,
    spawn_supervisor,
    wake_supervisor,
)

CDT = timezone(timedelta(hours=-5))
T0 = datetime(2026, 9, 28, 9, 0, tzinfo=CDT)
S = 7_000_000
SUP_START = 1000.0
MEMBER = 7_000_001
GEN = "c" * 32
ME = ProcessRef(7_100_000, 5000.0)
PROJECT = "seam"


# --- the unified types ---------------------------------------------------------------

def test_one_membership_enum_and_one_identity_type():
    """The two lanes' duplicates are one type each now: an enum compared with
    `is` across modules only works if it is literally the same class."""
    assert lifecycle.Membership is procutil.Membership
    assert leases_mod.OwnershipIdentity is models.WorkloadIdentity
    assert leases_mod.Survivor is models.Survivor
    ident = WorkloadIdentity(S, SUP_START, SID_OWNER_SUPERVISOR)
    assert (ident.sid_owner_start_time, ident.sid_owner, ident.excludes_owner) == (
        SUP_START, SID_OWNER_SUPERVISOR, True
    )


def test_survivor_round_trips_an_unreadable_start_time():
    s = Survivor(pid=5, start_time=None, name="x", status="running")
    assert Survivor.from_dict(s.to_dict()) == s


# --- fixtures ----------------------------------------------------------------------------

@pytest.fixture
def paths(devctl_home):
    return devctl_home


@pytest.fixture
def key(paths):
    return lease_key(PROJECT, "/tmp/seam-project")


def base(**kw) -> Lease:
    lease = lifecycle.new_starting_lease(
        generation=GEN, project=PROJECT, profile="default", runner="process", port=47001,
        session="s", cwd="/tmp/seam-project", spawn_cwd="/tmp/seam-project", log="/tmp/seam.log",
        plan={"cmd": "true", "cwd": "/tmp", "port_env": "PORT"}, now=T0,
        expires=T0 + timedelta(hours=2),
    )
    return replace(lease, **kw)


def supervised(state: str, **kw) -> Lease:
    return base(
        state=state,
        supervisor=SupervisorRef(S, SUP_START, registered=True),
        handle={"pid": S + 5, "pid_start_time": SUP_START + 1, "sid": S,
                "sid_owner_start_time": SUP_START, "sid_owner": SID_OWNER_SUPERVISOR},
        **kw,
    )


def table_with_member(**kw) -> FakeProcessTable:
    t = FakeProcessTable()
    t.add(MEMBER, S, SUP_START + 2, name="node", **kw)
    return t


def recover(paths, key, *, table=None, alive=False, now=T0 + timedelta(minutes=1), **kw):
    return recover_lease(
        key, GEN, paths=paths, table=table or FakeProcessTable(), me=ME,
        now_fn=lambda: now, alive_fn=lambda ref, k: alive,
        term_grace_s=0.05, kill_grace_s=0.05, **kw,
    )


def events(paths, kind=None):
    rows = EventLog(paths.events_file).read(project=PROJECT)
    return [r for r in rows if kind is None or r["event"] == kind]


# --- §10 rows that need no stop --------------------------------------------------------

def test_no_lease_or_another_generation_is_gone(paths, key):
    assert recover(paths, key).outcome == RECOVERED_GONE
    replace(base(), generation="d" * 32).write(paths.lease_file(key))
    assert recover(paths, key).outcome == RECOVERED_GONE
    paths.lease_file(key).write_text("{not json")
    assert recover(paths, key).outcome == RECOVERED_GONE


def test_unregistered_start_past_timeout_is_abandoned(paths, key):
    base().write(paths.lease_file(key))
    result = recover(paths, key, now=T0 + timedelta(seconds=31))
    assert result.outcome == RECOVERED_CLEANED
    assert not paths.lease_file(key).exists()
    (failed,) = events(paths, ev.UP_FAILED)
    assert failed["phase"] == ev.PHASE_REGISTRATION and failed["error"] == SUPERVISOR_START_FAILED


def test_running_with_dead_supervisor_and_empty_session_is_exited(paths, key):
    supervised("running").write(paths.lease_file(key))
    assert recover(paths, key).outcome == RECOVERED_CLEANED
    assert not paths.lease_file(key).exists()
    assert [e["event"] for e in events(paths)] == [ev.SUPERVISOR_LOST, ev.DOWN]
    down = events(paths, ev.DOWN)[0]
    assert down["reason"] == ev.SWEEP_DEAD and down["supervisor_lost"] is True


def test_stopping_found_empty_keeps_the_requested_reason(paths, key):
    stop = StopRequest(GEN, ev.SESSION_END, ev.DECLARED, "down", T0, ME)
    supervised("stopping", stop=stop).write(paths.lease_file(key))
    assert recover(paths, key).outcome == RECOVERED_CLEANED
    assert events(paths, ev.DOWN)[0]["reason"] == ev.SESSION_END


def test_starting_registered_found_empty_is_startup_failed(paths, key):
    supervised("starting").write(paths.lease_file(key))
    assert recover(paths, key).outcome == RECOVERED_CLEANED
    lease = Lease.read(paths.lease_file(key))  # kept for its waiter
    assert lease.state == "startup_failed" and lease.error["phase"] == ev.PHASE_SUPERVISOR_LOST
    assert events(paths, ev.UP_FAILED)[0]["phase"] == ev.PHASE_SUPERVISOR_LOST


def test_leftover_terminal_record_is_removed(paths, key):
    supervised("stopped").write(paths.lease_file(key))
    assert recover(paths, key).outcome == RECOVERED_CLEANED
    assert not paths.lease_file(key).exists()


def test_owned_by_a_live_supervisor_is_left_alone(paths, key):
    supervised("running").write(paths.lease_file(key))
    t = table_with_member()
    assert recover(paths, key, table=t, alive=True).outcome == RECOVERED_KEPT
    assert t.sent == []
    assert Lease.read(paths.lease_file(key)).stop is None


def test_live_but_hung_supervisor_past_expiry_gets_a_stop_request_not_a_signal(paths, key):
    """§10 row 1: a request on disk and a (verified) wake; the workload is never signalled."""
    supervised("running").write(paths.lease_file(key))
    t = table_with_member()
    result = recover(paths, key, table=t, alive=True, now=T0 + timedelta(hours=2, seconds=6))
    assert result.outcome == RECOVERED_KEPT and t.sent == []
    lease = Lease.read(paths.lease_file(key))
    assert lease.stop.reason == ev.EXPIRY
    assert events(paths, ev.STOP_REQUESTED)[0]["reason"] == ev.EXPIRY


def test_ambiguous_identity_is_flagged_and_never_signalled(paths, key):
    supervised("cleanup_incomplete", cleanup=CleanupRecord(attempts=1)).write(paths.lease_file(key))
    t = table_with_member()
    t.add(S, S, SUP_START + 500.0, name="stranger")  # PID S now names a different process
    assert recover(paths, key, table=t).outcome == RECOVERED_KEPT
    assert t.sent == []
    assert Lease.read(paths.lease_file(key)).cleanup.identity_ambiguous is True
    assert events(paths, ev.CLEANUP_INCOMPLETE)[0]["identity_ambiguous"] is True


def test_live_claim_by_another_recoverer_is_respected(paths, key):
    holder = supervision.self_ref()  # a live process that is not ME
    claim = RecoveryClaim(holder.pid, holder.start_time, T0 + timedelta(seconds=50))
    stop = StopRequest(GEN, ev.EXPLICIT, ev.DECLARED, "down", T0, ME)
    supervised("stopping", stop=stop, recovery=claim).write(paths.lease_file(key))
    t = table_with_member()
    assert recover(paths, key, table=t).outcome == RECOVERED_KEPT
    assert t.sent == []


def test_unsupervised_within_its_lease_is_kept_without_a_reason(paths, key):
    supervised("unsupervised").write(paths.lease_file(key))
    t = table_with_member()
    assert recover(paths, key, table=t).outcome == RECOVERED_KEPT
    assert t.sent == []


# --- recovery stops ----------------------------------------------------------------------

def test_recovery_stop_that_leaves_survivors_is_cleanup_incomplete(paths, key):
    supervised("unsupervised").write(paths.lease_file(key))
    t = table_with_member(ignores_term=True, unkillable=True)
    result = recover(paths, key, table=t, reason=ev.EXPLICIT, op="down")
    assert result.outcome == RECOVERED_INCOMPLETE
    lease = Lease.read(paths.lease_file(key))
    assert lease.state == "cleanup_incomplete" and lease.recovery is None
    assert [s.pid for s in lease.cleanup.survivors] == [MEMBER]
    (row,) = events(paths, ev.CLEANUP_INCOMPLETE)
    assert row["survivors"][0]["pid"] == MEMBER and row["op"] == "down"
    assert events(paths, ev.STOP_REQUESTED)


def test_startup_recovery_with_survivors_records_up_failed_and_cleanup(paths, key):
    supervised("starting").write(paths.lease_file(key))
    t = table_with_member(ignores_term=True, unkillable=True)
    assert recover(paths, key, table=t).outcome == RECOVERED_INCOMPLETE
    kinds = [e["event"] for e in events(paths)]
    assert kinds == [ev.SUPERVISOR_LOST, ev.UP_FAILED, ev.CLEANUP_INCOMPLETE]


def test_recovery_stop_verified_on_the_seam(paths, key):
    stop = StopRequest(GEN, ev.EXPLICIT, ev.DECLARED, "down", T0, ME)
    supervised("cleanup_incomplete", stop=stop, cleanup=CleanupRecord(attempts=2)).write(
        paths.lease_file(key)
    )
    t = table_with_member()
    assert recover(paths, key, table=t).outcome == RECOVERED_STOPPED
    assert not paths.lease_file(key).exists()
    down = events(paths, ev.DOWN)[0]
    assert down["attempts"] == 3 and down["mode"] == ev.MODE_RECOVERY


def test_claim_taken_over_mid_stop_records_nothing(paths, key):
    """If the claim lapses and another recoverer takes the lease while this one
    is stopping, the outcome is theirs to record: one teardown, one event."""
    stop = StopRequest(GEN, ev.EXPLICIT, ev.DECLARED, "down", T0, ME)
    supervised("stopping", stop=stop).write(paths.lease_file(key))
    t = table_with_member()

    def steal(pid):
        lease = Lease.read(paths.lease_file(key))
        other = RecoveryClaim(7_200_000, 1.0, T0)
        replace(lease, recovery=other).write(paths.lease_file(key))
        t.before_verify = None

    t.before_verify = steal
    assert recover(paths, key, table=t).outcome == RECOVERED_SUPERSEDED
    assert events(paths, ev.DOWN) == []


def test_identity_turning_ambiguous_mid_stop_stops_signalling(paths, key):
    stop = StopRequest(GEN, ev.EXPLICIT, ev.DECLARED, "down", T0, ME)
    supervised("stopping", stop=stop).write(paths.lease_file(key))
    t = table_with_member(ignores_term=True)

    def reuse(pid):
        t.add(S, S, SUP_START + 900.0, name="stranger")
        t.before_verify = None

    t.before_verify = reuse
    result = recover(paths, key, table=t)
    assert result.outcome == RECOVERED_KEPT and result.stop.identity_ambiguous
    assert all(sig != 9 for _, sig in t.sent)
    assert events(paths, ev.CLEANUP_INCOMPLETE)[0]["identity_ambiguous"] is True


# --- spawn / liveness / wake / escaped listener -------------------------------------------

def test_spawn_failures_are_supervisor_start_failed(paths, monkeypatch):
    def boom(*a, **kw):
        raise OSError("no exec")

    monkeypatch.setattr(supervision.subprocess, "Popen", boom)
    with pytest.raises(DevctlError) as ei:
        spawn_supervisor("p--k", GEN, paths=paths)
    assert ei.value.code == SUPERVISOR_START_FAILED


def test_spawn_of_a_process_that_vanished_is_a_failure(paths, monkeypatch):
    class Dummy:
        pid = 7_300_000

    monkeypatch.setattr(supervision.subprocess, "Popen", lambda *a, **kw: Dummy())
    monkeypatch.setattr(supervision.procutil, "observe_start_time", lambda pid: None)
    with pytest.raises(DevctlError) as ei:
        spawn_supervisor("p--k", GEN, paths=paths, extra_env={"X": "1"})
    assert ei.value.code == SUPERVISOR_START_FAILED


def test_is_supervisor_refuses_what_it_cannot_read(monkeypatch):
    class Denied:
        def __init__(self, pid):
            raise psutil.AccessDenied(pid)

    monkeypatch.setattr(supervision.psutil, "Process", Denied)
    assert not is_supervisor(7_400_000, 1.0, "k")

    class Zombie:
        def __init__(self, pid):
            pass

        def status(self):
            return psutil.STATUS_ZOMBIE

    monkeypatch.setattr(supervision.psutil, "Process", Zombie)
    assert not is_supervisor(7_400_000, 1.0, "k")
    assert not is_supervisor(7_400_000, None, "k")


def test_wake_of_a_vanished_pid_reports_not_sent(monkeypatch):
    monkeypatch.setattr(supervision, "is_supervisor", lambda pid, start, key: True)
    assert not wake_supervisor(ProcessRef(7_500_000, 1.0), "k")  # ESRCH, nothing sent


def test_escaped_listener_reports_only_a_foreign_listener(monkeypatch):
    monkeypatch.setattr(supervision.procutil, "port_owner", lambda port: None)
    assert escaped_listener(1, S) is None

    def unavailable(port):
        raise procutil.ProbeUnavailable("no lsof")

    monkeypatch.setattr(supervision.procutil, "port_owner", unavailable)
    assert escaped_listener(1, S) is None
    me = os.getpid()
    monkeypatch.setattr(
        supervision.procutil, "port_owner", lambda port: ProcInfo(pid=me, name="py", cmdline=())
    )
    assert escaped_listener(1, os.getsid(me)) is None
    assert escaped_listener(1, S) == {"pid": me, "name": "py"}
    monkeypatch.setattr(
        supervision.procutil, "port_owner", lambda port: ProcInfo(pid=7_600_000, name="x", cmdline=())
    )
    assert escaped_listener(1, S) == {"pid": 7_600_000, "name": "x"}
