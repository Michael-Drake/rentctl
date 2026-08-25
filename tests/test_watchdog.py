"""Tests for the watchdog: single-tick outcomes and the loop (spec §6.2)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from rentctl.core import events as ev
from rentctl.core.events import EventLog
from rentctl.core.leases import Lease
from rentctl.core.paths import lease_key
from rentctl import watchdog
from rentctl.watchdog import CONTINUE, CORRUPT, DEAD, EXPIRED, GONE

from conftest import CDT, Clock, FakeRunner

NOW = datetime(2026, 7, 14, 8, 0, tzinfo=CDT)


KEY = lease_key("webapp", "/x")


def write_lease(
    paths, runner: FakeRunner, *, expires_min: int, project="webapp", cwd="/x", port=5180
) -> Lease:
    pid = runner._next_pid
    runner._next_pid += 1
    runner._alive[pid] = True
    lease = Lease(
        project=project,
        profile="default",
        runner="process",
        handle={"pid": pid, "pid_start_time": float(pid)},
        port=port,
        session="s",
        cwd=cwd,
        created=NOW - timedelta(hours=1),
        expires=NOW + timedelta(minutes=expires_min),
        log="/l",
    )
    lease.write(paths.lease_file_for(project, cwd))
    return lease


def factory(runner):
    return lambda name: runner


def test_watch_once_gone(devctl_home):
    r = FakeRunner()
    assert watchdog.watch_once(KEY, devctl_home, NOW, factory(r)) == GONE


def test_watch_once_continue(devctl_home):
    r = FakeRunner()
    write_lease(devctl_home, r, expires_min=60)
    assert watchdog.watch_once(KEY, devctl_home, NOW, factory(r)) == CONTINUE
    assert devctl_home.lease_file_for("webapp", "/x").exists()


def test_watch_once_dead_removes_lease(devctl_home):
    r = FakeRunner()
    lease = write_lease(devctl_home, r, expires_min=60)
    r.kill_pid(lease.process_handle().pid)  # process died
    assert watchdog.watch_once(KEY, devctl_home, NOW, factory(r)) == DEAD
    assert not devctl_home.lease_file_for("webapp", "/x").exists()


def test_watch_once_expired_stops_and_removes(devctl_home):
    r = FakeRunner()
    lease = write_lease(devctl_home, r, expires_min=-1)  # already expired
    pid = lease.process_handle().pid
    assert watchdog.watch_once(KEY, devctl_home, NOW, factory(r)) == EXPIRED
    assert pid in r.stopped
    assert not devctl_home.lease_file_for("webapp", "/x").exists()


def test_watch_once_corrupt_lease(devctl_home):
    path = devctl_home.lease_file_for("webapp", "/x")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ broken")
    assert watchdog.watch_once(KEY, devctl_home, NOW, factory(FakeRunner())) == CORRUPT


def test_run_loops_until_expired(devctl_home):
    r = FakeRunner()
    write_lease(devctl_home, r, expires_min=3)  # expires 3 min out
    clock = Clock(NOW)
    slept: list[float] = []

    def sleep_fn(sec):
        slept.append(sec)
        clock.advance(minutes=1)  # each tick, a minute passes

    outcome = watchdog.run(
        KEY,
        devctl_home,
        interval=60.0,
        now_fn=clock,
        sleep_fn=sleep_fn,
        runner_factory=factory(r),
        max_ticks=10,
    )
    assert outcome == EXPIRED
    assert len(slept) == 3  # ticked at 0,1,2 min (continue), expired at 3 min
    assert not devctl_home.lease_file_for("webapp", "/x").exists()


def test_run_max_ticks_guard(devctl_home):
    r = FakeRunner()
    write_lease(devctl_home, r, expires_min=10_000)  # never expires in the window
    outcome = watchdog.run(
        KEY,
        devctl_home,
        now_fn=lambda: NOW,
        sleep_fn=lambda s: None,
        runner_factory=factory(r),
        max_ticks=3,
    )
    assert outcome == "max-ticks"


def test_main_smoke(devctl_home, capsys):
    rc = watchdog.main([KEY, "--interval", "0.01"])  # no lease → exits GONE immediately
    assert rc == 0
    assert "exit-lease-gone" in capsys.readouterr().out


# --- event log: the only source of cleanup-layer-3 evidence (spec §8, §11.1 G4) ---

def _events(devctl_home):
    return EventLog(devctl_home.events_file).read()


class StubbornRunner(FakeRunner):
    """Signals, achieves nothing. `ProcessRunner.stop()` returns None and cannot
    report a failed signal, so this is the shape of a real survivor."""

    def stop(self, handle):
        self.stopped.append(handle.pid)


def test_a_survivor_is_not_recorded_as_a_layer_3_kill(devctl_home):
    """This is the ONLY path that produces layer-3 evidence for the pilot gate,
    so asserting `killed=True` on an unverified stop does not merely mislead —
    it manufactures the evidence the gate is scored from."""
    r = StubbornRunner()
    write_lease(devctl_home, r, expires_min=-1)
    verdict = watchdog.watch_once(KEY, devctl_home, NOW, factory(r))
    (rec,) = _events(devctl_home)
    assert rec["killed"] is False
    assert rec["stop_failed"] is True
    assert verdict == watchdog.CONTINUE      # retry next tick, do not call it done


def test_a_survivor_keeps_its_lease(devctl_home):
    """The lease is the only record naming a process still holding a port."""
    r = StubbornRunner()
    write_lease(devctl_home, r, expires_min=-1)
    watchdog.watch_once(KEY, devctl_home, NOW, factory(r))
    assert devctl_home.lease_file(KEY).exists()


def test_expiry_records_a_layer_3_kill(devctl_home):
    r = FakeRunner()
    lease = write_lease(devctl_home, r, expires_min=-1)
    watchdog.watch_once(KEY, devctl_home, NOW, factory(r))
    (rec,) = _events(devctl_home)
    assert rec["event"] == "down"
    assert rec["reason"] == ev.EXPIRY
    assert rec["layer"] == 3
    assert rec["reason_source"] == ev.DECLARED
    assert rec["killed"] is True
    assert rec["op"] == "watchdog"
    assert rec["pid"] == lease.process_handle().pid
    assert rec["port"] == 5180


def test_dead_process_records_a_cleanup_not_a_kill(devctl_home):
    r = FakeRunner()
    lease = write_lease(devctl_home, r, expires_min=60)
    r.kill_pid(lease.process_handle().pid)
    watchdog.watch_once(KEY, devctl_home, NOW, factory(r))
    (rec,) = _events(devctl_home)
    assert rec["reason"] == ev.PROCESS_GONE
    assert rec["layer"] == 3
    assert rec["killed"] is False  # devctl signalled nothing; it only dropped the lease


@pytest.mark.parametrize("setup", ["gone", "corrupt"])
def test_no_event_when_the_watchdog_changed_nothing(devctl_home, setup):
    if setup == "corrupt":
        path = devctl_home.lease_file_for("webapp", "/x")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ broken")
    watchdog.watch_once(KEY, devctl_home, NOW, factory(FakeRunner()))
    assert _events(devctl_home) == []


def test_continue_ticks_record_nothing(devctl_home):
    r = FakeRunner()
    write_lease(devctl_home, r, expires_min=60)
    watchdog.watch_once(KEY, devctl_home, NOW, factory(r))
    assert _events(devctl_home) == []


# --- one watchdog per lease, not per project (ADR-0007 §5) ----------------

def test_watchdog_only_acts_on_its_own_lease(devctl_home):
    """A watchdog born for lane-1 must not expire lane-2's environment."""
    r = FakeRunner()
    mine = write_lease(devctl_home, r, expires_min=-1, cwd="/worktrees/lane-1", port=5180)
    theirs = write_lease(devctl_home, r, expires_min=-1, cwd="/worktrees/lane-2", port=5181)

    outcome = watchdog.watch_once(
        lease_key("webapp", "/worktrees/lane-1"), devctl_home, NOW, factory(r)
    )

    assert outcome == EXPIRED
    assert mine.process_handle().pid in r.stopped
    assert theirs.process_handle().pid not in r.stopped
    assert devctl_home.lease_file_for("webapp", "/worktrees/lane-2").exists()
