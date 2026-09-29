"""ADR-0016 plan step 4: the per-lease supervisor, as a REAL process, driven directly.

Each test writes a ``starting`` lease into isolated state, launches
``python -m rentctl.supervisor <key> <generation>`` the way §6 describes, and
then acts on the lease the way a CLI would: a stop request written on disk,
plus the verified SIGWINCH wake. The service is not involved (plan steps 5–7).

Every helper process has a lifetime cap, binds only an ephemeral loopback
port, and is accounted for by the ``workload_sessions`` guard — which counts
the supervisor itself, so a supervisor left running fails the test too.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import replace
from datetime import timedelta

import psutil
import pytest

from rentctl import supervisor as sup_mod
from rentctl.core import events as ev
from rentctl.core import procutil
from rentctl.core.procutil import SUPERVISION_SESSION, SUPERVISION_SUBREAPER
from rentctl.core.events import EventLog
from rentctl.core.leases import Lease, ProcessRef
from rentctl.core.lifecycle import LifecycleAction, decide, generation_of
from rentctl.core.models import SID_OWNER_SUPERVISOR, WorkloadIdentity
from rentctl.core.paths import lease_key
from rentctl.core.registry import RegistryProfile
from rentctl.core.runners import ProcessRunner
from rentctl.core.supervision import (
    RECOVERED_KEPT,
    RECOVERED_STARTUP_FAILED,
    RECOVERED_STOPPED,
    observe,
    recover_lease,
    spawn_supervisor,
    supervisor_alive,
    supervisor_argv,
    wake_supervisor,
)
from suphelp import (
    DAEMON,
    FORKER,
    PLAIN,
    PROJECT,
    PY,
    SERVER,
    Harness,
    answers,
    free_port,
    gone,
    now,
    read_pid,
    wait_until,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def h(devctl_home, tmp_path, workload_sessions):
    harness = Harness(devctl_home, tmp_path, workload_sessions)
    try:
        yield harness
    finally:
        harness.cleanup()


def server_cmd(h: Harness, *extra: str) -> str:
    srv = h.script("server", SERVER)
    return f"'{PY}' '{srv}' '{h.tmp}/srv.pid' {' '.join(extra)}"


def one(rows: list[dict]) -> dict:
    assert len(rows) == 1, rows
    return rows[0]


# --- #1 --------------------------------------------------------------------------

def test_sup_normal_server_and_children_stop(h):
    """#1: an http server and an ordinary child, under `sh`. A stop request
    plus a wake → `stopped`, the lease removed, exactly one
    `down{cleanup: verified, escalated: false}` written by the supervisor."""
    plain = h.script("plain", PLAIN)
    run = h.start(f"{server_cmd(h)} & '{PY}' '{plain}' '{h.tmp}/kid.pid' & wait")
    lease = run.wait_state("running")
    assert lease.readiness == "answered"
    assert lease.supervisor.registered and lease.supervisor.pid == run.pid
    # Plan step 9: the guarantee in effect is on the lease from registration.
    assert lease.supervisor.supervision in (SUPERVISION_SESSION, SUPERVISION_SUBREAPER)
    if sys.platform == "darwin":
        assert lease.supervisor.supervision == SUPERVISION_SESSION
    assert lease.handle["sid"] == run.pid and lease.handle["sid_owner"] == SID_OWNER_SUPERVISOR
    srv, kid = read_pid(h.tmp / "srv.pid"), read_pid(h.tmp / "kid.pid")
    assert os.getsid(srv) == os.getsid(kid) == run.pid  # born into S
    assert answers(run.port)
    up = one(run.events(ev.UP))
    assert up["supervisor_pid"] == run.pid and up["generation"] == run.generation

    t0 = time.monotonic()
    run.request_stop()
    run.wait_gone()
    stop_latency = time.monotonic() - t0

    assert run.wait_exit() == sup_mod.EXIT_OK
    assert run.members() == []
    down = one(run.events(ev.DOWN))
    assert down["cleanup"] == "verified" and down["escalated"] is False
    assert down["actor"] == "supervisor" and down["reason"] == ev.EXPLICIT
    assert down["reason_source"] == ev.DECLARED and down["op"] == "down"
    assert down["layer"] == 1 and down["killed"] is True
    assert stop_latency < 3.0, stop_latency
    print(f"\nstop latency (normal server, wake): {stop_latency * 1000:.0f} ms")


# --- #2 --------------------------------------------------------------------------

def test_sup_term_ignoring_child_after_shell_exit(h):
    """#2: the shell exits at once, leaving the server and a SIGTERM-ignoring
    child orphaned to launchd. Neither is the supervisor's child any more; both
    are still in S, so the stop finds the child and escalates to SIGKILL."""
    plain = h.script("plain", PLAIN)
    run = h.start(
        f"{server_cmd(h)} & '{PY}' '{plain}' '{h.tmp}/ign.pid' ign & sleep 0.3",
        plan_extra={"term_grace_s": 0.5},
    )
    lease = run.wait_state("running")
    ign = read_pid(h.tmp / "ign.pid")
    ign_start = procutil.observe_start_time(ign)
    assert wait_until(lambda: gone(lease.handle["pid"], lease.handle["pid_start_time"]))
    assert os.getsid(ign) == run.pid  # still in S after the shell died

    run.request_stop()
    run.wait_gone()

    assert gone(ign, ign_start)
    down = one(run.events(ev.DOWN))
    assert down["cleanup"] == "verified" and down["escalated"] is True
    assert run.wait_exit() == sup_mod.EXIT_OK


# --- #3 --------------------------------------------------------------------------

def test_sup_launch_parent_exited_before_stop(h):
    """#3: the launch shell exits after 1 s and the server lives on as an orphan.
    In between, the environment is alive — the §10 table KEEPs it (1.0.x would
    have CLEANed it on the dead leader) — and a later stop is verified."""
    run = h.start(f"{server_cmd(h)} & sleep 1")
    lease = run.wait_state("running")
    leader, leader_start = lease.handle["pid"], lease.handle["pid_start_time"]
    assert wait_until(lambda: gone(leader, leader_start), timeout=10)
    assert answers(run.port)
    obs = observe(run.lease(), run.key)
    assert obs.supervisor_alive and obs.membership is procutil.Membership.MEMBERS
    assert decide(run.lease(), obs, now()).action is LifecycleAction.KEEP

    run.request_stop()
    run.wait_gone()
    assert one(run.events(ev.DOWN))["cleanup"] == "verified"
    assert run.members() == []
    assert run.wait_exit() == sup_mod.EXIT_OK


# --- #4 --------------------------------------------------------------------------

def test_sup_child_spawned_during_shutdown_is_stopped(h):
    """#4: on SIGTERM the forker spawns a TERM-ignoring child and exits. The
    child is born into S after the killpg; the re-scan finds it, and
    escalation SIGKILLs it."""
    plain, forker = h.script("plain", PLAIN), h.script("forker", FORKER)
    run = h.start(
        f"{server_cmd(h)} & '{PY}' '{forker}' '{h.tmp}/forker.pid' '{h.tmp}/forked.pid' "
        f"'{plain}' & wait",
        plan_extra={"term_grace_s": 0.5},
    )
    run.wait_state("running")
    read_pid(h.tmp / "forker.pid")

    run.request_stop()
    forked = read_pid(h.tmp / "forked.pid")
    forked_start = procutil.observe_start_time(forked)
    run.wait_gone()

    assert gone(forked, forked_start)
    down = one(run.events(ev.DOWN))
    assert down["cleanup"] == "verified" and down["escalated"] is True
    assert run.wait_exit() == sup_mod.EXIT_OK


# --- #5 --------------------------------------------------------------------------

def test_sup_grace_expires_escalates_to_sigkill(h):
    """#5: the server itself ignores SIGTERM. The 0.5 s grace runs out and the
    supervisor escalates per pid; the event says `escalated: true`."""
    run = h.start(server_cmd(h, "--ignore-term"), plan_extra={"term_grace_s": 0.5})
    run.wait_state("running")
    srv = read_pid(h.tmp / "srv.pid")
    srv_start = procutil.observe_start_time(srv)

    t0 = time.monotonic()
    run.request_stop()
    run.wait_gone()
    elapsed = time.monotonic() - t0

    assert gone(srv, srv_start)
    down = one(run.events(ev.DOWN))
    assert down["escalated"] is True and down["cleanup"] == "verified" and down["killed"] is True
    assert 0.5 <= elapsed < 4.0, elapsed
    print(f"\nstop latency (TERM ignored, 0.5 s grace): {elapsed * 1000:.0f} ms")
    assert run.wait_exit() == sup_mod.EXIT_OK


# --- #6 (real supervisor, seam signaller) -----------------------------------------

def test_supervisor_does_not_exit_while_members_remain(h):
    """#6 / R3: every per-pid signal is refused (the seam stands in for a
    UID-changed child), so a TERM-ignoring child survives. The lease stays
    `cleanup_incomplete` naming it, the supervisor stays alive and retries on
    the backoff with one event per attempt, and once signals work again a
    fresh stop request retries at once and reaches `stopped`."""
    flag = h.tmp / "refuse"
    flag.write_text("on")
    plain = h.script("plain", PLAIN)
    run = h.start(
        f"{server_cmd(h)} & '{PY}' '{plain}' '{h.tmp}/ign.pid' ign & wait",
        plan_extra={"term_grace_s": 0.3, "kill_grace_s": 0.3},
        extra_env={"RENTCTL_TEST_REFUSE_SIGNALS": str(flag)},
    )
    run.wait_state("running")
    ign = read_pid(h.tmp / "ign.pid")

    run.request_stop()
    lease = run.wait_state("cleanup_incomplete")
    assert [s.pid for s in lease.cleanup.survivors] == [ign]
    assert lease.cleanup.attempts == 1
    first = one(run.events(ev.CLEANUP_INCOMPLETE))
    assert first["survivors"][0]["pid"] == ign and first["attempt"] == 1
    assert run.events(ev.DOWN) == []

    # R3: alive and holding S. The first backoff retry lands ~5 s later.
    assert wait_until(lambda: run.lease().cleanup.attempts >= 2, timeout=9)
    assert run.alive() and run.state() == "cleanup_incomplete"
    assert len(run.events(ev.CLEANUP_INCOMPLETE)) == 2

    flag.unlink()
    run.request_stop()  # on cleanup_incomplete: an immediate retry (§8)
    run.wait_gone(timeout=10)
    down = one(run.events(ev.DOWN))
    assert down["cleanup"] == "verified" and down["attempts"] == 3
    assert run.wait_exit() == sup_mod.EXIT_OK
    assert run.members() == []


# --- #7 --------------------------------------------------------------------------

@pytest.mark.parametrize("point", ["after_launch", "before_running_write"])
def test_supervisor_killed_after_launch_leaves_workload_recoverable(h, point):
    """#7, startup half: the supervisor parks at `after_launch` (or just before
    its `running` write) and is SIGKILLed. The workload is still in S, the
    lease is `starting` and registered — its session is on disk — and the §10
    recovery stops it: `startup_failed` (supervisor_lost), members empty."""
    run = h.start(server_cmd(h), extra_env={"RENTCTL_TEST_FAULT": point})
    assert wait_until(lambda: "sid" in (run.lease().handle or {}))
    if point == "before_running_write":
        assert wait_until(lambda: answers(run.port))
        time.sleep(0.3)  # the probe has answered; the supervisor is parked before the write
    run.sigkill()

    lease = run.lease()
    assert lease.state == "starting" and lease.supervisor.registered
    assert lease.ownership() == WorkloadIdentity(run.pid, run.start_time, SID_OWNER_SUPERVISOR)
    assert wait_until(lambda: len(run.members()) >= 1)

    result = recover_lease(run.key, run.generation, paths=h.paths, kill_grace_s=1.0)

    assert result.outcome == RECOVERED_STARTUP_FAILED, result
    after = run.lease()
    assert after.state == "startup_failed" and after.error["phase"] == ev.PHASE_SUPERVISOR_LOST
    assert run.members() == []
    assert run.events(ev.SUPERVISOR_LOST)
    assert one(run.events(ev.UP_FAILED))["phase"] == ev.PHASE_SUPERVISOR_LOST


def test_supervisor_killed_after_register_launched_nothing(h):
    """I1: killed between registration and launch, the supervisor had started
    nothing. Recovery finds the session empty and records `startup_failed`."""
    marker = h.tmp / "launched"
    run = h.start(f"touch '{marker}'; sleep 5", extra_env={"RENTCTL_TEST_FAULT": "after_register"})
    assert wait_until(lambda: run.lease().supervisor is not None and run.lease().supervisor.registered)
    run.sigkill()
    assert not marker.exists()
    assert recover_lease(run.key, run.generation, paths=h.paths).outcome == "cleaned"
    assert run.state() == "startup_failed"


def test_supervisor_killed_while_running_workload_keeps_serving(h):
    """#7, running half: a real SIGKILL of the supervisor. The workload keeps
    serving — a helper crash is not a dev-server outage — recovery marks it
    `unsupervised`, and a later `down` recovers it to `stopped`."""
    run = h.start(server_cmd(h))
    run.wait_state("running")
    run.sigkill()
    time.sleep(0.5)
    assert answers(run.port)
    assert run.members()

    kept = recover_lease(run.key, run.generation, paths=h.paths)
    assert kept.outcome == RECOVERED_KEPT
    assert run.state() == "unsupervised"
    assert answers(run.port)
    assert one(run.events(ev.SUPERVISOR_LOST))["state_before"] == "running"

    stopped = recover_lease(run.key, run.generation, paths=h.paths, reason=ev.EXPLICIT, op="down")
    assert stopped.outcome == RECOVERED_STOPPED and stopped.stop.verified
    assert not run.path.exists()
    assert run.members() == []
    down = one(run.events(ev.DOWN))
    assert down["mode"] == ev.MODE_RECOVERY and down["reason"] == ev.EXPLICIT
    assert down["supervisor_lost"] is True


def test_supervisor_killed_mid_stop_is_resumed_with_its_reason(h):
    """The supervisor dies while `stopping` (fault `mid_stop`). Recovery
    resumes the teardown under the original request's reason."""
    run = h.start(server_cmd(h), extra_env={"RENTCTL_TEST_FAULT": "mid_stop"})
    run.wait_state("running")
    run.request_stop(ev.SESSION_END)
    run.wait_state("stopping")
    run.sigkill()
    assert run.members()

    result = recover_lease(run.key, run.generation, paths=h.paths)
    assert result.outcome == RECOVERED_STOPPED
    assert not run.path.exists() and run.members() == []
    assert one(run.events(ev.DOWN))["reason"] == ev.SESSION_END


# --- #11 -------------------------------------------------------------------------

@pytest.mark.parametrize("case", ["other_generation", "absent", "not_starting"])
def test_stale_supervisor_argv_exits_without_launch(h, case):
    """#11: a supervisor whose argv names a generation the lease does not hold
    (or no lease, or a lease past `starting`) exits without launching
    anything, and leaves the lease byte-identical."""
    marker = h.tmp / "launched"
    key, gen_b, _ = h.write_starting(f"touch '{marker}'; sleep 5")
    path = h.paths.lease_file(key)
    argv_gen = "a" * 32 if case == "other_generation" else gen_b
    if case == "absent":
        path.unlink()
    elif case == "not_starting":
        replace(Lease.read(path), state="stopping").write(path)
    before = path.read_bytes() if path.exists() else None

    proc, _ = h.spawn(key, argv_gen, record_spawned=False)
    assert proc.wait(timeout=15) == sup_mod.EXIT_NOT_LAUNCHED

    assert not marker.exists()
    assert (path.read_bytes() if path.exists() else None) == before


# --- the supervisor and its own group ------------------------------------------------

def test_supervisor_survives_its_own_group_term(h):
    """The owner-mode stop sends SIGTERM to group S, which includes the
    supervisor. Its Python handler absorbs it: the process lives to write the
    `down` and exits 0, not by the signal."""
    run = h.start(server_cmd(h))
    run.wait_state("running")
    run.request_stop()
    run.wait_gone()
    assert one(run.events(ev.DOWN))["cleanup"] == "verified"  # written after the killpg
    assert run.wait_exit() == 0  # not -SIGTERM


def test_external_sigterm_stops_the_environment(h):
    """A SIGTERM from outside is "stop the environment" (§4), recorded as
    `supervisor-terminated`, which maps to no cleanup layer."""
    run = h.start(server_cmd(h))
    run.wait_state("running")
    assert procutil.verified_signal(run.pid, run.start_time, run.pid, signal.SIGTERM) is (
        procutil.SignalResult.SIGNALLED
    )
    run.wait_gone()
    down = one(run.events(ev.DOWN))
    assert down["reason"] == ev.SUPERVISOR_TERMINATED and "layer" not in down
    assert run.wait_exit() == 0


# --- wake, expiry, renewal, lease loss, exit ------------------------------------------

def test_sigwinch_wake_latency(h):
    """§8: the verified wake applies a stop request at once; without it the
    1 s lease stat does, so a lost wake costs at most about a second."""
    woken = h.start(server_cmd(h))
    woken.wait_state("running")
    t0 = time.monotonic()
    woken.request_stop(wake=True)
    assert wait_until(lambda: woken.state() != "running", timeout=5, interval=0.002)
    wake_s = time.monotonic() - t0
    woken.wait_gone()

    (h.tmp / "srv.pid").unlink()
    polled = h.start(server_cmd(h), cwd=h.tmp / "proj2")
    polled.wait_state("running")
    t0 = time.monotonic()
    polled.request_stop(wake=False)
    assert wait_until(lambda: polled.state() != "running", timeout=5, interval=0.002)
    stat_s = time.monotonic() - t0
    polled.wait_gone()

    print(f"\nwake -> stopping: {wake_s * 1000:.1f} ms; "
          f"no wake (1 s stat) -> stopping: {stat_s * 1000:.0f} ms")
    assert wake_s < 0.5, wake_s
    assert stat_s < 2.5, stat_s


def test_expiry_honours_renewal_then_stops(h):
    """The expiry timer reads `expires` from the lease, so a renewal moves it.
    Past the renewed deadline the supervisor stops with reason `expiry`."""
    run = h.start(server_cmd(h), expires_in=timedelta(seconds=4))
    run.wait_state("running")
    run.renew(now() + timedelta(seconds=6))
    time.sleep(4.5)
    assert run.state() == "running"  # the original deadline passed; renewed, still up
    run.wait_gone(timeout=10)
    down = one(run.events(ev.DOWN))
    assert down["reason"] == ev.EXPIRY and down["layer"] == 3 and down["cleanup"] == "verified"
    assert run.wait_exit() == 0


def test_lease_lost_stops_own_session_and_exits(h):
    """§7 last row: the lease is removed from under a running supervisor. It
    stops *its own* session, records `lease-lost`, and exits; it writes nothing."""
    run = h.start(server_cmd(h))
    run.wait_state("running")
    run.path.unlink()
    assert run.wait_exit(timeout=15) == 0
    assert run.members() == [] and not run.path.exists()
    assert one(run.events(ev.DOWN))["reason"] == ev.LEASE_LOST


def test_workload_exiting_on_its_own_is_recorded_process_gone(h):
    """The session empties with no stop: `exited`, removed, `down{process-gone}`."""
    run = h.start(server_cmd(h, "--life", "1.5"))
    run.wait_state("running")
    run.wait_gone(timeout=10)
    down = one(run.events(ev.DOWN))
    assert down["reason"] == ev.PROCESS_GONE and down["killed"] is False
    assert run.wait_exit() == 0


def test_command_that_fails_is_startup_failed(h):
    """A command that exits at once → `startup_failed` kept for its waiter, with
    the error and the log tail, and an `up_failed{phase: readiness}`."""
    run = h.start("echo boom; exit 3")
    lease = run.wait_state("startup_failed")
    assert lease.error["code"] == "START_TIMEOUT" and lease.error["phase"] == ev.PHASE_READINESS
    assert any("boom" in line for line in lease.error["log_tail"])
    failed = one(run.events(ev.UP_FAILED))
    assert failed["phase"] == ev.PHASE_READINESS and failed["cleanup"] == "verified"
    assert run.wait_exit() == 0


def test_daemonizing_command_is_unsupervisable_and_not_killed(h):
    """§5/R2: the command double-forks and setsid()s, so its listener leaves S.
    Positive evidence — S empty, the port answered from outside it by a process
    born after launch — gives UNSUPERVISABLE. The daemon is not signalled.

    This is the `session` guarantee (macOS, or Linux without the subreaper), so
    the supervisor is held at that level on every platform; the Linux subreaper
    variant is `test_daemonizing_command_captured_by_subreaper`."""
    daemon = h.script("daemon", DAEMON)
    run = h.start(
        f"'{PY}' '{daemon}' '{h.tmp}/daemon.pid'", extra_env={"RENTCTL_TEST_NO_SUBREAPER": "1"}
    )
    try:
        lease = run.wait_state("startup_failed")
        pid = read_pid(h.tmp / "daemon.pid")
        start = procutil.observe_start_time(pid)
        assert lease.supervisor.supervision == SUPERVISION_SESSION
        assert lease.error["code"] == "UNSUPERVISABLE"
        assert lease.error["phase"] == ev.PHASE_UNSUPERVISABLE
        assert not gone(pid, start)  # never signalled
        assert answers(run.port)
    finally:
        # The daemon left every session rentctl could own; the test started it,
        # so the test ends it — by pid + start time, never by pid alone.
        pidfile = h.tmp / "daemon.pid"
        if pidfile.exists():
            pid = int(pidfile.read_text())
            start = procutil.observe_start_time(pid)
            if start is not None:
                procutil.verified_signal(pid, start, os.getsid(pid), signal.SIGKILL)


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="Linux only (ADR-0016 §5, L9): needs prctl(PR_SET_CHILD_SUBREAPER)",
)
def test_daemonizing_command_captured_by_subreaper(h):
    """§5 on Linux: the same double-fork + setsid daemon, under a supervisor
    that is a child subreaper. The daemon leaves S but is reparented to the
    supervisor, not init, so it is a member: the environment is `running`, and
    a stop kills it and verifies the tree empty — no UNSUPERVISABLE, no
    escaped listener. Written on macOS; first executed on the ubuntu CI legs."""
    daemon = h.script("daemon", DAEMON)
    pidfile = h.tmp / "daemon.pid"
    run = h.start(f"'{PY}' '{daemon}' '{pidfile}'")
    try:
        lease = run.wait_state("running")
        assert lease.supervisor.supervision == SUPERVISION_SUBREAPER
        pid = read_pid(pidfile)
        start = procutil.observe_start_time(pid)
        # It left S. Not "it leads its own session": a double-fork daemon's
        # final pid is the SECOND child, a member of the session its first child
        # created — the first CI run of this test (ubuntu) showed exactly that,
        # sid = pid - 1.
        assert os.getsid(pid) != run.pid                     # it left S ...
        assert psutil.Process(pid).ppid() == run.pid          # ... into the subreaper (L5)
        assert answers(run.port)

        run.request_stop()
        run.wait_gone()
        assert gone(pid, start)
        down = one(run.events(ev.DOWN))
        assert down["cleanup"] == "verified" and down["actor"] == "supervisor"
        assert not down.get("escaped_listener")
        assert not answers(run.port)
        assert run.wait_exit() == sup_mod.EXIT_OK
    finally:
        if pidfile.exists():
            pid = int(pidfile.read_text())
            start = procutil.observe_start_time(pid)
            if start is not None:  # the test started it; by pid + start time only
                procutil.verified_signal(pid, start, os.getsid(pid), signal.SIGKILL)


# --- R0: the recovery supervisor ---------------------------------------------------------

def test_recovery_supervisor_stops_a_legacy_workload(h, workload_sessions):
    """R0: a 1.0.x-shaped lease (the shell its own session leader, no
    supervisor). `--recover` adopts its session under a recovery claim and
    stops it, including a TERM-ignoring child; one `down{mode: recovery}`."""
    plain = h.script("plain", PLAIN)
    cwd = h.tmp / "legacy"
    cwd.mkdir()
    port = free_port()
    prof = RegistryProfile(
        cmd=f"{server_cmd(h)} & '{PY}' '{plain}' '{h.tmp}/ign.pid' ign & wait",
        cwd=str(cwd), port_env="PORT", preferred_offset=0,
    )
    handle = ProcessRunner().start(prof, port, h.tmp / "legacy.log")  # tracked by the guard
    ign = read_pid(h.tmp / "ign.pid")
    ign_start = procutil.observe_start_time(ign)
    key = lease_key(PROJECT, str(cwd))
    t = now()
    legacy = Lease(
        project=PROJECT, profile="default", runner="process",
        handle={"pid": handle.pid, "pid_start_time": handle.pid_start_time},
        port=port, session="test", cwd=str(cwd), created=t, expires=t + timedelta(hours=1),
        log=str(h.tmp / "legacy.log"),
    )
    legacy.write(h.paths.lease_file(key))

    ref = spawn_supervisor(
        key, generation_of(legacy), paths=h.paths, recover=True, reason=ev.SESSION_END, op="down",
    )
    workload_sessions.append(WorkloadIdentity(ref.pid, ref.start_time))
    assert wait_until(lambda: not h.paths.lease_file(key).exists(), timeout=25)

    assert gone(ign, ign_start)
    scan = procutil.session_scan(handle.pid, handle.pid_start_time)
    assert scan.state is procutil.Membership.EMPTY
    downs = [e for e in EventLog(h.paths.events_file).read(project=PROJECT) if e["event"] == ev.DOWN]
    down = one(downs)
    assert down["mode"] == ev.MODE_RECOVERY and down["reason"] == ev.SESSION_END
    assert down["escalated"] is True and down["cleanup"] == "verified"
    assert wait_until(lambda: gone(ref.pid, ref.start_time))


# --- the in-process API: spawn, liveness, wake ---------------------------------------------

def test_spawn_supervisor_names_a_detached_session_leader(h, workload_sessions):
    key, _, _ = h.write_starting("sleep 30")
    ref = spawn_supervisor(key, "f" * 32, paths=h.paths)  # a stale generation: exits at once
    workload_sessions.append(WorkloadIdentity(ref.pid, ref.start_time))
    assert ref.registered is False and ref.start_time > 0
    assert wait_until(lambda: gone(ref.pid, ref.start_time))
    assert Lease.read(h.paths.lease_file(key)).supervisor is None


def test_wake_and_liveness_refuse_a_decoy(tmp_path):
    """§3: the wake goes only to a process whose start time AND cmdline name
    this lease's supervisor. A decoy with a SIGWINCH handler, whose cmdline
    even contains the module name, counts every delivery; it must count none."""
    counter = tmp_path / "winch"
    decoy = subprocess.Popen(
        [PY, "-c",
         "import signal, sys, time\n"
         f"signal.signal(signal.SIGWINCH, lambda *a: open({str(counter)!r}, 'a').write('x'))\n"
         "time.sleep(20)\n",
         "rentctl.supervisor"],
        start_new_session=True,
    )
    try:
        start = procutil.observe_start_time(decoy.pid)
        key = lease_key(PROJECT, str(tmp_path))
        assert not supervisor_alive(ProcessRef(decoy.pid, start), key)  # its cmdline lacks the key
        assert not wake_supervisor(ProcessRef(decoy.pid, start), key)
        assert not wake_supervisor(ProcessRef(decoy.pid, start + 100.0), key)  # wrong start time
        assert not wake_supervisor(ProcessRef(0, 1.0), key)
        assert not wake_supervisor(ProcessRef(os.getpid(), 1.0), key)
        time.sleep(0.2)
        assert not counter.exists()
    finally:
        decoy.kill()
        decoy.wait()


def test_supervisor_argv_carries_key_and_generation_last():
    argv = supervisor_argv("p--abc", "g1", recover=True, reason="session-end", op="down")
    assert argv[-2:] == ["p--abc", "g1"] and "--recover" in argv
    assert "rentctl.supervisor" in argv
