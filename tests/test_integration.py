"""Integration scenarios with REAL processes (spec §10).

These spawn actual ``http.server`` and shell processes on real (ephemeral) ports
and drive the full stack — service, process runner, watchdog, reconcile. The
three the kickoff charter names explicitly are here: process-group child kill,
the concurrent-``up`` race, and the PID-recycle refusal.

Run just these with ``-m integration``; skip them with ``-m 'not integration'``.
"""

from __future__ import annotations

import os
import random
import socket
import subprocess
import sys
import threading
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest

from rentctl.core import procutil
from rentctl.core.leases import Lease
from rentctl.core.paths import lease_key
from rentctl.core.registry import BLOCK_SIZE, RegistryProfile
from rentctl.core.runners import ProcessRunner
from rentctl.core.service import Service, _now_local
from suphelp import LOOPSERVE

pytestmark = pytest.mark.integration


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def free_block(size: int = BLOCK_SIZE) -> int:
    """A base port with ``size`` consecutive free ports above it.

    Needed since ADR-0004: a project owns its whole block, so any listener inside
    it with no lease reads as that project's squatter — and under strict
    enforcement gets reclaimed. A test block must therefore be genuinely empty,
    not merely have a free first port. Real registries allocate dedicated blocks;
    only tests have to go looking for one.
    """
    for _ in range(50):
        # Drawn below the ephemeral ranges, for the reason `suphelp.free_block`
        # gives: a block found empty among the ports the kernel hands to every
        # outgoing connection did not stay empty under load. (That also retires
        # the old overflow guard — no base here comes near 65535.)
        base = random.randrange(20000, 32000 - size)
        socks = []
        try:
            for offset in range(size):
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.bind(("127.0.0.1", base + offset))
                socks.append(s)
            return base
        except OSError:
            continue
        finally:
            for s in socks:
                s.close()
    raise RuntimeError(f"no free {size}-port block found")  # pragma: no cover


def wait_until(predicate, timeout: float = 15.0, interval: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def fast_runner(name):
    return ProcessRunner(term_grace_s=3.0, kill_grace_s=2.0)


@pytest.fixture
def integ(devctl_home, write_registry, tmp_path):
    """A real Service over an http.server profile on a free port block."""
    port = free_block()
    cmd = f"'{sys.executable}' '{LOOPSERVE}' \"$PORT\""
    write_registry(
        {
            "projects": {
                "demo": {
                    "block": port,
                    "runner": "process",
                    "profiles": {"default": {"cmd": cmd, "cwd": str(tmp_path), "port_env": "PORT"}},
                }
            }
        }
    )
    svc = Service(
        devctl_home,
        # The grace periods ride in the lease plan to the supervisor (ADR-0016
        # §6), and bound an in-process recovery stop.
        term_grace_s=3.0,
        kill_grace_s=2.0,
        # Deliberately ABOVE the product default of 30s, not below it. At 10s
        # this fixture was stricter than anything rentctl ships, and it failed
        # nine integration tests on GitHub's macOS runners -- not from a bug,
        # but because `python -m http.server` could not finish booting in time.
        # The evidence was START_TIMEOUT with an EMPTY log_tail and a process
        # still alive: nothing had crashed, it simply was not listening yet.
        # That was put down to ~2.2s of Python startup; measured in 1.1.0 it is
        # 0.02s, and the real cost is a ~35s reverse-DNS lookup between bind and
        # listen (tests/loopserve.py), which the workload no longer makes.
        # No test asserts on the timeout path, so the only cost of a generous
        # value is wall-clock on a genuine failure.
        readiness_timeout=60.0,
        session_id_fn=lambda: "itest",
    )
    # A fixed cwd, so the lease key is deterministic across calls (ADR-0007).
    cwd = str(tmp_path)
    ctx = SimpleNamespace(
        svc=svc,
        port=port,
        paths=devctl_home,
        project="demo",
        cmd=cmd,
        tmp=tmp_path,
        cwd=cwd,
        key=lease_key("demo", cwd),
        lease=devctl_home.lease_file_for("demo", cwd),
    )
    yield ctx
    # Teardown: down every instance. Anything that outlives it is caught — and
    # failed — by the `workload_sessions` guard, which tracks every supervisor
    # and every runner-started workload; nothing here signals by port.
    try:
        svc.env_down("demo", all_instances=True)
    except Exception:
        pass


def legacy_env(integ, *, expires_in: timedelta = timedelta(minutes=30), poison: float = 0.0):
    """A 1.0.x environment: a runner-started server and a lease in 1.0.x's shape.

    The runner is what 1.0.x spawned with (its own session), so the handle maps
    to that session exactly as §2 describes. ``poison`` skews the recorded
    start time, which is what a recycled pid looks like from the lease's side.
    """
    runner = fast_runner("process")
    prof = RegistryProfile(cmd=integ.cmd, cwd=str(integ.tmp), port_env="PORT", preferred_offset=0)
    handle = runner.start(prof, integ.port, integ.tmp / "legacy.log")
    assert wait_until(lambda: _answers(integ.port), timeout=30)
    lease = Lease(
        project="demo", profile="default", runner="process",
        handle={"pid": handle.pid, "pid_start_time": handle.pid_start_time + poison},
        port=integ.port, session="old", cwd=integ.cwd, created=_now_local(),
        expires=_now_local() + expires_in, log=str(integ.tmp / "legacy.log"),
    )
    lease.write(integ.lease)
    return handle, lease


def _answers(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


# --- up / down / idempotency (spec §10 items 1-2) -------------------------

def test_up_creates_answering_env(integ):
    res = integ.svc.env_up("demo", cwd=integ.cwd)
    # Carry `res` into the failure message. `env_up` already puts the reason and
    # the server's own log tail in there, and a bare `assert res["ok"] is True`
    # throws all of it away -- which is how nine red integration tests on the
    # macOS runner said nothing at all about why. The first release attempt was
    # diagnosed from an ephemeral port number in an unrelated traceback.
    assert res["ok"] is True, f"env_up failed on port {integ.port}: {res}"
    assert res["port"] == integ.port
    assert res["already_running"] is False
    lease = Lease.read(integ.lease)
    assert procutil.is_alive(lease.process_handle())
    # Owned by a registered supervisor whose session the server was born in.
    assert lease.state == "running" and lease.supervisor.registered
    assert os.getsid(lease.process_handle().pid) == lease.supervisor.pid
    # The port actually answers HTTP.
    with socket.create_connection(("127.0.0.1", integ.port), timeout=2):
        pass
    ls = integ.svc.env_ls()
    demo = next(e for e in ls["environments"] if e["project"] == "demo")
    assert demo["healthy"] is True


def test_down_kills_and_is_idempotent(integ):
    integ.svc.env_up("demo", cwd=integ.cwd)
    handle = Lease.read(integ.lease).process_handle()
    res = integ.svc.env_down("demo", cwd=integ.cwd)
    assert res["was_running"] is True
    assert res["stopped"] is True  # read from the lease the supervisor removed
    assert not procutil.is_alive(handle)
    assert not integ.lease.exists()
    # Down again → idempotent success, not an error.
    again = integ.svc.env_down("demo", cwd=integ.cwd)
    assert again["ok"] is True
    assert again["was_running"] is False


# --- process-group child kill (charter-named, spec §10 item 6) ------------

def test_process_group_child_outlives_parent_is_killed(tmp_path):
    """A grandchild in the same session must die with a process-group kill."""
    r = ProcessRunner(term_grace_s=3.0, kill_grace_s=2.0)
    childfile = tmp_path / "child.pid"
    # Parent python spawns `sleep 300` (same group, no new session) and records
    # its pid, then sleeps. Only a process-GROUP kill catches the sleep child.
    inner = (
        "import subprocess,sys,time; "
        "c=subprocess.Popen(['sleep','300']); "
        "open(sys.argv[1],'w').write(str(c.pid)); "
        "time.sleep(300)"
    )
    cmd = f'{sys.executable} -c "{inner}" "{childfile}"'
    prof = RegistryProfile(cmd=cmd, cwd=str(tmp_path), port_env="PORT", preferred_offset=0)
    handle = r.start(prof, 0, tmp_path / "log")
    try:
        assert wait_until(lambda: childfile.exists() and childfile.read_text().strip(), timeout=10)
        child_pid = int(childfile.read_text().strip())
        assert procutil.observe_start_time(child_pid) is not None  # child alive
        r.stop(handle)
        assert wait_until(lambda: not procutil.is_alive(handle))
        assert wait_until(lambda: procutil.observe_start_time(child_pid) is None)  # child killed too
    finally:
        if not procutil.is_alive(handle):
            pass
        else:  # pragma: no cover - cleanup only on failure
            r.stop(handle)


# --- SIGKILL escalation against a stubborn process (spec §6.1) ------------

def test_stop_escalates_to_sigkill(tmp_path):
    """A group leader that ignores SIGTERM must still be force-killed."""
    r = ProcessRunner(term_grace_s=1.0, kill_grace_s=3.0)
    # sh traps (ignores) TERM and loops forever, so it survives SIGTERM even after
    # its sleep child dies → stop() must escalate to SIGKILL to end the group.
    prof = RegistryProfile(
        cmd="trap '' TERM; while true; do sleep 1; done",
        cwd=str(tmp_path),
        port_env="PORT",
        preferred_offset=0,
    )
    handle = r.start(prof, 0, tmp_path / "log")
    assert wait_until(lambda: procutil.is_alive(handle), timeout=5)
    r.stop(handle)  # a stubborn group leader — stop() must end it either way
    assert wait_until(lambda: not procutil.is_alive(handle), timeout=6)
    # NOTE: whether TERM or the SIGKILL escalation ends it depends on the child's
    # inherited signal disposition, which differs under pytest — so the escalation
    # *branch* is covered deterministically in test_process_runner, not here.


# --- concurrent up race (charter-named, spec §10 item 7) ------------------

def test_concurrent_up_starts_exactly_one(integ):
    results: list[dict] = []
    lock = threading.Lock()

    def call():
        res = integ.svc.env_up("demo", cwd=integ.cwd)
        with lock:
            results.append(res)

    threads = [threading.Thread(target=call) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(r["ok"] for r in results)
    fresh = [r for r in results if r["already_running"] is False]
    running = [r for r in results if r["already_running"] is True]
    assert len(fresh) == 1          # exactly one actually started a server
    assert len(running) == 3        # the rest serialized and saw it running
    # All agree on the same pid and port.
    assert {r["pid"] for r in results} == {fresh[0]["pid"]}
    assert {r["port"] for r in results} == {integ.port}


# --- concurrent worktrees, real processes (ADR-0004 / ADR-0007) -----------

def test_two_worktrees_run_side_by_side(integ, tmp_path):
    """The scenario ADR-0004 adds to spec §10: two lanes, concurrent env_up, two
    distinct ports, both cleanly torn down — against real servers."""
    lane_a, lane_b = tmp_path / "lane-a", tmp_path / "lane-b"
    lane_a.mkdir()
    lane_b.mkdir()

    a = integ.svc.env_up("demo", cwd=str(lane_a))
    b = integ.svc.env_up("demo", cwd=str(lane_b))

    assert a["ok"] and b["ok"]
    assert a["already_running"] is False and b["already_running"] is False
    assert a["port"] != b["port"]
    assert {a["port"], b["port"]} <= set(range(integ.port, integ.port + BLOCK_SIZE))

    # Both actually answer, independently.
    for res in (a, b):
        with socket.create_connection(("127.0.0.1", res["port"]), timeout=2):
            pass

    handle_a = Lease.read(integ.paths.lease_file_for("demo", str(lane_a))).process_handle()
    handle_b = Lease.read(integ.paths.lease_file_for("demo", str(lane_b))).process_handle()

    # Lane A's session end must not touch lane B. The hook returns `pending`
    # at once (R0); lane A's supervisor completes the stop on its own.
    res = integ.svc.env_down(cwd=str(lane_a), reason="session-end")
    assert [d["pending"] for d in res["downed"]] == [True]
    assert wait_until(lambda: not procutil.is_alive(handle_a))
    assert procutil.is_alive(handle_b) is True
    with socket.create_connection(("127.0.0.1", b["port"]), timeout=2):
        pass

    integ.svc.env_down(cwd=str(lane_b), reason="session-end")
    assert wait_until(lambda: not procutil.is_alive(handle_b))
    assert wait_until(lambda: integ.paths.project_lease_files("demo") == [])


def test_concurrent_up_from_distinct_cwds_draws_distinct_ports(integ, tmp_path):
    """The draw happens under the project lock, so a real race cannot hand the
    same port to two lanes."""
    lanes = [tmp_path / f"lane-{i}" for i in range(4)]
    for lane in lanes:
        lane.mkdir()
    results: list[dict] = []
    lock = threading.Lock()

    def call(cwd):
        res = integ.svc.env_up("demo", cwd=str(cwd))
        with lock:
            results.append(res)

    threads = [threading.Thread(target=call, args=(lane,)) for lane in lanes]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(r["ok"] for r in results)
    assert all(r["already_running"] is False for r in results)
    assert len({r["port"] for r in results}) == 4     # no two drew the same port
    assert len({r["pid"] for r in results}) == 4


# --- PID-recycle refusal (charter-named, spec §10 item 8) -----------------

def test_down_refuses_recycled_pid(integ):
    """A lease whose PID is alive but start-time is wrong must not be killed.

    A 1.0.x lease: its handle *is* the session's owner (§2). A schema-2 lease
    names its session by the supervisor instead, which the next test covers.
    """
    # The lease records a bogus start time for a live pid (the real process
    # exited and its pid was recycled by something unrelated, as the lease sees it).
    real_handle, _ = legacy_env(integ, poison=9999.0)
    before = integ.lease.read_bytes()

    res = integ.svc.env_down("demo", cwd=integ.cwd)
    # PID S is held by a process whose start time is not the lease's owner, and
    # session S has members: the number may have been reused, so NOTHING under
    # it can be proved ours (ADR-0016 §2). Refuse, keep the lease, surface it.
    # 1.0.x deleted the lease here — correct only while `alive` looked at the
    # leader alone; now the session is what is judged, and it is not empty.
    assert res["stopped"] is False
    assert res["identity_ambiguous"] is True
    assert procutil.is_alive(real_handle) is True  # the real server was NOT signalled
    assert integ.lease.read_bytes() == before      # not even a stop request
    incomplete = [e for e in integ.svc.events.read() if e["event"] == "cleanup_incomplete"]
    assert incomplete and incomplete[-1]["identity_ambiguous"] is True

    # Real cleanup via the true handle.
    outcome = ProcessRunner(term_grace_s=3, kill_grace_s=2).stop(real_handle)
    assert outcome.verified
    assert not procutil.is_alive(real_handle)
    integ.lease.unlink()


def test_down_never_signals_through_a_supervised_leases_leader_pid(integ):
    """On a schema-2 lease the leader's `pid_start_time` names nothing: the
    session is the supervisor's. Skewing it changes neither who is stopped nor
    how — the stop request goes to the supervisor, which stops its own session."""
    integ.svc.env_up("demo", cwd=integ.cwd)
    lease = Lease.read(integ.lease)
    skewed = dict(lease.handle, pid_start_time=lease.handle["pid_start_time"] + 9999)
    replace_lease = Lease(**{**lease.__dict__, "handle": skewed})
    replace_lease.write(integ.lease)
    res = integ.svc.env_down("demo", cwd=integ.cwd)
    assert res["stopped"] is True
    down = [e for e in integ.svc.events.read() if e["event"] == "down"][-1]
    assert down["actor"] == "supervisor"


# --- squatter: a server with no lease (spec §10 item 5) --------------------

def test_orphaned_server_reported_and_strict_reclaims(integ, write_registry, tmp_path):
    """A leaseless server on the block. Deleting a supervised lease no longer
    makes one — its supervisor reads that as `lease-lost` and stops its own
    session — so the orphan here is started directly, as a 1.0.x one was."""
    real_handle, _ = legacy_env(integ)
    integ.lease.unlink()

    ls = integ.svc.env_ls()
    squatters = [e for e in ls["environments"] if e.get("status") == "squatter"]
    assert any(s["port"] == integ.port for s in squatters)

    # A strict-enforcement service reclaims the block port.
    cmd = f"'{sys.executable}' '{LOOPSERVE}' \"$PORT\""
    write_registry(
        {
            "enforcement": "strict",
            "projects": {
                "demo": {
                    "block": integ.port,
                    "runner": "process",
                    "profiles": {"default": {"cmd": cmd, "cwd": str(tmp_path), "port_env": "PORT"}},
                }
            },
        }
    )
    # The probe is confined to the one port this test's own orphan holds: the
    # block is only free when drawn, other suites on this machine draw from the
    # same ephemeral range, and strict mode SIGTERMs whatever squats a block —
    # so an unconfined strict sweep here could signal a stranger's server.
    strict = Service(
        integ.paths,
        port_owner_fn=lambda port: procutil.port_owner(port) if port == integ.port else None,
    )
    ours = procutil.port_owner(integ.port)
    assert ours is not None and os.getsid(ours.pid) == real_handle.pid
    res = strict.env_sweep()
    # ADR-0016 §11: re-decided under L, then one verified SIGTERM — to our orphan.
    (row,) = res["killed_squatters"]
    assert row["port"] == integ.port and row["pid"] == ours.pid
    assert row["killed"] is True and row["outcome"] == "signalled", row
    assert wait_until(lambda: not procutil.is_alive(real_handle))


# --- watchdog expiry kill (spec §10 item 3) -------------------------------
# 1.1 never spawns a watchdog, but a 1.0.x one may still babysit a legacy lease
# after an upgrade (§14). It keeps working on the fixed runner.

def test_real_watchdog_kills_on_expiry(integ):
    real_handle, lease = legacy_env(integ)

    # Shorten the lease to ~2s from now, then let a real watchdog notice.
    soon = _now_local() + timedelta(seconds=2)
    lease.renewed(soon).write(integ.lease)

    wd = subprocess.Popen(
        [sys.executable, "-m", "rentctl.watchdog", integ.key, "--interval", "0.3"],
        env={**os.environ},
        start_new_session=True,
    )
    try:
        assert wait_until(lambda: not integ.lease.exists(), timeout=20)
        assert wait_until(lambda: not procutil.is_alive(real_handle), timeout=20)
    finally:
        try:
            wd.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - cleanup
            wd.kill()


# --- watchdog dies → sweep backstop (spec §10 item 4) ---------------------

def test_real_watchdog_exits_on_a_supervised_lease(integ):
    """§14: a watchdog that finds a schema-2 lease under its key exits at once
    and touches nothing — the supervisor owns it."""
    integ.svc.env_up("demo", cwd=integ.cwd)
    before = integ.lease.read_bytes()
    wd = subprocess.run(
        [sys.executable, "-m", "rentctl.watchdog", integ.key, "--interval", "0.3"],
        env={**os.environ}, capture_output=True, text=True, timeout=30,
    )
    assert wd.stdout.strip() == "exit-lease-supervised"
    assert integ.lease.read_bytes() == before


def test_sweep_backstops_dead_watchdog(integ):
    real_handle, lease = legacy_env(integ)

    # Spawn a real (slow-tick) watchdog, then kill it — simulating a dead babysitter.
    wd = subprocess.Popen(
        [sys.executable, "-m", "rentctl.watchdog", integ.key, "--interval", "600"],
        env={**os.environ},
        start_new_session=True,
    )
    wd.kill()
    wd.wait(timeout=5)

    # Expire the lease; the watchdog is gone, so only a sweep can clean it (layer 4).
    past = _now_local() - timedelta(minutes=1)
    lease.renewed(past).write(integ.lease)

    res = integ.svc.env_sweep()
    assert any(s["action"] == "expire" for s in res["swept"])
    assert wait_until(lambda: not procutil.is_alive(real_handle))
    assert not integ.lease.exists()
