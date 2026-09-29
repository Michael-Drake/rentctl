"""Service-layer tests: the four operations' decision logic, driven through the
fake supervision seam (``fakesup.FakeSupervision``) and a controllable clock so
every branch is fast and deterministic.

Since ADR-0016 the service starts and stops nothing itself: it writes lease
transitions, spawns and wakes supervisors, and waits — holding no lock — for
the outcome they write. The fake supervisors run the REAL lifecycle over a fake
process table; no real process is scanned or signalled here except the
disposable watchdog stand-ins in the WI-0069 section.

Real-process end-to-end behaviour lives in test_integration.py and
test_service_supervised.py.
"""

from __future__ import annotations

import json
import shutil
import signal
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psutil
import pytest

from fakesup import FakeSupervision
from rentctl.core import lifecycle, procutil, supervision
from rentctl.core import service as service_mod
from rentctl.core.errors import (
    BLOCK_EXHAUSTED,
    CLEANUP_INCOMPLETE,
    CWD_ESCAPES_ROOT,
    INVALID_CWD,
    PROFILE_MISMATCH,
    REGISTRY_INVALID,
    START_TIMEOUT,
    STATE_WRITE_FAILED,
    STOP_IN_PROGRESS,
    SUPERVISOR_START_FAILED,
    UNKNOWN_PROJECT,
    UNSUPPORTED_ENVIRONMENT,
    DevctlError,
)
from rentctl.core.leases import Lease, SupervisorRef
from rentctl.core.lifecycle import Actor, ActorKind, Event, EventKind, new_starting_lease, transition
from rentctl.core.models import ProcInfo, Readiness
from rentctl.core.paths import lease_key
from rentctl.core.service import Service

CDT = timezone(timedelta(hours=-5))


class Clock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw):
        self.now = self.now + timedelta(**kw)


@pytest.fixture
def clock():
    return Clock(datetime(2026, 7, 14, 8, 0, tzinfo=CDT))


@pytest.fixture
def world(devctl_home, clock):
    return FakeSupervision(devctl_home, clock)


def make_service(paths, world, clock, **kw) -> Service:
    kw.setdefault("session_id_fn", lambda: "sess-1")
    return Service(
        paths, now_fn=clock, supervision=world, term_grace_s=0.05, kill_grace_s=0.05, **kw
    )


@pytest.fixture
def service(devctl_home, write_registry, sample_registry_data, world, clock, monkeypatch):
    write_registry(sample_registry_data)

    # Nobody is listening, unless a test says otherwise.
    #
    # Without this the port draw probes the REAL machine, so these tests assert
    # against whatever happens to hold a port on the developer's Mac — and the
    # sample registry hands them webapp's own block (5180), which means the
    # suite fails exactly when the pilot project is in use. Observed 2026-07-31:
    # 13 tests went red mid-session with no code change, because webapp started
    # two dev servers on 5180 and 5181.
    #
    # Set here in fixture SETUP rather than as an injected `port_owner_fn`, so a
    # test that fakes a squatter in its own body still wins — its `setattr` runs
    # after this one, and an injected callable would have outranked it.
    monkeypatch.setattr(procutil, "port_owner", lambda port: None)

    # …and nothing answers a socket either. `env_ls`'s `healthy` opens a real
    # connection, which is a second machine dependency on a different path: with
    # webapp's dev server live on 5180, a fake environment reported itself
    # healthy because *something else* answered.
    monkeypatch.setattr(service_mod, "_port_answering", lambda port: False)
    return make_service(devctl_home, world, clock)


def lease_at(paths, cwd, project="webapp") -> Lease | None:
    return Lease.read_if_exists(paths.lease_file_for(project, cwd))


def events(service):
    return service.events.read()


def kinds(service) -> list[str]:
    return [e["event"] for e in events(service)]


# --- env_up: the §6 protocol ------------------------------------------------

def test_up_fresh(service, devctl_home, world):
    res = service.env_up("webapp", cwd="/proj/webapp")
    assert res["ok"] is True
    assert res["already_running"] is False
    assert res["port"] == 5180
    assert res["url"] == "http://localhost:5180"
    assert res["pid"] == 1000
    assert res["state"] == "running"
    lease = lease_at(devctl_home, "/proj/webapp")
    assert lease.schema == 2 and lease.state == "running"
    assert lease.session == "sess-1"
    assert lease.cwd == "/proj/webapp"
    # The supervisor is spawned for the lease key, not the project (ADR-0007 §5),
    # and it is the lease's registered owner.
    sup = world.sup_for(lease_key("webapp", "/proj/webapp"))
    assert lease.supervisor == SupervisorRef(sup.pid, sup.start, registered=True)
    assert res["supervisor_pid"] == sup.pid
    # New leases never name a watchdog: the schema-2 file carries the poison.
    assert json.loads(devctl_home.lease_file_for("webapp", "/proj/webapp").read_text())[
        "watchdog_pid"
    ] == "supervised"


def test_up_writes_the_starting_lease_before_spawning(service, devctl_home, world):
    """I1: the record exists, with its generation and plan, before anything is
    spawned — so there is no window in which a workload has no lease (S2)."""
    seen: list[Lease] = []
    real_spawn = world.spawn

    def spawn(key, generation, *, paths):
        seen.append(Lease.read(paths.lease_file(key)))
        return real_spawn(key, generation, paths=paths)

    world.spawn = spawn
    service.env_up("webapp", cwd="/proj/webapp")
    (at_spawn,) = seen
    assert at_spawn.state == "starting"
    assert at_spawn.supervisor is None
    assert len(at_spawn.generation) == 32
    assert at_spawn.plan["cmd"] == "npm run dev"
    assert at_spawn.plan["port_env"] == "PORT"
    assert at_spawn.plan["readiness_timeout_s"] == 30.0
    assert at_spawn.plan["term_grace_s"] == 0.05


def test_up_does_not_write_the_supervisors_up_event_again(service):
    """The supervisor writes `up` when it reaches running; the waiter reads it."""
    service.env_up("webapp", cwd="/proj/webapp")
    assert kinds(service) == ["up"]


def test_up_profile_port(service):
    res = service.env_up("webapp", profile="api-only")
    assert res["port"] == 5181


def test_up_already_running_renews(service, clock, world):
    first = service.env_up("webapp")
    clock.advance(minutes=30)
    second = service.env_up("webapp", lease_minutes=120)
    assert second["already_running"] is True
    assert second["pid"] == first["pid"]
    assert len(world.started) == 1  # not restarted
    assert second["lease_expires"] > first["lease_expires"]  # pushed out


def test_up_replaces_a_lease_whose_workload_exited(service, world):
    """The supervisor notices its session emptied, records `process-gone` and
    removes the lease; the next `up` starts fresh."""
    service.env_up("webapp")
    world.kill_workload(1000)
    world.tick()
    res = service.env_up("webapp")
    assert res["already_running"] is False
    assert res["pid"] == 1001
    assert len(world.started) == 2
    gone = [e for e in events(service) if e["event"] == "down"]
    assert gone[-1]["reason"] == "process-gone" and gone[-1]["actor"] == "supervisor"


def test_up_cleans_a_dead_supervisors_empty_lease_and_starts_fresh(service, world):
    """§10: supervisor dead, session empty → CLEAN, recorded by `up` itself."""
    service.env_up("webapp", cwd="/proj/A")
    key = lease_key("webapp", "/proj/A")
    world.kill_supervisor(key)
    world.kill_workload(1000)
    res = service.env_up("webapp", cwd="/proj/A")
    assert res["already_running"] is False and res["pid"] == 1001
    down = [e for e in events(service) if e["event"] == "down"][-1]
    assert down["reason"] == "sweep-dead" and down["actor"] == "up"
    assert "supervisor_lost" in kinds(service)


def test_up_routes_around_a_squatted_port(service, monkeypatch):
    """A squatter no longer blocks the start — the draw skips it. Still never
    killed; the port is simply not drawn (F7 preserved, ADR-0004)."""
    monkeypatch.setattr(
        procutil,
        "port_owner",
        lambda port: ProcInfo(pid=777, name="node", cmdline=()) if port == 5180 else None,
    )
    res = service.env_up("webapp")
    assert res["ok"] is True
    assert res["port"] == 5181


def test_up_block_exhausted_names_the_holders(service, monkeypatch):
    monkeypatch.setattr(
        procutil, "port_owner", lambda port: ProcInfo(pid=700 + port % 10, name="node", cmdline=())
    )
    res = service.env_up("webapp")
    assert res["ok"] is False
    assert res["error"] == BLOCK_EXHAUSTED
    assert res["block"] == 5180
    assert len(res["holders"]) == 10
    assert "no rentctl lease" in res["holders"]["5180"]


def test_up_start_timeout(service, devctl_home, world):
    """The supervisor's readiness failed: it stopped its own session (verified)
    and wrote `startup_failed`; the waiter reads the error and consumes it."""
    world.readiness = "not_listening"
    res = service.env_up("webapp", cwd="/proj/webapp")
    assert res["ok"] is False
    assert res["error"] == START_TIMEOUT
    assert res["log_tail"]  # captured tail returned for diagnosis
    assert world.stopped == [1000]  # the failed workload was stopped
    assert lease_at(devctl_home, "/proj/webapp") is None  # consumed, not left behind
    assert "cleanup" not in res  # the stop verified: nothing to report
    # One up_failed, written by the supervisor — not a second one by the waiter.
    (failed,) = [e for e in events(service) if e["event"] == "up_failed"]
    assert failed["phase"] == "readiness" and failed["cleanup"] == "verified"


def test_up_start_timeout_names_survivors_of_its_own_stop(service, devctl_home, world):
    """A startup teardown that leaves survivors keeps the lease as
    `cleanup_incomplete{phase: startup}`, and the envelope names them."""
    world.readiness = "not_listening"
    world.stubborn = True
    res = service.env_up("webapp", cwd="/proj/webapp")
    assert res["error"] == START_TIMEOUT
    assert res["cleanup"] == "incomplete"
    assert res["state"] == "cleanup_incomplete"
    assert [s["pid"] for s in res["survivors"]] == [1000]
    lease = lease_at(devctl_home, "/proj/webapp")
    assert lease.state == "cleanup_incomplete" and lease.cleanup.phase == "startup"


def test_up_exited_during_startup(service, world):
    world.readiness = "dies"
    res = service.env_up("webapp", cwd="/proj/webapp")
    assert res["error"] == START_TIMEOUT
    assert "exited during startup" in res["message"]


def test_up_unknown_project(service):
    res = service.env_up("ghost")
    assert res["ok"] is False
    assert res["error"] == UNKNOWN_PROJECT


def test_up_bad_registry(devctl_home, world, clock):
    # No registry file written → fail closed.
    svc = make_service(devctl_home, world, clock)
    res = svc.env_up("webapp")
    assert res["ok"] is False
    assert res["error"] == REGISTRY_INVALID
    assert world.sups == {}


def test_up_clamps_lease_minutes(service, clock):
    res = service.env_up("webapp", lease_minutes=10_000)
    expires = datetime.fromisoformat(res["lease_expires"])
    assert expires == clock.now + timedelta(minutes=480)  # clamped to max


def test_up_spawn_failure_removes_the_starting_lease(service, devctl_home, world):
    world.spawn_error = DevctlError(SUPERVISOR_START_FAILED, "fork failed")
    res = service.env_up("webapp", cwd="/proj/webapp")
    assert res["ok"] is False and res["error"] == SUPERVISOR_START_FAILED
    assert lease_at(devctl_home, "/proj/webapp") is None
    (failed,) = [e for e in events(service) if e["event"] == "up_failed"]
    assert failed["phase"] == "spawn" and failed["error"] == SUPERVISOR_START_FAILED


def test_up_state_write_failure_starts_nothing(service, devctl_home, world, monkeypatch):
    def refuse(self, path):
        raise OSError("disk full")

    monkeypatch.setattr(Lease, "write", refuse)
    res = service.env_up("webapp", cwd="/proj/webapp")
    assert res["error"] == STATE_WRITE_FAILED
    assert world.sups == {}  # §6 step 1: nothing was spawned
    (failed,) = [e for e in events(service) if e["event"] == "up_failed"]
    assert failed["phase"] == "state_write"


def test_up_supervisor_that_dies_before_registering_is_abandoned(service, devctl_home, world):
    """§6 failure table row 3: a supervisor dead before registering launched
    nothing (I1). The waiter's liveness check lets §10 abandon the record."""
    real_spawn = world.spawn

    def spawn_then_die(key, generation, *, paths):
        ref = real_spawn(key, generation, paths=paths)
        world.kill_supervisor(key)
        return ref

    world.spawn = spawn_then_die
    res = service.env_up("webapp", cwd="/proj/webapp")
    assert res["ok"] is False and res["error"] == START_TIMEOUT
    assert "cancelled" in res["message"]
    assert world.started == []
    assert lease_at(devctl_home, "/proj/webapp") is None
    registration = [e for e in events(service) if e.get("phase") == "registration"]
    assert registration and registration[0]["event"] == "up_failed"


def test_up_that_never_reaches_running_asks_its_supervisor_to_stop(service, devctl_home, world):
    """Past the wait budget the supervisor is alive but stuck: the waiter writes
    `stop{reason: startup-abandoned}` and reports where the lease is."""
    world.hung = True
    res = service.env_up("webapp", cwd="/proj/webapp")
    assert res["error"] == START_TIMEOUT
    assert res["state"] == "starting"
    lease = lease_at(devctl_home, "/proj/webapp")
    assert lease.stop.reason == "startup-abandoned"
    world.hung = False
    world.settle()
    # It never registered, so it refuses to register over the recorded stop and exits
    # having launched nothing (I1); the next reconcile abandons the record.
    assert world.started == []
    service.env_sweep()
    assert lease_at(devctl_home, "/proj/webapp") is None


# --- concurrent `up` (§14) --------------------------------------------------

def _start_elsewhere(paths, world, clock, cwd="/proj/webapp", port=5180) -> tuple[str, str]:
    """Another caller's start, left at `starting` with its supervisor spawned."""
    key = lease_key("webapp", cwd)
    gen = "e" * 32
    lease = new_starting_lease(
        generation=gen, project="webapp", profile="default", runner="process", port=port,
        session="other", cwd=cwd, spawn_cwd="/tmp/webapp", log=str(paths.log_file("webapp", "x")),
        plan={"cmd": "npm run dev", "cwd": "/tmp/webapp", "port_env": "PORT"}, now=clock(),
        expires=clock() + timedelta(hours=1),
    )
    ref = world.spawn(key, gen, paths=paths)
    spawned = transition(lease, Event(EventKind.SPAWNED, clock(), supervisor=ref),
                         Actor(ActorKind.CLI, gen, supervision.self_ref()))
    spawned.write(paths.lease_file(key))
    return key, gen


def test_up_on_a_starting_lease_waits_and_reports_already_running(service, devctl_home, world, clock):
    _start_elsewhere(devctl_home, world, clock)
    res = service.env_up("webapp", cwd="/proj/webapp")
    assert res["ok"] is True and res["already_running"] is True
    assert len(world.started) == 1  # the other start, not a second one


def test_up_on_a_starting_lease_reports_its_failure(service, devctl_home, world, clock):
    _start_elsewhere(devctl_home, world, clock)
    world.readiness = "not_listening"
    res = service.env_up("webapp", cwd="/proj/webapp")
    assert res["ok"] is False and res["error"] == START_TIMEOUT


def test_up_on_a_stopping_lease_waits_then_starts_a_fresh_generation(service, devctl_home, world):
    first = service.env_up("webapp", cwd="/proj/webapp")
    before = lease_at(devctl_home, "/proj/webapp")
    service.env_down("webapp", cwd="/proj/webapp", wait_s=0)  # requested, not yet applied
    res = service.env_up("webapp", cwd="/proj/webapp")
    assert res["ok"] is True and res["already_running"] is False
    assert res["pid"] != first["pid"]
    assert lease_at(devctl_home, "/proj/webapp").generation != before.generation


def test_up_on_a_stop_that_never_finishes_is_stop_in_progress(service, devctl_home, world):
    service.env_up("webapp", cwd="/proj/webapp")
    world.hung = True
    service.env_down("webapp", cwd="/proj/webapp", wait_s=0)
    res = service.env_up("webapp", cwd="/proj/webapp")
    assert res["ok"] is False and res["error"] == STOP_IN_PROGRESS
    world.hung = False
    world.settle()


def test_up_on_cleanup_incomplete_refuses_and_names_the_survivors(service, devctl_home, world):
    """Replacing that record would lose the only record naming them (§14)."""
    world.stubborn = True
    service.env_up("webapp", cwd="/proj/webapp")
    service.env_down("webapp", cwd="/proj/webapp")
    res = service.env_up("webapp", cwd="/proj/webapp")
    assert res["ok"] is False and res["error"] == CLEANUP_INCOMPLETE
    assert [s["pid"] for s in res["survivors"]] == [1000]
    assert lease_at(devctl_home, "/proj/webapp").state == "cleanup_incomplete"


def test_up_on_an_unsupervised_lease_reports_it_without_renewing(service, devctl_home, world):
    service.env_up("webapp", cwd="/proj/webapp")
    world.kill_supervisor(lease_key("webapp", "/proj/webapp"))
    service.env_ls()  # the reconciler marks it unsupervised
    before = lease_at(devctl_home, "/proj/webapp")
    res = service.env_up("webapp", cwd="/proj/webapp")
    assert res["ok"] is True and res["already_running"] is True
    assert res["state"] == "unsupervised" and "not renewed" in res["detail"]
    assert lease_at(devctl_home, "/proj/webapp").expires == before.expires


# --- concurrent worktrees (ADR-0007 lease identity + ADR-0004 port draw) ---

def test_two_worktrees_get_two_leases_and_two_ports(service, devctl_home, world):
    """The defect this pair of ADRs exists for: before, lane-2 was handed lane-1's
    server under already_running and never reached port selection."""
    one = service.env_up("webapp", cwd="/worktrees/lane-1")
    two = service.env_up("webapp", cwd="/worktrees/lane-2")

    assert one["already_running"] is False
    assert two["already_running"] is False       # not handed lane-1's server
    assert one["port"] == 5180                   # primary keeps the familiar port
    assert two["port"] == 5181                   # sibling draws the next one
    assert one["pid"] != two["pid"]              # two workloads
    assert len(world.started) == 2
    assert len(devctl_home.project_lease_files("webapp")) == 2


def test_a_lane_teardown_does_not_touch_its_sibling(service, devctl_home, world):
    """The false-kill path: lane-1's ordinary session end used to kill lane-2's
    server, because one lease carried one cwd."""
    one = service.env_up("webapp", cwd="/worktrees/lane-1")
    two = service.env_up("webapp", cwd="/worktrees/lane-2")

    res = service.env_down(cwd="/worktrees/lane-1", reason="session-end")
    # R0: the hook sends the request and returns; the supervisor finishes.
    assert [d["pending"] for d in res["downed"]] == [True]
    world.tick()

    assert one["pid"] in world.stopped
    assert two["pid"] not in world.stopped                # sibling untouched
    assert lease_at(devctl_home, "/worktrees/lane-1") is None
    assert lease_at(devctl_home, "/worktrees/lane-2") is not None


def test_each_lane_sees_its_own_env_as_already_running(service, world):
    service.env_up("webapp", cwd="/worktrees/lane-1")
    service.env_up("webapp", cwd="/worktrees/lane-2")
    again = service.env_up("webapp", cwd="/worktrees/lane-2")
    assert again["already_running"] is True
    assert again["port"] == 5181                          # its own, not lane-1's
    assert len(world.started) == 2                        # nothing restarted


def test_third_lane_draws_the_third_port(service):
    ports = [
        service.env_up("webapp", cwd=f"/worktrees/lane-{i}")["port"] for i in range(1, 4)
    ]
    assert ports == [5180, 5181, 5182]


def test_freed_port_is_reused_by_the_next_lane(service):
    service.env_up("webapp", cwd="/worktrees/lane-1")
    service.env_up("webapp", cwd="/worktrees/lane-2")
    service.env_down(cwd="/worktrees/lane-1")
    assert service.env_up("webapp", cwd="/worktrees/lane-3")["port"] == 5180


def test_eleventh_lane_fails_loud(service):
    for i in range(10):
        assert service.env_up("webapp", cwd=f"/worktrees/lane-{i}")["ok"] is True
    res = service.env_up("webapp", cwd="/worktrees/lane-10")
    assert res["ok"] is False
    assert res["error"] == BLOCK_EXHAUSTED
    assert len(res["holders"]) == 10


def test_a_dead_lanes_port_is_reclaimable(service, world):
    """Free-ness is derived from the lifecycle, not from a stored free-list."""
    first = service.env_up("webapp", cwd="/worktrees/lane-1")
    world.kill_workload(first["pid"])      # crashed; its supervisor notices
    world.tick()
    assert service.env_up("webapp", cwd="/worktrees/lane-2")["port"] == 5180


def test_an_unsupervised_lanes_live_workload_still_holds_its_port(service, world):
    """A dead supervisor is not a dead server: its port is not handed out."""
    service.env_up("webapp", cwd="/worktrees/lane-1")
    world.kill_supervisor(lease_key("webapp", "/worktrees/lane-1"))
    assert service.env_up("webapp", cwd="/worktrees/lane-2")["port"] == 5181


def test_symlinked_cwd_is_one_instance_not_two(service, tmp_path, world):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    first = service.env_up("webapp", cwd=str(real))
    second = service.env_up("webapp", cwd=str(link))
    assert second["already_running"] is True
    assert second["pid"] == first["pid"]
    assert len(world.started) == 1


# --- env_down: stop requests, wake, waiting (§8) ------------------------------

def test_down_project(service, devctl_home, world):
    service.env_up("webapp", cwd="/proj/webapp")
    res = service.env_down("webapp", cwd="/proj/webapp")
    assert res["ok"] is True
    assert res["was_running"] is True
    assert res["stopped"] is True
    assert 1000 in world.stopped
    assert lease_at(devctl_home, "/proj/webapp") is None


def test_down_writes_a_generation_matched_request_and_wakes(service, devctl_home, world):
    service.env_up("webapp", cwd="/proj/webapp")
    lease = lease_at(devctl_home, "/proj/webapp")
    res = service.env_down("webapp", cwd="/proj/webapp", wait_s=0)
    assert res["pending"] is True and res["stopped"] is None and res["state"] == "stopping"
    assert f"supervisor {lease.supervisor.pid}" in res["detail"]
    requested = lease_at(devctl_home, "/proj/webapp")
    assert requested.stop.generation == lease.generation
    assert requested.stop.reason == "explicit" and requested.stop.op == "down"
    assert world.woken == [lease.supervisor.pid]
    (req,) = [e for e in events(service) if e["event"] == "stop_requested"]
    assert req["generation"] == lease.generation
    world.tick()
    assert lease_at(devctl_home, "/proj/webapp") is None


def test_repeated_downs_are_acknowledged_once(service, devctl_home, world):
    """#9: the first request wins; a repeat is acknowledged with no write and
    no event, and the teardown gets exactly one `down`."""
    service.env_up("webapp", cwd="/proj/webapp")
    service.env_down("webapp", cwd="/proj/webapp", wait_s=0)
    service.env_down("webapp", cwd="/proj/webapp", reason="session-end", wait_s=0)
    world.tick()
    assert kinds(service).count("stop_requested") == 1
    (down,) = [e for e in events(service) if e["event"] == "down"]
    assert down["reason"] == "explicit"  # the first reason wins


def test_down_not_running_is_idempotent(service):
    res = service.env_down("webapp")  # never upped
    assert res["ok"] is True
    assert res["was_running"] is False


def test_down_by_project_scopes_to_this_cwd(service, devctl_home, world):
    """An LLM finishing with its own dev server must not reach into a sibling
    lane (ADR-0007 §3)."""
    one = service.env_up("webapp", cwd="/worktrees/lane-1")
    two = service.env_up("webapp", cwd="/worktrees/lane-2")
    service.env_down("webapp", cwd="/worktrees/lane-1")
    assert one["pid"] in world.stopped
    assert two["pid"] not in world.stopped
    assert lease_at(devctl_home, "/worktrees/lane-2") is not None


def test_down_all_instances_is_opt_in(service, devctl_home, world):
    one = service.env_up("webapp", cwd="/worktrees/lane-1")
    two = service.env_up("webapp", cwd="/worktrees/lane-2")
    res = service.env_down("webapp", all_instances=True)
    assert res["ok"] is True
    assert {d["cwd"] for d in res["downed"]} == {"/worktrees/lane-1", "/worktrees/lane-2"}
    assert all(d["stopped"] is True for d in res["downed"])
    assert one["pid"] in world.stopped
    assert two["pid"] in world.stopped
    assert devctl_home.project_lease_files("webapp") == []


def test_down_all_instances_spares_a_project_whose_name_extends_this_one(
    service, devctl_home, write_registry, sample_registry_data, world
):
    """WI-0081: ``webapp--v2`` is a legal, distinct project. Its leases start
    with ``webapp--``, and a prefix glob made ``down webapp --all`` kill it."""
    data = sample_registry_data
    data["projects"]["webapp--v2"] = {
        "block": 5190,
        "runner": "process",
        "profiles": {"default": {"cmd": "npm run dev", "cwd": "/tmp/v2", "port_env": "PORT"}},
    }
    write_registry(data)
    mine = service.env_up("webapp", cwd="/worktrees/lane-1")
    theirs = service.env_up("webapp--v2", cwd="/worktrees/lane-1")
    assert theirs["port"] == 5190  # drew from its own block, not blocked by webapp's

    res = service.env_down("webapp", all_instances=True)
    assert [d["cwd"] for d in res["downed"]] == ["/worktrees/lane-1"]
    assert mine["pid"] in world.stopped
    assert theirs["pid"] not in world.stopped
    assert lease_at(devctl_home, "/worktrees/lane-1", "webapp--v2") is not None


def test_down_all_instances_records_each_as_layer_1(service):
    service.env_up("webapp", cwd="/worktrees/lane-1")
    service.env_up("webapp", cwd="/worktrees/lane-2")
    service.env_down("webapp", all_instances=True)
    downs = [e for e in events(service) if e["event"] == "down"]
    assert len(downs) == 2
    assert {e["layer"] for e in downs} == {1}
    assert {e["reason_source"] for e in downs} == {"declared"}


def test_down_all_by_cwd(service, devctl_home, write_registry, sample_registry_data):
    # Two projects; only one shares the caller cwd.
    sample_registry_data["projects"]["worldcup"] = {
        "block": 5190,
        "runner": "process",
        "profiles": {"default": {"cmd": "npm run dev", "cwd": "/tmp/wc", "port_env": "PORT"}},
    }
    write_registry(sample_registry_data)
    service.env_up("webapp", cwd="/proj/A")
    service.env_up("worldcup", cwd="/proj/B")
    res = service.env_down(cwd="/proj/A")
    assert res["ok"] is True
    assert [d["project"] for d in res["downed"]] == ["webapp"]
    assert lease_at(devctl_home, "/proj/A") is None
    assert lease_at(devctl_home, "/proj/B", "worldcup") is not None  # untouched


def test_session_end_sends_every_request_and_returns_pending(
    service, devctl_home, write_registry, sample_registry_data, world
):
    """R0: `down --all --reason session-end` waits for nothing. Every lease gets
    its request and its wake; the supervisors finish afterwards."""
    sample_registry_data["projects"]["worldcup"] = {
        "block": 5190,
        "runner": "process",
        "profiles": {"default": {"cmd": "npm run dev", "cwd": "/tmp/wc", "port_env": "PORT"}},
    }
    write_registry(sample_registry_data)
    service.env_up("webapp", cwd="/proj/A")
    service.env_up("worldcup", cwd="/proj/A")
    mono = world.monotonic()
    res = service.env_down(cwd="/proj/A", reason="session-end")
    assert world.monotonic() == mono  # never slept: no waiting at all
    assert {d["project"] for d in res["downed"]} == {"webapp", "worldcup"}
    assert all(d["pending"] is True and d["state"] == "stopping" for d in res["downed"])
    assert len(world.woken) == 2
    world.tick()
    assert devctl_home.project_lease_files("webapp") == []
    assert devctl_home.project_lease_files("worldcup") == []


def test_explicit_wait_overrides_the_session_end_default(service, devctl_home):
    service.env_up("webapp", cwd="/proj/A")
    res = service.env_down(cwd="/proj/A", reason="session-end", wait_s=15)
    assert [d["stopped"] for d in res["downed"]] == [True]


def test_down_past_its_budget_is_pending_and_still_ok(service, devctl_home, world):
    service.env_up("webapp", cwd="/proj/A")
    world.hung = True
    res = service.env_down("webapp", cwd="/proj/A")
    assert res["ok"] is True
    assert res["pending"] is True and res["stopped"] is None
    assert world.monotonic() >= service_mod.DOWN_WAIT_S
    world.hung = False
    world.tick()
    assert lease_at(devctl_home, "/proj/A") is None


# --- an empty --cwd is an unexpanded variable, not "no --cwd" (WI-0025) ----

def test_down_refuses_an_empty_cwd(service):
    """Every hook devctl writes interpolates `--cwd "$PROJECT_DIR"`. If that
    variable is unset the shell passes an empty string, and an empty string is
    falsy — so this used to fall back to the hook process's own directory and
    tear down whatever was leased there. Silent, and usually right by accident."""
    service.env_up("webapp", cwd="/proj/A")
    res = service.env_down(cwd="")
    assert res["ok"] is False
    assert res["error"] == INVALID_CWD
    # And critically: nothing was torn down on the way to failing.
    assert service.paths.lease_file_for("webapp", "/proj/A").exists()


def test_up_refuses_an_empty_cwd(service):
    res = service.env_up("webapp", cwd="")
    assert res["ok"] is False
    assert res["error"] == INVALID_CWD


def test_a_whitespace_only_cwd_is_also_refused(service):
    """`--cwd " "` is the same unexpanded-variable accident with a space in the
    hook text; a truthiness check would have let it through."""
    res = service.env_down(cwd="   ")
    assert res["ok"] is False
    assert res["error"] == INVALID_CWD


def test_an_absent_cwd_remains_legal(service):
    """Absence is the documented shape for a runtime with no project-dir
    variable — `session_end_command` omits `--cwd` entirely for those. Refusing
    it would break the very case the omission was designed for."""
    service.env_up("webapp", cwd="/proj/A")
    res = service.env_down(cwd=None, project="webapp")
    assert res["ok"] is True


# --- env_ls ---------------------------------------------------------------

def test_ls_lists_kept(service, world):
    service.env_up("webapp")
    res = service.env_ls()
    assert res["ok"] is True
    envs = {e["project"]: e for e in res["environments"]}
    assert envs["webapp"]["port"] == 5180
    assert envs["webapp"]["healthy"] is False  # fake server binds no real port
    assert envs["webapp"]["state"] == "running"
    assert envs["webapp"]["supervisor"]["alive"] is True
    assert envs["webapp"]["supervisor"]["registered"] is True
    assert "pending" not in envs["webapp"]


def test_ls_shows_a_pending_stop_and_survivors(service, world):
    world.stubborn = True
    service.env_up("webapp", cwd="/proj/A")
    service.env_down("webapp", cwd="/proj/A", wait_s=0)
    (row,) = service.env_ls()["environments"]
    assert row["pending"] is True and row["stop_reason"] == "explicit"
    world.tick()
    (row,) = service.env_ls()["environments"]
    assert row["state"] == "cleanup_incomplete"
    assert [s["pid"] for s in row["survivors"]] == [1000]


def test_ls_cleans_a_dead_supervisors_empty_lease(service, devctl_home, world):
    service.env_up("webapp", cwd="/proj/A")
    world.kill_supervisor(lease_key("webapp", "/proj/A"))
    world.kill_workload(1000)
    res = service.env_ls()
    assert res["environments"] == []
    assert lease_at(devctl_home, "/proj/A") is None  # reconciled away


def test_supervisor_lost_while_running_is_kept_as_unsupervised(service, devctl_home, world):
    """§10: a helper crash must not become a dev-server outage. The workload is
    kept, visibly, and nothing is signalled."""
    service.env_up("webapp", cwd="/proj/A")
    world.kill_supervisor(lease_key("webapp", "/proj/A"))
    (row,) = service.env_ls()["environments"]
    assert row["state"] == "unsupervised"
    assert row["supervisor"]["alive"] is False
    assert world.table.sent == []
    (lost,) = [e for e in events(service) if e["event"] == "supervisor_lost"]
    assert lost["detected_by"] == "ls" and lost["state_before"] == "running"
    # `down` then recovers it in this process, verified.
    res = service.env_down("webapp", cwd="/proj/A")
    assert res["stopped"] is True
    down = [e for e in events(service) if e["event"] == "down"][-1]
    assert down["mode"] == "recovery" and down["actor"] == "cli" and down["reason"] == "explicit"
    assert lease_at(devctl_home, "/proj/A") is None


# --- env_sweep ------------------------------------------------------------

def test_sweep_removes_dead(service, devctl_home, world):
    service.env_up("webapp", cwd="/proj/A")
    world.kill_supervisor(lease_key("webapp", "/proj/A"))
    world.kill_workload(1000)
    res = service.env_sweep()
    assert [s["action"] for s in res["swept"]] == ["clean"]
    assert lease_at(devctl_home, "/proj/A") is None


def test_sweep_expires_an_unsupervised_lease_through_a_recovery_claim(
    service, devctl_home, clock, world
):
    service.env_up("webapp", lease_minutes=120, cwd="/proj/A")
    world.kill_supervisor(lease_key("webapp", "/proj/A"))
    clock.advance(minutes=121)  # past expiry, workload still alive
    res = service.env_sweep()
    assert [s["action"] for s in res["swept"]] == ["expire"]
    assert world.table.signals_to(1000) == [signal.SIGTERM]
    assert lease_at(devctl_home, "/proj/A") is None


def test_a_supervised_lease_expires_by_its_own_supervisor(service, devctl_home, clock, world):
    """Expiry is the supervisor's (layer 3); sweep leaves a live one alone."""
    service.env_up("webapp", lease_minutes=120, cwd="/proj/A")
    clock.advance(minutes=121)
    assert service.env_sweep()["swept"] == []
    world.tick()
    assert lease_at(devctl_home, "/proj/A") is None
    down = [e for e in events(service) if e["event"] == "down"][-1]
    assert down["reason"] == "expiry" and down["layer"] == 3 and down["actor"] == "supervisor"


def test_sweep_nudges_a_hung_supervisor_past_expiry_without_signalling(
    service, devctl_home, clock, world
):
    service.env_up("webapp", lease_minutes=120, cwd="/proj/A")
    world.hung = True
    clock.advance(minutes=121)
    res = service.env_sweep()
    assert res["swept"] == []
    assert lease_at(devctl_home, "/proj/A").stop.reason == "expiry"
    assert world.table.sent == []  # the workload is never signalled from here
    world.hung = False
    world.tick()
    assert lease_at(devctl_home, "/proj/A") is None


def test_sweep_keeps_healthy(service):
    service.env_up("webapp")
    res = service.env_sweep()
    assert res["swept"] == []
    assert [k["project"] for k in res["kept"]] == ["webapp"]


def test_sweep_abandons_a_start_that_never_registered(service, devctl_home, world, clock):
    """#8's seam half: the CLI died before its supervisor registered. I1 means
    nothing was launched, so the record is simply removed after 30 s."""
    key, _ = _start_elsewhere(devctl_home, world, clock)
    world.kill_supervisor(key)
    world.sups.clear()
    Lease.read(devctl_home.lease_file(key))  # still there
    res = service.env_sweep()
    assert [s["action"] for s in res["swept"]] == ["clean"]
    assert lease_at(devctl_home, "/proj/webapp") is None
    assert any(e["event"] == "up_failed" and e["phase"] == "registration" for e in events(service))


def test_starting_lease_without_supervisor_abandoned_after_timeout(service, devctl_home, clock):
    """No supervisor recorded at all: only the registration timeout settles it."""
    key = lease_key("webapp", "/proj/webapp")
    lease = new_starting_lease(
        generation="f" * 32, project="webapp", profile="default", runner="process", port=5180,
        session="s", cwd="/proj/webapp", spawn_cwd="/tmp/webapp", log="/dev/null",
        plan={"cmd": "x", "cwd": "/tmp", "port_env": "PORT"}, now=clock(),
        expires=clock() + timedelta(hours=1),
    )
    lease.write(devctl_home.lease_file(key))
    assert service.env_sweep()["swept"] == []  # inside the timeout: kept
    clock.advance(seconds=31)
    assert [s["action"] for s in service.env_sweep()["swept"]] == ["clean"]


def test_sweep_recovers_a_dead_supervisors_start(service, devctl_home, world, clock):
    """#7's seam half: registered and launched, then the supervisor died. Sweep
    recovers the session and writes `startup_failed`, which is later cleaned."""
    key, _ = _start_elsewhere(devctl_home, world, clock)
    sup = world.sup_for(key)
    real_ready = world.readiness
    world.readiness = "__never__"
    # Register and launch by hand, then lose the supervisor before readiness.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sup, "_teardown", lambda lease, error=None: None)
        sup.step()
    world.readiness = real_ready
    world.kill_supervisor(key)
    assert Lease.read(devctl_home.lease_file(key)).handle["sid"] == sup.pid
    res = service.env_sweep()
    assert [s["action"] for s in res["swept"]] == ["recover"]
    failed = Lease.read(devctl_home.lease_file(key))
    assert failed.state == "startup_failed" and failed.error["phase"] == "supervisor_lost"
    assert world.table.signals_to(1000) == [signal.SIGTERM]
    clock.advance(seconds=31)
    service.env_sweep()
    assert not devctl_home.lease_file(key).exists()


def test_sweep_retries_cleanup_incomplete_when_its_supervisor_is_gone(
    service, devctl_home, world
):
    world.stubborn = True
    service.env_up("webapp", cwd="/proj/A")
    service.env_down("webapp", cwd="/proj/A")
    world.kill_supervisor(lease_key("webapp", "/proj/A"))
    for proc in world.table.procs.values():
        proc.unkillable = proc.ignores_term = False  # the survivor becomes stoppable
    res = service.env_sweep()
    assert [s["action"] for s in res["swept"]] == ["stop"]
    assert lease_at(devctl_home, "/proj/A") is None


def test_ambiguous_identity_is_kept_flagged_and_never_signalled(service, devctl_home, world):
    """PID S reused by a stranger while members remain: keep, surface, no signal."""
    world.stubborn = True
    service.env_up("webapp", cwd="/proj/A")
    service.env_down("webapp", cwd="/proj/A")
    sup = world.kill_supervisor(lease_key("webapp", "/proj/A"))
    world.table.add(sup.pid, 1, sup.start + 999, name="stranger")  # PID S reused
    sent = list(world.table.sent)
    res = service.env_sweep()
    assert res["swept"] == []
    (row,) = res["kept"]
    assert row["identity_ambiguous"] is True
    assert world.table.sent == sent
    assert lease_at(devctl_home, "/proj/A").cleanup.identity_ambiguous is True


# --- denied process inspection is unknown, never "no processes" (Astra P2) -

DENIED = "the process list could not be read: [Errno 1] Operation not permitted"


def test_up_where_process_inspection_is_denied_starts_nothing(service, devctl_home, world):
    """An agent sandbox that refuses sysctl(): no lease, no supervisor, no
    workload — and an error that says where rentctl has to run instead."""
    world.table.denied = DENIED
    res = service.env_up("webapp", cwd="/proj/webapp")
    assert res["ok"] is False and res["error"] == UNSUPPORTED_ENVIRONMENT
    assert "agent's sandbox" in res["message"] and res["reason"] == DENIED
    assert world.sups == {} and world.started == []
    assert lease_at(devctl_home, "/proj/webapp") is None


def test_inspection_lost_while_running_keeps_the_lease(service, devctl_home, world):
    """A scan that cannot run is not an empty session: the supervisor keeps
    supervising instead of recording the workload as gone."""
    service.env_up("webapp", cwd="/proj/A")
    world.table.denied = DENIED
    world.settle(5)
    lease = lease_at(devctl_home, "/proj/A")
    assert lease is not None and lease.state == "running"
    assert "down" not in kinds(service)


def test_inspection_lost_at_stop_never_reports_cleanup_verified(service, devctl_home, world):
    service.env_up("webapp", cwd="/proj/A")
    world.table.denied = DENIED
    sent = list(world.table.sent)
    res = service.env_down("webapp", cwd="/proj/A")
    assert res["stopped"] is False and res["state"] == "cleanup_incomplete"
    assert world.table.sent == sent  # an unknown session is never signalled
    lease = lease_at(devctl_home, "/proj/A")
    assert lease is not None and lease.cleanup.identity_ambiguous is True


def test_inspection_lost_in_recovery_keeps_the_lease(service, devctl_home, world):
    """test_sweep_removes_dead's exact setup, seen through a denied table: the
    workload really is gone, but nothing can prove it, so nothing is removed."""
    service.env_up("webapp", cwd="/proj/A")
    world.kill_supervisor(lease_key("webapp", "/proj/A"))
    world.kill_workload(1000)
    world.table.denied = DENIED
    res = service.env_sweep()
    assert res["swept"] == []
    assert lease_at(devctl_home, "/proj/A") is not None
    world.table.denied = None  # inspection restored: now it is provably empty
    assert [s["action"] for s in service.env_sweep()["swept"]] == ["clean"]


# --- a teardown that did not work must not report one (WI-0031) -----------

@pytest.fixture
def stubborn_service(service, world):
    """A workload whose members survive TERM and KILL — a process in
    uninterruptible sleep (blocked on a mounted volume, which is how this
    machine serves projects), or a child that changed uid."""
    world.stubborn = True
    return service


def test_a_failed_teardown_keeps_the_lease(stubborn_service, devctl_home):
    """The lease is the only record naming the process. Deleting it over a live
    server is precisely what manufactures an unattributable survivor holding a
    port — the failure devctl exists to prevent, reported as a clean teardown."""
    stubborn_service.env_up("webapp", cwd="/proj/A")
    res = stubborn_service.env_down(project="webapp", cwd="/proj/A")
    assert res["stopped"] is False
    assert res["state"] == "cleanup_incomplete"
    assert "survivors" in res["detail"]
    assert [s["pid"] for s in res["survivors"]] == [1000]
    assert lease_at(devctl_home, "/proj/A") is not None


def test_a_failed_teardown_is_not_recorded_as_a_kill(stubborn_service):
    """`killed` used to be the liveness reading taken BEFORE the attempt — a
    claim about intent. False layer-2 evidence is worse than no evidence.

    Since ADR-0016 §13 a failed stop is not a `down` row at all: 1.0.x wrote
    `down{stop_failed: true}` and `summarize` counted it as a teardown."""
    stubborn_service.env_up("webapp", cwd="/proj/A")
    stubborn_service.env_down(project="webapp", cwd="/proj/A")
    found = stubborn_service.events.read()
    assert [e for e in found if e["event"] == "down"] == []
    (incomplete,) = [e for e in found if e["event"] == "cleanup_incomplete"]
    assert incomplete["reason"] == "explicit"
    assert incomplete["identity_ambiguous"] is False
    assert "layer" not in incomplete


def test_sweep_does_not_report_a_survivor_as_swept(stubborn_service, clock, devctl_home, world):
    """Reporting it as swept is how a live server becomes invisible: gone from
    `kept`, gone from the lease dir, and named nowhere."""
    stubborn_service.env_up("webapp", cwd="/proj/A")
    clock.advance(hours=3)
    world.tick()  # its supervisor tries the expiry stop; the survivor stays
    res = stubborn_service.env_sweep()
    assert res["swept"] == []
    assert [k["project"] for k in res["kept"]] == ["webapp"]
    assert res["kept"][0]["state"] == "cleanup_incomplete"
    assert lease_at(devctl_home, "/proj/A") is not None


def test_a_successful_teardown_still_reports_a_kill(service):
    """The honest path must be unchanged — this is the regression guard for the
    fix itself. Since ADR-0016 §8 it also says `stopped: true` in so many words."""
    service.env_up("webapp", cwd="/proj/A")
    res = service.env_down(project="webapp", cwd="/proj/A")
    assert res["stopped"] is True
    down = [e for e in service.events.read() if e["event"] == "down"][-1]
    assert down["killed"] is True
    assert "stop_failed" not in down


# --- a second profile in one cwd is refused, not substituted (WI-0030) ----

def test_a_different_profile_in_the_same_cwd_is_refused(service):
    """The bug this replaces: `up --profile api-only` over a running `default`
    returned ok/already_running with the DEFAULT profile's port and url, and
    `npm run api` never ran. An agent asked for the api server, was told it was
    already up, and debugged the wrong process.

    A lease is keyed on (project, cwd) with no profile component (ADR-0007), so
    one directory holds exactly one environment — this is a request the identity
    model cannot satisfy, and the only honest answers are refuse or re-key."""
    first = service.env_up("webapp", profile="default", cwd="/proj/A")
    assert first["ok"] is True
    res = service.env_up("webapp", profile="api-only", cwd="/proj/A")
    assert res["ok"] is False
    assert res["error"] == PROFILE_MISMATCH
    assert res["running_profile"] == "default"
    assert res["requested_profile"] == "api-only"


def test_the_same_profile_still_renews(service):
    """The refusal must not break the common path — `up` on a live lease of the
    SAME profile is a renewal, which is the whole point of F3."""
    service.env_up("webapp", profile="default", cwd="/proj/A")
    again = service.env_up("webapp", profile="default", cwd="/proj/A")
    assert again["ok"] is True
    assert again["already_running"] is True


def test_the_envelope_always_names_the_profile_serving(service):
    """Half of the defect was that nothing in the output could contradict the
    caller's assumption — there was no profile field at all."""
    fresh = service.env_up("webapp", profile="api-only", cwd="/proj/B")
    assert fresh["profile"] == "api-only"
    renewed = service.env_up("webapp", profile="api-only", cwd="/proj/B")
    assert renewed["profile"] == "api-only"


def test_a_different_profile_in_a_different_cwd_is_fine(service):
    """The refusal is scoped to the directory, not the project — two worktrees
    running two profiles is exactly what per-lease identity is for."""
    service.env_up("webapp", profile="default", cwd="/proj/A")
    other = service.env_up("webapp", profile="api-only", cwd="/proj/B")
    assert other["ok"] is True
    assert other["profile"] == "api-only"


# --- G3's inbound channel (WI-0016, spec §11.1) ---------------------------

def test_a_false_kill_report_is_matched_to_the_teardown_it_disputes(service):
    """The report carries the disputed event, so whoever scores G3 later is not
    correlating timestamps by hand."""
    service.env_up("webapp", cwd="/proj/A")
    service.env_down(project="webapp", cwd="/proj/A")
    res = service.report_false_kill("webapp", note="I was using that")
    assert res["ok"] is True
    assert res["matched"]["event"] == "down"
    assert res["matched"]["project"] == "webapp"


def test_a_report_with_no_matching_teardown_is_still_recorded(service):
    """Never refuses. A complaint channel that can reject the complaint is not a
    complaint channel — and G3 has no other input. An unmatchable report is also
    a real signal: the process may never have been devctl's."""
    res = service.report_false_kill("webapp", note="something vanished")
    assert res["ok"] is True
    assert res["matched"] is None
    assert "No teardown in the log matches" in res["note"]


def test_the_report_reaches_the_gate_summary(service):
    """End-to-end: reporting has to move the number the gate is scored from, or
    the channel is decoration."""
    service.env_up("webapp", cwd="/proj/A")
    service.env_down(project="webapp", cwd="/proj/A")
    service.report_false_kill("webapp", note="killed my server")
    from rentctl.core.events import summarize
    g3 = summarize(service.events.read())["false_kills"]
    assert g3["reported"] == 1
    assert g3["unmatched"] == 0


def test_a_report_narrowed_by_port_matches_that_port(service):
    service.env_up("webapp", cwd="/proj/A")
    service.env_down(project="webapp", cwd="/proj/A")
    assert service.report_false_kill("webapp", note="x", port=9999)["matched"] is None
    assert service.report_false_kill("webapp", note="x", port=5180)["matched"] is not None


# --- the pin's third call site: sweep reports drift (WI-0004, ADR-0003) ---

PINNED_TOML = """
[project]
name = "webapp"
runner = "process"

[profiles.default]
cmd = "npm run dev -- --now-different"
cwd = "."
port_env = "PORT"
"""


def _drifted_registry(sample_registry_data, root):
    """A webapp entry pinned to a command its devctl.toml no longer matches."""
    (root / "devctl.toml").write_text(PINNED_TOML)
    entry = sample_registry_data["projects"]["webapp"]
    entry["source_dir"] = str(root)
    entry["profiles"]["default"]["cmd_sha256"] = "0" * 64
    return sample_registry_data


def test_sweep_reports_a_drifted_command(service, write_registry, sample_registry_data, tmp_path):
    """env_up refuses on drift because it is about to run the changed command.
    Sweep only reports: it runs at session START, so the same fact arrives when
    there is time to act, rather than when someone wanted to begin work."""
    root = tmp_path / "proj"
    root.mkdir()
    write_registry(_drifted_registry(sample_registry_data, root))
    res = service.env_sweep()
    assert res["ok"] is True
    drift = {d["project"]: d for d in res["command_drift"]}
    assert "webapp" in drift
    assert "sync" in drift["webapp"]["fix"]  # names the remedy, not just the fault


def test_sweep_still_reconciles_despite_drift(
    service, devctl_home, write_registry, sample_registry_data, tmp_path, clock, world
):
    """The load-bearing property. Refusing to clean up because a config drifted
    would leave a real server running to guard against a command sweep was never
    going to execute — fail-closed and fail-safe point opposite ways here, and
    cleanup follows fail-safe."""
    service.env_up("webapp", cwd="/proj/A")
    world.kill_supervisor(lease_key("webapp", "/proj/A"))  # so the sweep is what acts
    root = tmp_path / "proj"
    root.mkdir()
    write_registry(_drifted_registry(sample_registry_data, root))
    clock.advance(hours=3)  # past the lease
    res = service.env_sweep()
    assert res["command_drift"]  # drift seen…
    assert [s["project"] for s in res["swept"]] == ["webapp"]  # …and the sweep still ran
    assert lease_at(devctl_home, "/proj/A") is None


def test_sweep_is_silent_when_nothing_drifted(service):
    """Absent on the clean path, so the field cannot decay into noise."""
    assert "command_drift" not in service.env_sweep()


def test_sweep_reports_squatter_advisory(service, monkeypatch):
    # A listener on webapp's block port with no lease → advisory report, no kill.
    monkeypatch.setattr(
        procutil,
        "port_owner",
        lambda port: ProcInfo(pid=888, name="vite", cmdline=()) if port == 5180 else None,
    )
    res = service.env_sweep()
    assert "killed_squatters" not in res
    squats = {s["port"]: s for s in res["squatters"]}
    assert squats[5180]["pid"] == 888
    assert squats[5180]["status"] == "squatter"


def test_sweep_strict_kills_squatter(
    devctl_home, write_registry, sample_registry_data, world, clock, monkeypatch
):
    sample_registry_data["enforcement"] = "strict"
    write_registry(sample_registry_data)
    monkeypatch.setattr(
        procutil,
        "port_owner",
        lambda port: ProcInfo(pid=888, name="vite", cmdline=()) if port == 5180 else None,
    )
    world.table.add(888, 888, 50.0, name="vite")  # a foreign session, no lease
    svc = make_service(devctl_home, world, clock)
    res = svc.env_sweep()
    assert world.table.signals_to(888) == [signal.SIGTERM]  # one TERM, no escalation
    (row,) = res["killed_squatters"]
    assert row["killed"] is True and row["outcome"] == "signalled" and row["port"] == 5180
    (rec,) = [e for e in events(svc) if e["event"] == "squatter_reclaim"]
    assert rec["killed"] is True and rec["pid"] == 888 and rec["signal"] == "SIGTERM"
    assert rec["start_time"] == 50.0 and rec["sid"] == 888
    assert "down" not in kinds(svc)  # a reclaim is never counted as a teardown


# --- ADR-0016 §11 (S4): strict reclaim is re-decided under the project lock ---


def _strict_service(devctl_home, write_registry, sample_registry_data, world, clock):
    sample_registry_data["enforcement"] = "strict"
    write_registry(sample_registry_data)
    return make_service(devctl_home, world, clock)


def test_strict_reclaim_rechecks_start_time_under_lock(
    devctl_home, write_registry, sample_registry_data, world, clock, monkeypatch
):
    """S4 seam: the snapshot saw pid 888 on 5180; by the time the sweep holds
    L, 888 has exited and the pid names a different process. The re-probe
    under L finds the same pid listening but a different start time: no signal.
    The re-probe is asserted to run while L is held."""
    held: list[str] = []
    real_lock = service_mod.project_lock

    @contextmanager
    def instrumented(path):
        with real_lock(path):
            held.append(str(path))
            try:
                yield
            finally:
                held.pop()

    monkeypatch.setattr(service_mod, "project_lock", instrumented)
    world.table.add(888, 888, 50.0, name="vite")
    probes: list[list[str]] = []

    def owner(port):
        if port != 5180:
            return None
        probes.append(list(held))
        if len(probes) == 2:  # the re-probe: 888 was recycled in between
            world.table.kill_now(888)
            world.table.add(888, 888, 9999.0, name="other")
        return ProcInfo(pid=888, name="vite", cmdline=())

    monkeypatch.setattr(procutil, "port_owner", owner)
    svc = _strict_service(devctl_home, write_registry, sample_registry_data, world, clock)
    res = svc.env_sweep()
    assert probes[0] == []  # the snapshot is lock-free…
    assert probes[1] == [str(devctl_home.lock_file("webapp"))]  # …the recheck is under L
    assert world.table.sent == []
    (row,) = res["killed_squatters"]
    assert row["killed"] is False and row["outcome"] == "identity_mismatch"
    (rec,) = [e for e in events(svc) if e["event"] == "squatter_reclaim"]
    assert rec["killed"] is False and rec["outcome"] == "identity_mismatch"
    assert "signal" not in rec


def test_strict_reclaim_identity_change_after_recheck_is_refused_by_verified_signal(
    devctl_home, write_registry, sample_registry_data, world, clock, monkeypatch
):
    """The last window: identity changes between the recheck and the signal.
    `verified_signal` re-verifies inside the send and refuses."""
    world.table.add(888, 888, 50.0, name="vite")
    monkeypatch.setattr(
        procutil, "port_owner",
        lambda port: ProcInfo(pid=888, name="vite", cmdline=()) if port == 5180 else None,
    )

    def recycle(pid):
        world.table.kill_now(pid)
        world.table.add(pid, pid, 9999.0, name="other")

    world.table.before_verify = recycle
    svc = _strict_service(devctl_home, write_registry, sample_registry_data, world, clock)
    res = svc.env_sweep()
    assert world.table.sent == []
    (row,) = res["killed_squatters"]
    assert row["killed"] is False and row["outcome"] == "identity_mismatch"


def test_strict_reclaim_skips_a_port_a_starting_lease_claimed_after_the_snapshot(
    devctl_home, write_registry, sample_registry_data, world, clock, monkeypatch
):
    """S4 seam, the readiness-window race: the lock-free snapshot saw a leaseless
    listener on 5180, then an `up` wrote its `starting` lease (§6: before its
    first process) and its workload bound mid-readiness. Under L the port is
    held by a non-terminal lease, so nothing is re-probed or signalled."""
    world.table.add(888, 888, 50.0, name="vite")
    probes: list[int] = []

    def owner(port):
        if port != 5180:
            return None
        probes.append(port)
        if len(probes) == 1:  # right after the snapshot, an up claims 5180
            t = clock()
            new_starting_lease(
                generation="g" * 32, project="webapp", profile="default", runner="process",
                port=5180, session="s", cwd="/proj/A", spawn_cwd="/proj/A", log="/dev/null",
                plan={"cmd": "true", "cwd": "/proj/A", "port_env": "PORT"},
                now=t, expires=t + timedelta(hours=1),
            ).write(devctl_home.lease_file_for("webapp", "/proj/A"))
        return ProcInfo(pid=888, name="vite", cmdline=())

    monkeypatch.setattr(procutil, "port_owner", owner)
    svc = _strict_service(devctl_home, write_registry, sample_registry_data, world, clock)
    res = svc.env_sweep()
    assert probes == [5180]  # never re-probed: refused on the lease alone
    assert world.table.sent == []
    (row,) = res["killed_squatters"]
    assert row["killed"] is False and row["outcome"] == "leased"


def test_strict_reclaim_never_signals_a_listener_in_a_live_leases_session(
    devctl_home, write_registry, sample_registry_data, world, clock, monkeypatch
):
    """§11 precondition 2: a running lease's workload that bound a second block
    port is that lease's, not a squatter — at snapshot and under L alike."""
    svc = _strict_service(devctl_home, write_registry, sample_registry_data, world, clock)
    monkeypatch.setattr(procutil, "port_owner", lambda port: None)
    assert svc.env_up("webapp", cwd="/proj/A")["ok"] is True
    ident = lease_at(devctl_home, "/proj/A").ownership()
    world.table.add(4242, ident.sid, ident.owner_start + 1, name="node")
    monkeypatch.setattr(
        procutil, "port_owner",
        lambda port: ProcInfo(pid=4242, name="node", cmdline=()) if port == 5187 else None,
    )
    res = svc.env_sweep()
    assert "killed_squatters" not in res and "squatters" not in res
    # And had the snapshot raced the lease (row taken before it existed), the
    # decision under L still refuses on the session.
    sq = service_mod._Squatter("webapp", 5187, 4242, "node", ident.owner_start + 1, ident.sid)
    (row,) = svc._kill_squatters([sq])
    assert row["killed"] is False and row["outcome"] == "lease_session"
    assert world.table.signals_to(4242) == []


@pytest.mark.parametrize(
    "case, expected",
    [
        ("unreadable", "lease_unreadable"),
        ("unverifiable", "unverifiable"),
        ("probe", "probe_unavailable"),
        ("gone", "gone"),
        ("changed", "listener_changed"),
        ("zombie", "gone"),
    ],
)
def test_strict_reclaim_refusals(
    case, expected, devctl_home, write_registry, sample_registry_data, world, clock, monkeypatch
):
    """Every way the decision under L can refuse, and none of them signals."""
    svc = _strict_service(devctl_home, write_registry, sample_registry_data, world, clock)
    world.table.add(888, 888, 50.0, name="vite")
    sq = service_mod._Squatter("webapp", 5180, 888, "vite", 50.0, 888)
    owner = ProcInfo(pid=888, name="vite", cmdline=())
    if case == "unreadable":
        devctl_home.lease_file_for("webapp", "/proj/Z").write_text("{not json")
    elif case == "unverifiable":
        sq = service_mod._Squatter("webapp", 5180, 888, "vite", None, None)
    elif case == "probe":
        _probe_unavailable(monkeypatch)
    elif case == "gone":
        owner = None
    elif case == "changed":
        owner = ProcInfo(pid=889, name="vite", cmdline=())
    elif case == "zombie":
        world.table.procs[888].status = psutil.STATUS_ZOMBIE
    if case != "probe":
        monkeypatch.setattr(procutil, "port_owner", lambda port: owner)
    (row,) = svc._kill_squatters([sq])
    assert row["killed"] is False and row["outcome"] == expected
    assert world.table.sent == []


def test_advisory_sweep_reports_and_never_signals(
    service, devctl_home, world, monkeypatch
):
    """Advisory is unchanged: the squatter is reported, nothing is locked for
    it, nothing is signalled, and no reclaim is recorded."""
    world.table.add(888, 888, 50.0, name="vite")
    monkeypatch.setattr(
        procutil, "port_owner",
        lambda port: ProcInfo(pid=888, name="vite", cmdline=()) if port == 5180 else None,
    )
    res = service.env_sweep()
    assert res["squatters"] == [
        {"project": "webapp", "port": 5180, "pid": 888, "name": "vite", "status": "squatter"}
    ]
    assert "killed_squatters" not in res
    assert world.table.sent == []
    assert "squatter_reclaim" not in kinds(service)


# --- ADR-0008: an unrunnable listener probe never reads as a clean board ---


def _probe_unavailable(monkeypatch):
    def _raise(_port):
        raise procutil.ProbeUnavailable("lsof is not installed; then psutil denied")

    monkeypatch.setattr(procutil, "port_owner", _raise)


def test_up_still_starts_when_the_probe_is_unavailable(service, monkeypatch):
    """ADR-0008 §2: degrade, don't refuse. A bad draw fails safely at bind time."""
    _probe_unavailable(monkeypatch)
    res = service.env_up("webapp")
    assert res["ok"] is True
    assert res["port"]


def test_up_labels_a_draw_made_without_squatter_verification(service, monkeypatch):
    """The blind spot has to travel with the result, or it isn't surfaced at all."""
    _probe_unavailable(monkeypatch)
    res = service.env_up("webapp")
    assert res["squatter_check"] == "unavailable"
    assert "lsof" in res["squatter_check_detail"]


def test_up_says_nothing_when_the_probe_worked(service):
    """No noise on the normal path — absence of the key means verified."""
    res = service.env_up("webapp")
    assert res["ok"] is True
    assert "squatter_check" not in res
    assert "squatter_check_detail" not in res


def test_ls_marks_an_unverified_board_instead_of_looking_clean(service, monkeypatch):
    """The core of the ADR: no squatter rows AND a stated reason why."""
    _probe_unavailable(monkeypatch)
    res = service.env_ls()
    assert res["ok"] is True
    assert not [e for e in res["environments"] if e.get("status") == "squatter"]
    assert res["squatter_check"] == "unavailable"


def test_sweep_marks_an_unverified_sweep(service, monkeypatch):
    _probe_unavailable(monkeypatch)
    res = service.env_sweep()
    assert res["ok"] is True
    assert "squatters" not in res
    assert res["squatter_check"] == "unavailable"


def test_strict_sweep_kills_nothing_when_the_probe_is_unavailable(
    devctl_home, write_registry, sample_registry_data, world, clock, monkeypatch
):
    """The safety-critical case.

    Under strict enforcement devctl reclaims its block by killing squatters. If an
    unrunnable probe reported an empty list, the sweep would look like a clean
    reclaim that inspected nothing. Nothing may be killed on evidence that was
    never gathered — F7's "never kill what you don't own" stays fail-closed even
    though the draw path deliberately does not (ADR-0008 §2, Alternatives).
    """
    sample_registry_data["enforcement"] = "strict"
    write_registry(sample_registry_data)
    _probe_unavailable(monkeypatch)
    killed: list[int] = []
    monkeypatch.setattr("rentctl.core.service.os.kill", lambda pid, sig: killed.append(pid))
    svc = make_service(devctl_home, world, clock)
    res = svc.env_sweep()
    assert killed == []
    assert "killed_squatters" not in res
    assert res["squatter_check"] == "unavailable"


# --- the event log: what actually happened, and which layer did it ---------
# (spec §8 cleanup layers; §11.1 G4 is scored from these records)

def test_up_records_the_start(service):
    service.env_up("webapp", cwd="/proj/webapp")
    (rec,) = events(service)
    assert rec["event"] == "up"
    assert rec["project"] == "webapp"
    assert rec["port"] == 5180
    assert rec["pid"] == 1000
    assert rec["session"] == "sess-1"
    assert rec["cwd"] == "/proj/webapp"
    assert rec["already_running"] is False
    assert rec["generation"] and rec["supervisor_pid"]


def test_up_on_running_env_records_a_renewal(service, clock):
    service.env_up("webapp")
    clock.advance(minutes=30)
    service.env_up("webapp")
    first, second = events(service)
    assert first["already_running"] is False
    assert second["already_running"] is True
    assert second["generation"] == first["generation"]


def test_up_failure_is_recorded(service, monkeypatch):
    monkeypatch.setattr(
        procutil, "port_owner", lambda port: ProcInfo(pid=777, name="node", cmdline=())
    )
    service.env_up("webapp")
    (rec,) = events(service)
    assert rec["event"] == "up_failed"
    assert rec["error"] == BLOCK_EXHAUSTED


def test_down_by_project_is_declared_layer_1(service):
    service.env_up("webapp")
    service.env_down("webapp")
    rec = events(service)[-1]
    assert rec["event"] == "down"
    assert rec["reason"] == "explicit"
    assert rec["layer"] == 1
    assert rec["reason_source"] == "declared"
    assert rec["killed"] is True


def test_down_all_without_a_reason_is_inferred(service):
    """`devctl down --all` typed by hand looks exactly like the SessionEnd hook.
    The reason is still recorded — but marked a guess, so it cannot pass as proof
    that layer 2 fired. It also waits like any explicit `down`."""
    service.env_up("webapp", cwd="/proj/A")
    res = service.env_down(cwd="/proj/A")
    assert [d["stopped"] for d in res["downed"]] == [True]
    rec = events(service)[-1]
    assert rec["reason"] == "session-end"
    assert rec["layer"] == 2
    assert rec["reason_source"] == "inferred"


def test_down_all_with_a_declared_reason_is_layer_2_evidence(service, world):
    service.env_up("webapp", cwd="/proj/A")
    service.env_down(cwd="/proj/A", reason="session-end")
    req = events(service)[-1]
    # The hook's own evidence, inside its budget: the request (§13).
    assert req["event"] == "stop_requested" and req["reason"] == "session-end"
    world.tick()
    rec = events(service)[-1]
    assert rec["event"] == "down"
    assert rec["layer"] == 2
    assert rec["reason_source"] == "declared"
    assert rec["killed"] is True


def test_down_with_no_lease_records_nothing(service):
    """The SessionEnd hook fires in every session; the ones that leased nothing
    must not bury the real events."""
    service.env_down("webapp")
    service.env_down(cwd="/proj/nothing-here")
    assert events(service) == []


def test_sweep_expiry_is_layer_4_kill(service, clock, world):
    service.env_up("webapp", lease_minutes=120, cwd="/proj/A")
    world.kill_supervisor(lease_key("webapp", "/proj/A"))
    clock.advance(minutes=121)
    service.env_sweep()
    rec = events(service)[-1]
    assert rec["event"] == "down"
    assert rec["reason"] == "sweep-expired"
    assert rec["layer"] == 4
    assert rec["op"] == "sweep"
    assert rec["killed"] is True
    assert rec["mode"] == "recovery"


def test_sweep_of_dead_process_is_recorded_as_no_kill(service, world):
    service.env_up("webapp", cwd="/proj/A")
    world.kill_supervisor(lease_key("webapp", "/proj/A"))
    world.kill_workload(1000)  # died on its own, and nobody supervised it
    service.env_sweep()
    rec = events(service)[-1]
    assert rec["reason"] == "sweep-dead"
    assert rec["layer"] == 4
    assert rec["killed"] is False


def test_ls_reconcile_is_recorded_under_its_own_op(service, world):
    """`ls` reconciles too, so it can tear down — the record says which command did."""
    service.env_up("webapp", cwd="/proj/A")
    world.kill_supervisor(lease_key("webapp", "/proj/A"))
    world.kill_workload(1000)
    service.env_ls()
    rec = events(service)[-1]
    assert rec["op"] == "ls"
    assert rec["layer"] == 4


def test_event_log_failure_does_not_break_a_teardown(service, devctl_home, world):
    """Fail open: if the log cannot be written, the kill still happens."""
    service.env_up("webapp", cwd="/proj/A")
    service.events.path = devctl_home.state_dir / "logs" / "webapp-blocked.log" / "events.jsonl"
    (devctl_home.state_dir / "logs" / "webapp-blocked.log").write_text("a file, not a dir")
    res = service.env_down("webapp", cwd="/proj/A")
    assert res["ok"] is True
    assert res["was_running"] is True
    assert 1000 in world.stopped
    assert lease_at(devctl_home, "/proj/A") is None


def test_ls_registry_invalid_still_lists_leases(service, devctl_home):
    service.env_up("webapp")
    # Corrupt the registry after the lease exists.
    devctl_home.registry_file.write_text("{bad")
    res = service.env_ls()
    assert res["ok"] is True
    assert [e["project"] for e in res["environments"]] == ["webapp"]
    assert "registry_error" in res


# --- ADR-0010: the server runs in the caller's worktree -------------------

def _init_repo_with_worktree(root: Path) -> tuple[Path, Path]:
    """A real repo with a frontend/ subdir, plus a linked worktree. Returns both."""
    env = {"HOME": str(root), "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}

    def git(*args, cwd):
        subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, env=env)

    main = root / "main"
    (main / "frontend").mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=main)
    git("config", "user.email", "t@example.com", cwd=main)
    git("config", "user.name", "t", cwd=main)
    (main / "frontend" / "package.json").write_text("{}\n")
    git("add", "-A", cwd=main)
    git("commit", "-qm", "init", cwd=main)
    lane = root / "lane"
    git("worktree", "add", "-q", str(lane), "-b", "lane", cwd=main)
    return main, lane


@pytest.fixture
def worktree_service(tmp_path, devctl_home, write_registry, world, clock, monkeypatch):
    """A service whose registry points at a real repo's frontend/ subdirectory."""
    main, lane = _init_repo_with_worktree(tmp_path / "repo")
    write_registry(
        {
            "projects": {
                "webapp": {
                    "block": 5180,
                    "runner": "process",
                    "profiles": {
                        "default": {
                            "cmd": "npm run dev",
                            "cwd": str(main / "frontend"),
                            "port_env": "PORT",
                        }
                    },
                }
            }
        }
    )
    monkeypatch.setattr(procutil, "port_owner", lambda port: None)
    return make_service(devctl_home, world, clock), main, lane


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_up_from_a_lane_spawns_in_that_lane(worktree_service, world, devctl_home):
    """The reported bug: a lane's server must serve the lane, not the main checkout."""
    svc, main, lane = worktree_service
    res = svc.env_up("webapp", cwd=str(lane))
    assert res["ok"] is True
    # What the supervisor was told to run it in — the whole point.
    assert world.start_cwds == [str((lane / "frontend").resolve())]
    # ...and it is visible to the caller rather than something they must infer.
    assert res["serving"] == str((lane / "frontend").resolve())
    # The lease still belongs to the lane it was requested from (ADR-0007),
    # which is what teardown matches on.
    lease = Lease.read(devctl_home.lease_file_for("webapp", str(lane)))
    assert lease.cwd == str(lane.resolve())
    assert lease.spawn_cwd == str((lane / "frontend").resolve())
    assert lease.plan["cwd"] == str((lane / "frontend").resolve())


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_up_from_the_main_checkout_is_unchanged(worktree_service, world):
    """No re-rooting when the caller is the enrolled checkout — same as before."""
    svc, main, lane = worktree_service
    res = svc.env_up("webapp", cwd=str(main))
    assert [Path(c).resolve() for c in world.start_cwds] == [(main / "frontend").resolve()]
    assert res["serving"] == str((main / "frontend").resolve())


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_up_from_an_unrelated_directory_uses_the_approved_cwd(worktree_service, world, tmp_path):
    """Fail toward the operator-approved directory, never toward an unverified one."""
    svc, main, lane = worktree_service
    stranger = tmp_path / "stranger"
    stranger.mkdir()
    svc.env_up("webapp", cwd=str(stranger))
    assert [Path(c).resolve() for c in world.start_cwds] == [(main / "frontend").resolve()]


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_two_lanes_get_two_ports_and_two_directories(worktree_service, world, tmp_path):
    """The full ADR-0007 + ADR-0010 promise, which the bug half-delivered."""
    svc, main, lane = worktree_service
    a = svc.env_up("webapp", cwd=str(lane))
    b = svc.env_up("webapp", cwd=str(main))
    assert a["port"] != b["port"]
    assert [Path(c).resolve() for c in world.start_cwds] == [
        (lane / "frontend").resolve(),
        (main / "frontend").resolve(),
    ]


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_up_from_a_lane_whose_subdir_escapes_is_refused_not_spawned(
    worktree_service, world, devctl_home, tmp_path
):
    """WI-0068: a lane's ``frontend`` symlinked outside the repo must never reach
    the supervisor — and must not quietly run the main checkout instead either."""
    svc, main, lane = worktree_service
    outside = tmp_path / "outside"
    outside.mkdir()
    shutil.rmtree(lane / "frontend")
    (lane / "frontend").symlink_to(outside, target_is_directory=True)

    res = svc.env_up("webapp", cwd=str(lane))
    assert res["ok"] is False
    assert res["error"] == CWD_ESCAPES_ROOT
    assert world.start_cwds == []
    assert world.sups == {}
    assert not devctl_home.lease_file_for("webapp", str(lane)).exists()


# --- readiness, as the waiting `up` reports it ------------------------------
#
# The probe itself — loopback, then the owner attributed by SESSION — runs in
# the supervisor now and is tested there (test_supervisor_readiness.py). What
# the service owns is carrying the verdict the supervisor wrote to the caller.


def test_up_reports_a_server_that_bound_a_non_loopback_address(service, world):
    world.readiness = "listening"
    res = service.env_up("webapp", cwd="/proj/webapp")
    assert res["ok"] is True
    assert res["readiness"] == "listening"
    assert world.stopped == []
    assert "will not reach it" in res["readiness_detail"]


def test_up_reports_a_probe_that_could_not_run(service, world):
    world.readiness = "unknown"
    res = service.env_up("webapp", cwd="/proj/webapp")
    assert res["ok"] is True
    assert res["readiness"] == "unknown"
    assert "swept rather than orphaned" in res["readiness_detail"]


def test_up_answered_has_no_readiness_detail(service):
    res = service.env_up("webapp", cwd="/proj/webapp")
    assert res["readiness"] == "answered"
    assert "readiness_detail" not in res


def test_renewing_a_lease_reports_that_readiness_was_not_probed(service):
    """No start happened, so "answered" would be a claim nothing checked."""
    service.env_up("webapp", cwd="/proj/webapp")
    again = service.env_up("webapp", cwd="/proj/webapp")
    assert again["already_running"] is True
    assert again["readiness"] == "not_probed"


def test_readiness_is_up_only_excludes_a_verified_absence():
    """Pure: the one state that means "stop it" is the one that proved absence."""
    assert Readiness.ANSWERED.is_up
    assert Readiness.LISTENING.is_up
    assert Readiness.UNKNOWN.is_up
    assert Readiness.NOT_PROBED.is_up
    assert not Readiness.NOT_LISTENING.is_up


# --- migration: legacy (1.0.x) leases (§14) ---------------------------------

LEGACY_PID = 4242
LEGACY_START = 1784000000.0


def write_legacy(paths, clock, *, cwd="/proj/webapp", minutes=60, **kw) -> Path:
    """A lease exactly as 1.0.x wrote it: no schema, a {pid, start} handle."""
    lease = Lease(
        project="webapp", profile="default", runner="process",
        handle={"pid": LEGACY_PID, "pid_start_time": LEGACY_START}, port=5180, session="old",
        cwd=cwd, created=clock(), expires=clock() + timedelta(minutes=minutes), log="/dev/null",
        **kw,
    )
    path = paths.lease_file_for("webapp", cwd)
    lease.write(path)
    return path


def test_legacy_lease_renewed_in_legacy_format(service, devctl_home, world, clock):
    """A 1.0.x watchdog may still be babysitting it, so `up` renews it in place
    in the format that watchdog reads (§14)."""
    world.add_legacy_workload(LEGACY_PID, LEGACY_START)
    path = write_legacy(devctl_home, clock, watchdog_pid=777, watchdog_pid_start_time=5.0)
    clock.advance(minutes=30)
    res = service.env_up("webapp", cwd="/proj/webapp")
    assert res["ok"] is True and res["already_running"] is True
    assert res["pid"] == LEGACY_PID
    raw = json.loads(path.read_text())
    assert "schema" not in raw and raw["watchdog_pid"] == 777
    assert datetime.fromisoformat(raw["expires"]) == clock.now + timedelta(minutes=120)
    assert world.sups == {}  # nothing new spawned
    service.env_down("webapp", cwd="/proj/webapp")


def test_legacy_lease_stop_uses_session_membership(service, devctl_home, world, clock):
    """The legacy handle maps to its leader's session (§2): the stop reaches a
    member whose leader is long gone, and is verified — here, in-process."""
    world.add_legacy_workload(LEGACY_PID, LEGACY_START)
    world.table.add(LEGACY_PID + 1, LEGACY_PID, LEGACY_START + 1, name="node")  # the child
    world.table.kill_now(LEGACY_PID)  # the leader already exited (S1's shape)
    path = write_legacy(devctl_home, clock)
    res = service.env_down("webapp", cwd="/proj/webapp")
    assert res["stopped"] is True and res["was_running"] is True
    assert world.table.signals_to(LEGACY_PID + 1) == [signal.SIGTERM]
    assert not path.exists()
    down = [e for e in events(service) if e["event"] == "down"][-1]
    assert down["mode"] == "recovery" and down["reason"] == "explicit"


def test_session_end_hands_a_legacy_lease_to_a_recovery_supervisor(
    service, devctl_home, world, clock
):
    """R0: the hook never runs a recovery stop its 1.5 s could cut off halfway.
    It spawns a detached recovery supervisor, which finishes the stop."""
    world.add_legacy_workload(LEGACY_PID, LEGACY_START)
    path = write_legacy(devctl_home, clock)
    res = service.env_down(cwd="/proj/webapp", reason="session-end", session="old")
    (row,) = res["downed"]
    assert row["pending"] is True and "recovery supervisor" in row["detail"]
    ((ref, key, gen, reason, source, op),) = world.recoveries
    assert (reason, source, op) == ("session-end", "declared", "down")
    # The request is on disk already, and the file is schema 2 now: a 1.0.x
    # watchdog reading it gets CORRUPT and exits without touching anything.
    assert json.loads(path.read_text())["watchdog_pid"] == "supervised"
    world.tick()
    assert not path.exists()
    assert world.table.signals_to(LEGACY_PID) == [signal.SIGTERM]


def test_a_legacy_lease_whose_workload_is_gone_is_cleaned_without_a_stop(
    service, devctl_home, world, clock
):
    path = write_legacy(devctl_home, clock)  # no workload in the table
    res = service.env_down(cwd="/proj/webapp", reason="session-end", session="old")
    assert [d["stopped"] for d in res["downed"]] == [True]
    assert [d["was_running"] for d in res["downed"]] == [False]
    assert not path.exists() and world.recoveries == []


def test_down_on_an_ambiguous_legacy_lease_refuses_without_a_signal(
    service, devctl_home, world, clock
):
    """PID S held by a different process while its session has members: nothing
    under it can be proved ours. No request, no signal, lease untouched."""
    world.table.add(LEGACY_PID, 1, LEGACY_START + 9999, name="stranger")  # reused pid
    world.table.add(LEGACY_PID + 1, LEGACY_PID, LEGACY_START + 1, name="node")
    path = write_legacy(devctl_home, clock)
    before = path.read_bytes()
    res = service.env_down("webapp", cwd="/proj/webapp")
    assert res["stopped"] is False and res["identity_ambiguous"] is True
    assert world.table.sent == []
    assert path.read_bytes() == before
    (incomplete,) = [e for e in events(service) if e["event"] == "cleanup_incomplete"]
    assert incomplete["identity_ambiguous"] is True
    up = service.env_up("webapp", cwd="/proj/webapp")
    assert up["error"] == CLEANUP_INCOMPLETE and up["identity_ambiguous"] is True


def test_an_expired_legacy_lease_is_swept_through_a_recovery_claim(
    service, devctl_home, world, clock
):
    world.add_legacy_workload(LEGACY_PID, LEGACY_START)
    path = write_legacy(devctl_home, clock, minutes=5)
    clock.advance(minutes=6)
    res = service.env_sweep()
    assert [s["action"] for s in res["swept"]] == ["expire"]
    assert not path.exists()
    rec = events(service)[-1]
    assert rec["reason"] == "sweep-expired" and rec["layer"] == 4 and rec["killed"] is True


# --- §9: nobody waits while holding L ----------------------------------------

def test_no_lock_held_while_waiting(service, devctl_home, world, clock, monkeypatch):
    """Instrument `project_lock` everywhere the service reaches it, and fail if
    any wait (the waiters' sleep, which is also when fake supervisors act) or
    any recovery stop happens under it. Every hold is also bounded."""
    held: list[str] = []
    holds: list[float] = []
    real_lock = service_mod.project_lock

    @contextmanager
    def instrumented(path):
        with real_lock(path):
            held.append(str(path))
            import time as _t
            t0 = _t.monotonic()
            try:
                yield
            finally:
                holds.append(_t.monotonic() - t0)
                held.pop()

    monkeypatch.setattr(service_mod, "project_lock", instrumented)
    monkeypatch.setattr(supervision, "project_lock", instrumented)
    real_sleep = world.sleep

    def checked_sleep(seconds):
        assert held == [], f"a waiter slept while holding {held}"
        real_sleep(seconds)

    world.sleep = checked_sleep
    real_stop = supervision.stop_workload

    def checked_stop(*a, **kw):
        assert held == [], f"a recovery stop ran while holding {held}"
        return real_stop(*a, **kw)

    monkeypatch.setattr(supervision, "stop_workload", checked_stop)

    service.env_up("webapp", cwd="/proj/A")                       # waits for running
    service.env_up("webapp", cwd="/proj/B")
    service.env_down("webapp", cwd="/proj/A")                     # waits for stopped
    world.kill_supervisor(lease_key("webapp", "/proj/B"))
    service.env_down("webapp", cwd="/proj/B")                     # inline recovery stop
    world.stubborn = True
    service.env_up("webapp", cwd="/proj/C")
    clock.advance(hours=3)
    world.kill_supervisor(lease_key("webapp", "/proj/C"))
    service.env_sweep()                                           # recovery stop in sweep
    assert holds and max(holds) < 1.0, max(holds)


# --- WI-0069 / §3: a legacy watchdog is signalled only if provably ours ------
#
# Leases survive a reboot, and `claude --continue` skips the startup sweep, so a
# SessionEnd teardown can meet a lease whose `watchdog_pid` now names somebody
# else's process. Each test below stands a REAL disposable child in for the
# watchdog: a stand-in that can actually receive SIGTERM is the only way to
# prove it was — or was not — sent. Nothing else on the machine is ever named.


def _sleeper(*marker: str) -> subprocess.Popen:
    # A trailing argv marker is how a stand-in "is" a watchdog to the §3 check.
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", *marker])


def _reap(child: subprocess.Popen) -> None:
    if child.poll() is None:
        child.kill()
    child.wait(timeout=5)


def _still_running(child: subprocess.Popen) -> bool:
    try:
        child.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        return True
    return False


def _skips(service) -> list[dict]:
    return [e for e in service.events.read() if e["event"] == "watchdog_signal_skipped"]


@pytest.fixture
def legacy_with_watchdog(service, devctl_home, world, clock):
    """Write a live legacy lease naming ``child`` as its watchdog."""

    def make(child: subprocess.Popen | None, start: float | None) -> Path:
        world.add_legacy_workload(LEGACY_PID, LEGACY_START)
        return write_legacy(
            devctl_home, clock,
            watchdog_pid=None if child is None else child.pid,
            watchdog_pid_start_time=start,
        )

    return make


def test_watchdog_with_matching_start_time_is_signalled(service, legacy_with_watchdog):
    child = _sleeper("rentctl.watchdog")
    try:
        started = procutil.observe_start_time(child.pid)
        assert started is not None
        path = legacy_with_watchdog(child, started)

        res = service.env_down("webapp", cwd="/proj/webapp")

        assert res["ok"] is True and res["stopped"] is True
        assert child.wait(timeout=5) == -signal.SIGTERM
        assert _skips(service) == []
        assert not path.exists()
    finally:
        _reap(child)


def test_watchdog_pid_with_a_different_start_time_is_not_signalled(service, legacy_with_watchdog):
    """The reboot case: the pid is alive, but it is not the process we spawned."""
    child = _sleeper("rentctl.watchdog")
    try:
        started = procutil.observe_start_time(child.pid)
        assert started is not None
        path = legacy_with_watchdog(child, started - 3600.0)

        res = service.env_down("webapp", cwd="/proj/webapp")

        assert res["ok"] is True
        assert _still_running(child), "a process that merely inherited the pid was signalled"
        (skip,) = _skips(service)
        assert skip["pid"] == child.pid
        assert skip["reason"] == "pid-recycled"
        # The teardown itself still completed: the guard narrows the watchdog
        # signal, it does not hold the lease hostage.
        assert not path.exists()
    finally:
        _reap(child)


def test_a_matching_pid_that_is_not_a_watchdog_is_not_signalled(service, legacy_with_watchdog):
    """§3 adds the cmdline check: start time alone does not make it a watchdog."""
    child = _sleeper()
    try:
        started = procutil.observe_start_time(child.pid)
        legacy_with_watchdog(child, started)
        service.env_down("webapp", cwd="/proj/webapp")
        assert _still_running(child)
        (skip,) = _skips(service)
        assert skip["reason"] == "not-a-watchdog"
    finally:
        _reap(child)


def test_legacy_lease_without_a_watchdog_start_time_is_not_signalled(service, legacy_with_watchdog):
    """A 1.0.1 lease records only the pid — there is nothing to verify it against."""
    child = _sleeper("rentctl.watchdog")
    try:
        path = legacy_with_watchdog(child, None)
        assert Lease.read(path).watchdog_pid_start_time is None  # old leases still load

        res = service.env_down("webapp", cwd="/proj/webapp")

        assert res["ok"] is True
        assert _still_running(child)
        (skip,) = _skips(service)
        assert skip["pid"] == child.pid
        assert skip["reason"] == "unverifiable"
        assert not path.exists()
    finally:
        _reap(child)


def test_watchdog_that_is_already_gone_is_no_error(service, legacy_with_watchdog):
    child = _sleeper("rentctl.watchdog")
    started = procutil.observe_start_time(child.pid)
    assert started is not None
    child.kill()
    child.wait(timeout=5)
    path = legacy_with_watchdog(child, started)

    res = service.env_down("webapp", cwd="/proj/webapp")

    assert res["ok"] is True
    assert res["was_running"] is True
    assert not path.exists()
    # A watchdog that exited on its own is the ordinary case, not a skipped kill.
    assert _skips(service) == []


def test_watchdog_that_exits_between_check_and_signal_is_no_error(service, monkeypatch):
    """The check passed, then the pid vanished before SIGTERM landed.

    Faked end to end — a fake pid, a faked observation, a faked failing kill —
    so no real process is ever named by this test.
    """
    monkeypatch.setattr(procutil, "observe_start_time", lambda pid: 1784080000.0)
    monkeypatch.setattr(Service, "_is_watchdog", staticmethod(lambda pid: True))

    def _vanished(pid, sig):
        raise ProcessLookupError(pid)

    monkeypatch.setattr(service_mod.os, "kill", _vanished)
    lease = Lease(
        project="webapp",
        profile="default",
        runner="process",
        handle={"pid": 1000, "pid_start_time": 1000.0},
        port=5180,
        session="s",
        cwd="/proj/webapp",
        created=datetime(2026, 7, 14, 8, 0, tzinfo=CDT),
        expires=datetime(2026, 7, 14, 9, 0, tzinfo=CDT),
        log="/dev/null",
        watchdog_pid=424242,
        watchdog_pid_start_time=1784080000.0,
    )
    service._kill_watchdog(lease)  # must not raise
    assert _skips(service) == []


def test_new_code_never_spawns_a_watchdog():
    """§14: the supervisor replaces it. The spawn path is gone, not dormant."""
    import inspect

    assert not hasattr(Service, "_spawn_watchdog")
    assert "watchdog_spawn" not in inspect.signature(Service).parameters
    assert service_mod.decide is lifecycle.decide  # §10's table, not the 1.0.x oracle


# --- `up` over every lease state it can meet (§10, §14) ----------------------

def _rewrite(paths, cwd, **fields) -> Lease:
    path = paths.lease_file_for("webapp", cwd)
    lease = Lease(**{**Lease.read(path).__dict__, **fields})
    lease.write(path)
    return lease


def test_up_replaces_a_terminal_record_left_behind(service, devctl_home, world):
    """A crash between the event and the unlink leaves `stopped` on disk. It
    holds no port and names nothing running; a new generation replaces it."""
    service.env_up("webapp", cwd="/proj/A")
    world.kill_supervisor(lease_key("webapp", "/proj/A"))
    old = _rewrite(devctl_home, "/proj/A", state="stopped")
    res = service.env_up("webapp", cwd="/proj/A")
    assert res["already_running"] is False
    assert lease_at(devctl_home, "/proj/A").generation != old.generation


def test_up_on_a_legacy_lease_whose_workload_is_gone_starts_fresh(service, devctl_home, world, clock):
    write_legacy(devctl_home, clock)  # nothing in its session
    res = service.env_up("webapp", cwd="/proj/webapp")
    assert res["already_running"] is False and res["state"] == "running"
    assert lease_at(devctl_home, "/proj/webapp").schema == 2
    down = [e for e in events(service) if e["event"] == "down"][-1]
    assert down["reason"] == "sweep-dead" and down["actor"] == "up"


def test_up_on_an_expired_unsupervised_lease_recovers_it_then_starts_fresh(
    service, devctl_home, world, clock
):
    service.env_up("webapp", lease_minutes=10, cwd="/proj/A")
    world.kill_supervisor(lease_key("webapp", "/proj/A"))
    service.env_ls()  # → unsupervised
    clock.advance(minutes=11)
    res = service.env_up("webapp", cwd="/proj/A")
    assert res["already_running"] is False and res["pid"] == 1001
    assert world.table.signals_to(1000) == [signal.SIGTERM]


def test_up_on_an_ambiguous_unsupervised_lease_refuses(service, devctl_home, world):
    service.env_up("webapp", cwd="/proj/A")
    sup = world.kill_supervisor(lease_key("webapp", "/proj/A"))
    service.env_ls()  # → unsupervised
    world.table.add(sup.pid, 1, sup.start + 999, name="stranger")  # PID S reused
    res = service.env_up("webapp", cwd="/proj/A")
    assert res["error"] == CLEANUP_INCOMPLETE and res["identity_ambiguous"] is True
    assert world.table.sent == []


def test_up_on_a_starting_lease_whose_supervisor_died_reconciles_first(
    service, devctl_home, world, clock
):
    key, _ = _start_elsewhere(devctl_home, world, clock)
    world.kill_supervisor(key)  # dead before registering: nothing was launched
    res = service.env_up("webapp", cwd="/proj/webapp")
    assert res["ok"] is True and res["already_running"] is False
    assert any(e.get("phase") == "registration" for e in events(service))


def test_up_on_a_stopping_lease_waits_for_its_supervisor(service, devctl_home, world):
    service.env_up("webapp", cwd="/proj/A")
    lease = lease_at(devctl_home, "/proj/A")
    stopping = transition(
        lease, Event(EventKind.STOP_REQUEST, world.clock(), reason="explicit", op="down"),
        Actor(ActorKind.CLI, lease.generation, supervision.self_ref()),
    )
    _rewrite(devctl_home, "/proj/A", state="stopping", stop=stopping.stop)
    res = service.env_up("webapp", cwd="/proj/A")
    assert res["already_running"] is False and res["pid"] == 1001


def test_up_on_a_stopping_lease_with_a_dead_supervisor_resumes_its_stop(service, devctl_home, world):
    service.env_up("webapp", cwd="/proj/A")
    lease = lease_at(devctl_home, "/proj/A")
    stopping = transition(
        lease, Event(EventKind.STOP_REQUEST, world.clock(), reason="explicit", op="down"),
        Actor(ActorKind.CLI, lease.generation, supervision.self_ref()),
    )
    _rewrite(devctl_home, "/proj/A", state="stopping", stop=stopping.stop)
    world.kill_supervisor(lease_key("webapp", "/proj/A"))
    res = service.env_up("webapp", cwd="/proj/A")
    assert res["already_running"] is False
    down = [e for e in events(service) if e["event"] == "down"][0]
    assert down["reason"] == "explicit" and down["mode"] == "recovery"  # the original reason


def test_up_waiting_on_a_stop_that_leaves_survivors_refuses(service, devctl_home, world):
    world.stubborn = True
    service.env_up("webapp", cwd="/proj/A")
    service.env_down("webapp", cwd="/proj/A", wait_s=0)
    res = service.env_up("webapp", cwd="/proj/A")
    assert res["error"] == CLEANUP_INCOMPLETE
    assert [s["pid"] for s in res["survivors"]] == [1000]


def test_up_survives_a_failed_spawned_write(service, devctl_home, world, monkeypatch):
    """The supervisor registers itself whether or not the CLI's SPAWNED write
    landed (§6); the start still completes."""
    real_write = Lease.write
    calls = {"n": 0}

    def second_write_fails(self, path):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        real_write(self, path)

    monkeypatch.setattr(Lease, "write", second_write_fails)
    res = service.env_up("webapp", cwd="/proj/A")
    assert res["ok"] is True and res["state"] == "running"


# --- `down` edge paths ----------------------------------------------------------------

def test_down_finishes_the_removal_of_a_terminal_record(service, devctl_home, world):
    service.env_up("webapp", cwd="/proj/A")
    world.kill_supervisor(lease_key("webapp", "/proj/A"))
    _rewrite(devctl_home, "/proj/A", state="exited")
    res = service.env_down("webapp", cwd="/proj/A")
    assert res["stopped"] is True and res["was_running"] is False
    assert lease_at(devctl_home, "/proj/A") is None


def test_down_on_a_start_not_yet_registered_launches_nothing(service, devctl_home, world, clock):
    """A stop recorded before registration: the supervisor refuses to register
    over it and exits (I1); the waiter lets §10 abandon the record."""
    _start_elsewhere(devctl_home, world, clock)
    res = service.env_down("webapp", cwd="/proj/webapp")
    assert res["stopped"] is True
    assert world.started == []
    assert lease_at(devctl_home, "/proj/webapp") is None


def test_down_when_no_recovery_supervisor_can_start_says_so(service, devctl_home, world, clock):
    world.add_legacy_workload(LEGACY_PID, LEGACY_START)
    write_legacy(devctl_home, clock)
    world.spawn_error = DevctlError(SUPERVISOR_START_FAILED, "fork failed")
    res = service.env_down(cwd="/proj/webapp", reason="session-end", session="old")
    (row,) = res["downed"]
    assert row["pending"] is True
    assert "next `rent sweep` resumes it" in row["detail"]
    # …and it does.
    world.spawn_error = None
    service.env_sweep()
    assert lease_at(devctl_home, "/proj/webapp") is None


def test_down_on_cleanup_incomplete_waits_for_the_retry_it_asks_for(service, devctl_home, world):
    """§8: a request on `cleanup_incomplete` triggers an immediate retry; the
    answer is that retry's outcome, not the previous attempt's record."""
    world.stubborn = True
    service.env_up("webapp", cwd="/proj/A")
    first = service.env_down("webapp", cwd="/proj/A")
    assert first["stopped"] is False
    again = service.env_down("webapp", cwd="/proj/A")      # retried, still stuck
    assert again["stopped"] is False
    assert lease_at(devctl_home, "/proj/A").cleanup.attempts == 2
    for proc in world.table.procs.values():
        proc.unkillable = proc.ignores_term = False         # the survivor becomes stoppable
    last = service.env_down("webapp", cwd="/proj/A")
    assert last["stopped"] is True
    assert lease_at(devctl_home, "/proj/A") is None


def test_down_all_runs_unsupervised_recoveries_concurrently(
    service, devctl_home, write_registry, sample_registry_data, world
):
    sample_registry_data["projects"]["worldcup"] = {
        "block": 5190, "runner": "process",
        "profiles": {"default": {"cmd": "npm run dev", "cwd": "/tmp/wc", "port_env": "PORT"}},
    }
    write_registry(sample_registry_data)
    service.env_up("webapp", cwd="/proj/A")
    service.env_up("worldcup", cwd="/proj/A")
    world.kill_supervisor(lease_key("webapp", "/proj/A"))
    world.kill_supervisor(lease_key("worldcup", "/proj/A"))
    res = service.env_down(cwd="/proj/A")
    assert [d["stopped"] for d in res["downed"]] == [True, True]
    assert devctl_home.project_lease_files("webapp") == []


def test_a_supervisor_that_dies_mid_wait_is_recovered_by_the_waiter(
    service, devctl_home, world, monkeypatch
):
    service.env_up("webapp", cwd="/proj/A")
    key = lease_key("webapp", "/proj/A")
    world.hung = True
    ticks = {"n": 0}
    real_sleep = world.sleep

    def sleep_then_lose_it(seconds):
        ticks["n"] += 1
        if ticks["n"] == 3:
            world.kill_supervisor(key)
        real_sleep(seconds)

    world.sleep = sleep_then_lose_it
    res = service.env_down("webapp", cwd="/proj/A")
    assert res["stopped"] is True
    down = [e for e in events(service) if e["event"] == "down"][-1]
    assert down["mode"] == "recovery" and down["reason"] == "explicit"

