"""Integration scenarios with REAL processes (spec §10).

These spawn actual ``http.server`` and shell processes on real (ephemeral) ports
and drive the full stack — service, process runner, watchdog, reconcile. The
three the kickoff charter names explicitly are here: process-group child kill,
the concurrent-``up`` race, and the PID-recycle refusal.

Run just these with ``-m integration``; skip them with ``-m 'not integration'``.
"""

from __future__ import annotations

import os
import signal
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
from rentctl.core.models import ProcessHandle
from rentctl.core.paths import lease_key
from rentctl.core.registry import BLOCK_SIZE, RegistryProfile
from rentctl.core.runners import ProcessRunner
from rentctl.core.service import Service, _now_local

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
        base = free_port()
        # An ephemeral port in the top `size` would make base+offset exceed
        # 65535, and that raises OverflowError — NOT OSError — so it escaped the
        # retry below and failed the run outright. Rare, load-dependent, and
        # therefore exactly the kind of flake that gets re-run rather than fixed.
        if base + size - 1 > 65535:
            continue
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
    cmd = f'{sys.executable} -m http.server "$PORT" --bind 127.0.0.1'
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
        runner_factory=fast_runner,
        readiness_timeout=10.0,
        watchdog_spawn=lambda p: None,  # tests that need a real watchdog spawn their own
        session_id_fn=lambda: "itest",
    )
    # A fixed cwd, so the lease key is deterministic across calls (ADR-0007).
    cwd = str(tmp_path)
    ctx = SimpleNamespace(
        svc=svc,
        port=port,
        paths=devctl_home,
        project="demo",
        tmp=tmp_path,
        cwd=cwd,
        key=lease_key("demo", cwd),
        lease=devctl_home.lease_file_for("demo", cwd),
    )
    yield ctx
    # Teardown: down every instance, then hard-kill anything left in the block.
    try:
        svc.env_down("demo", all_instances=True)
    except Exception:
        pass
    for p in range(port, port + BLOCK_SIZE):
        owner = procutil.port_owner(p)
        if owner is not None:
            try:
                os.killpg(os.getpgid(owner.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass


# --- up / down / idempotency (spec §10 items 1-2) -------------------------

def test_up_creates_answering_env(integ):
    res = integ.svc.env_up("demo", cwd=integ.cwd)
    assert res["ok"] is True
    assert res["port"] == integ.port
    assert res["already_running"] is False
    lease = Lease.read(integ.lease)
    assert procutil.is_alive(lease.process_handle())
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
    assert wait_until(lambda: not procutil.is_alive(handle))
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

    # Lane A's session end must not touch lane B.
    integ.svc.env_down(cwd=str(lane_a), reason="session-end")
    assert wait_until(lambda: not procutil.is_alive(handle_a))
    assert procutil.is_alive(handle_b) is True
    with socket.create_connection(("127.0.0.1", b["port"]), timeout=2):
        pass

    integ.svc.env_down(cwd=str(lane_b), reason="session-end")
    assert wait_until(lambda: not procutil.is_alive(handle_b))
    assert integ.paths.project_lease_files("demo") == []


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
    """A lease whose PID is alive but start-time is wrong must not be killed."""
    integ.svc.env_up("demo", cwd=integ.cwd)
    lease = Lease.read(integ.lease)
    real_handle = lease.process_handle()

    # Rewrite the lease with the same PID but a bogus start time (simulates the
    # real process having exited and its PID recycled by something unrelated).
    poisoned = ProcessHandle(pid=real_handle.pid, pid_start_time=real_handle.pid_start_time + 9999)
    lease_bad = Lease(
        **{**lease.__dict__, "handle": poisoned.to_dict()}
    )
    lease_bad.write(integ.lease)

    res = integ.svc.env_down("demo", cwd=integ.cwd)
    assert res["was_running"] is False           # alive(poisoned) is False → refused
    assert procutil.is_alive(real_handle) is True  # the real server was NOT killed
    assert not integ.lease.exists()  # but the stale lease was cleaned

    # Real cleanup via the true handle.
    ProcessRunner(term_grace_s=3, kill_grace_s=2).stop(real_handle)
    assert wait_until(lambda: not procutil.is_alive(real_handle))


# --- squatter: delete lease, server survives (spec §10 item 5) ------------

def test_orphaned_server_reported_and_strict_reclaims(integ, write_registry, tmp_path):
    integ.svc.env_up("demo", cwd=integ.cwd)
    lease = Lease.read(integ.lease)
    real_handle = lease.process_handle()

    # Delete the lease but leave the server running → it becomes a squatter.
    integ.lease.unlink()

    ls = integ.svc.env_ls()
    squatters = [e for e in ls["environments"] if e.get("status") == "squatter"]
    assert any(s["port"] == integ.port for s in squatters)

    # A strict-enforcement service reclaims the block port.
    cmd = f'{sys.executable} -m http.server "$PORT" --bind 127.0.0.1'
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
    strict = Service(integ.paths, runner_factory=fast_runner, watchdog_spawn=lambda p: None)
    res = strict.env_sweep()
    assert "killed_squatters" in res
    assert wait_until(lambda: not procutil.is_alive(real_handle))


# --- watchdog expiry kill (spec §10 item 3) -------------------------------

def test_real_watchdog_kills_on_expiry(integ):
    integ.svc.env_up("demo", cwd=integ.cwd)
    lease = Lease.read(integ.lease)
    real_handle = lease.process_handle()

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

def test_sweep_backstops_dead_watchdog(integ):
    integ.svc.env_up("demo", cwd=integ.cwd)
    lease = Lease.read(integ.lease)
    real_handle = lease.process_handle()

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
