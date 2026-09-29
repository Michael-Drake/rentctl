"""Tests for the process runner: spawn, verify-then-kill, PID-recycle refusal.

These spawn real (short-lived) processes but run in well under a second each,
so they live with the unit suite. The heavier session scenarios (ADR-0016's
acceptance tests at runner level) are in ``test_runner_sessions.py``.
"""

from __future__ import annotations

import signal
import time

import pytest

from fakeproc import FakeProcessTable
from rentctl.core import procutil
from rentctl.core.errors import REGISTRY_INVALID, DevctlError
from rentctl.core.models import (
    CLEANUP_INCOMPLETE,
    SID_OWNER_SUPERVISOR,
    ProcessHandle,
    StopOutcome,
)
from rentctl.core.registry import RegistryProfile
from rentctl.core.runners import ProcessRunner, get_runner, stop_outcome


def profile(cmd: str, cwd: str) -> RegistryProfile:
    return RegistryProfile(cmd=cmd, cwd=cwd, port_env="PORT", preferred_offset=0)


def runner() -> ProcessRunner:
    return ProcessRunner(term_grace_s=2.0, kill_grace_s=1.0)


def wait_dead(handle: ProcessHandle, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not procutil.is_alive(handle):
            return True
        time.sleep(0.02)
    return not procutil.is_alive(handle)


def test_start_produces_live_handle(tmp_path):
    r = runner()
    h = r.start(profile("sleep 30", str(tmp_path)), 5180, tmp_path / "srv.log")
    try:
        assert h.pid > 0
        assert h.pid_start_time > 0
        # The handle names the workload's session: the shell is its own
        # session leader (start_new_session=True), so sid == its pid.
        assert h.sid == h.pid
        assert h.sid_owner_start_time == h.pid_start_time
        assert r.alive(h) is True
    finally:
        r.stop(h)


def test_start_injects_port_env_and_logs(tmp_path):
    r = runner()
    portfile = tmp_path / "seen_port"
    logf = tmp_path / "srv.log"
    cmd = f'echo "port=$PORT" > "{portfile}"; echo hello-log; sleep 30'
    h = r.start(profile(cmd, str(tmp_path)), 5185, logf)
    try:
        # Give the shell a moment to run the echo lines.
        for _ in range(50):
            if portfile.exists() and "hello-log" in logf.read_text():
                break
            time.sleep(0.02)
        assert portfile.read_text().strip() == "port=5185"
        assert "hello-log" in logf.read_text()
    finally:
        r.stop(h)


def test_stop_kills_and_is_idempotent(tmp_path):
    r = runner()
    h = r.start(profile("sleep 30", str(tmp_path)), 5180, tmp_path / "srv.log")
    first = r.stop(h)
    assert first.verified and first.signalled and not first.escalated
    assert wait_dead(h)
    assert r.alive(h) is False
    # A second stop on an empty session is a no-op, not an error — and it says
    # so: verified, with nothing signalled.
    again = r.stop(h)
    assert again.verified and not again.signalled


def test_stop_refuses_recycled_pid(tmp_path):
    """A handle with the right PID but wrong start time must never be killed.

    Under session membership this is the AMBIGUOUS case (ADR-0016 §2): PID S
    is held by a process that is not the recorded owner, and session S has
    members. Nothing under that number can be proved ours, so the stop signals
    nothing and says so — and ``alive`` answers True, because "cannot tell" is
    not "nobody there": the lease must be kept and surfaced, not deleted.
    """
    r = runner()
    h = r.start(profile("sleep 30", str(tmp_path)), 5180, tmp_path / "srv.log")
    try:
        recycled = ProcessHandle(pid=h.pid, pid_start_time=h.pid_start_time + 10_000)
        assert r.alive(recycled) is True
        outcome = r.stop(recycled)  # must NOT kill the real process
        assert outcome.cleanup == CLEANUP_INCOMPLETE
        assert outcome.identity_ambiguous is True
        assert outcome.signalled is False
        assert procutil.is_alive(h) is True  # still alive — we refused to touch it
    finally:
        r.stop(h)
    assert wait_dead(h)


def test_stop_on_never_started_handle_is_noop():
    outcome = runner().stop(ProcessHandle(pid=2_000_000_000, pid_start_time=1.0))  # no such pid
    assert outcome.verified and not outcome.signalled


def test_orphans_empty(tmp_path):
    assert runner().orphans() == []


def test_pgid_of_dead_pid_is_none():
    # getpgid on a nonexistent pid raises → process_group_of swallows it to None.
    # Lives in procutil now rather than on the runner: the readiness probe needs
    # the same answer to tell our listener from a squatter, and two copies of an
    # OS-boundary call is exactly the duplication that drifts.
    from rentctl.core import procutil as pu

    assert pu.process_group_of(2_000_000_000) is None


# --- the handle: current and legacy shapes (ADR-0016 §2, §12, §14) ---------

def test_legacy_handle_maps_to_its_leaders_session():
    """A 1.0.x handle has only {pid, pid_start_time}. 1.0.x spawned the shell with
    start_new_session=True, so the shell WAS the session leader: sid = pid."""
    legacy = ProcessHandle.from_dict({"pid": 4242, "pid_start_time": 123.5})
    ident = legacy.identity()
    assert (ident.sid, ident.owner_start, ident.owner) == (4242, 123.5, "workload")
    assert ident.exclude == frozenset()
    # Round-trips in legacy format, so a renewed 1.0.x lease stays readable by
    # the 1.0.x watchdog still babysitting it.
    assert legacy.to_dict() == {"pid": 4242, "pid_start_time": 123.5}


def test_extended_handle_round_trips_and_excludes_a_supervisor():
    h = ProcessHandle(
        pid=11, pid_start_time=2.0, sid=10, sid_owner_start_time=1.0, sid_owner=SID_OWNER_SUPERVISOR
    )
    assert ProcessHandle.from_dict(h.to_dict()) == h
    ident = h.identity()
    assert (ident.sid, ident.owner_start) == (10, 1.0)
    assert ident.exclude == frozenset({10})  # the supervisor is never its own member


# --- the runner against the fake table: deterministic branches -------------

def test_runner_alive_is_membership_not_the_leader():
    """The leader (pid 500) is gone; a member it left behind is not. 1.0.x read
    this as dead — `reconcile` CLEANed it and the watchdog exited DEAD (S1)."""
    t = FakeProcessTable()
    t.add(501, sid=500, start=101.0)
    r = ProcessRunner(table=t)
    h = ProcessHandle(pid=500, pid_start_time=100.0)
    assert r.alive(h) is True
    t.kill_now(501)
    assert r.alive(h) is False


def test_runner_stop_escalates_to_sigkill_deterministic():
    """The escalation branch: TERM leaves the member alive → per-pid SIGKILL."""
    t = FakeProcessTable()
    t.add(501, sid=500, start=101.0, ignores_term=True)
    r = ProcessRunner(term_grace_s=0.05, kill_grace_s=1.0, rescan_s=0.01, table=t)
    outcome = r.stop(ProcessHandle(pid=500, pid_start_time=100.0))
    assert outcome.verified and outcome.escalated
    assert t.signals_to(501) == [signal.SIGTERM, signal.SIGKILL]


def test_runner_stop_never_signals_a_group():
    """Recovery mode signals members one pid at a time. There is no killpg from
    outside the session: S is a remembered number there (ADR-0016 §3)."""
    t = FakeProcessTable()
    t.add(500, sid=500, start=100.0)
    t.add(501, sid=500, start=101.0)
    r = ProcessRunner(term_grace_s=0.05, kill_grace_s=0.1, rescan_s=0.01, table=t)
    assert r.stop(ProcessHandle(pid=500, pid_start_time=100.0)).verified
    assert sorted(t.sent) == [(500, signal.SIGTERM), (501, signal.SIGTERM)]
    assert all(pid > 0 for pid, _ in t.sent)


# --- stop_outcome: every caller reads what the stop achieved -------------------

class _LegacyRunner:
    """A runner written against the 1.0 interface: stop() returns None."""

    name = "process"

    def __init__(self, alive_after: bool) -> None:
        self.alive_after = alive_after

    def stop(self, handle):
        return None

    def alive(self, handle):
        return self.alive_after


def test_stop_outcome_takes_a_runners_outcome_at_its_word():
    class Reporting(_LegacyRunner):
        def stop(self, handle):
            return StopOutcome(cleanup=CLEANUP_INCOMPLETE, escalated=True)

    assert stop_outcome(Reporting(alive_after=False), None).cleanup == CLEANUP_INCOMPLETE


def test_stop_outcome_asks_a_legacy_runner_whether_it_worked():
    assert stop_outcome(_LegacyRunner(alive_after=False), None).verified
    assert not stop_outcome(_LegacyRunner(alive_after=True), None).verified


# --- runner factory -------------------------------------------------------

def test_get_runner_process():
    assert isinstance(get_runner("process"), ProcessRunner)


def test_get_runner_compose_not_built():
    with pytest.raises(DevctlError) as ei:
        get_runner("compose")
    assert ei.value.code == REGISTRY_INVALID


def test_get_runner_unknown():
    with pytest.raises(DevctlError) as ei:
        get_runner("nonsense")
    assert ei.value.code == REGISTRY_INVALID
