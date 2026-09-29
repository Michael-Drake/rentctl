"""ADR-0016 plan steps 5–7 with REAL processes: the service over real supervisors.

`env_up` spawns a real ``python -m rentctl.supervisor``, `env_down` writes a
real stop request and sends the real verified wake, and `ls`/`sweep` run the
§10 table against the real process table. The CLI tests run the real
``python -m rentctl.cli`` as a subprocess with isolated state.

Every workload binds only an ephemeral loopback port (never 5100–5129), every
helper has a lifetime cap, and every session is accounted for by the
``workload_sessions`` guard — which counts each supervisor itself, so a
supervisor left running fails the test.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest

from rentctl.core import procutil, supervision
from rentctl.core import service as service_mod
from rentctl.core.events import EventLog
from rentctl.core.leases import Lease, SupervisorRef
from rentctl.core.models import SID_OWNER_WORKLOAD, WorkloadIdentity
from rentctl.core.paths import lease_key
from rentctl.core.registry import RegistryProfile
from rentctl.core.runners import ProcessRunner
from rentctl.core.service import Service, _now_local
from suphelp import (
    DECOY,
    LOOPSERVE,
    PLAIN,
    PROJECT,
    PY,
    SERVER,
    Harness,
    answers,
    free_block,
    now,
    read_pid,
    wait_until,
)

pytestmark = pytest.mark.integration

TERM_GRACE = 1.0
KILL_GRACE = 2.0


class Env:
    """A real service over a registry this test writes, in isolated state."""

    def __init__(self, paths, tmp: Path, write_registry, sessions: list) -> None:
        self.paths = paths
        self.tmp = tmp
        self.write_registry = write_registry
        self.sessions = sessions
        self.projects: dict[str, dict] = {}
        self.used: set[int] = set()
        self.srv = tmp / "server.py"
        self.srv.write_text(SERVER)
        self.plain = tmp / "plain.py"
        self.plain.write_text(PLAIN)
        self.cwd = tmp / "proj"
        self.cwd.mkdir()
        self.svc = self.service()

    def service(self, **kw) -> Service:
        kw.setdefault("readiness_timeout", 30.0)
        return Service(
            self.paths, term_grace_s=TERM_GRACE, kill_grace_s=KILL_GRACE,
            session_id_fn=lambda: "itest", **kw,
        )

    def server_cmd(self, *extra: str) -> str:
        # Double quotes around the pid path: the shell must expand $PORT there.
        return f"'{PY}' '{self.srv}' \"{self.tmp}/srv-$PORT.pid\" {' '.join(extra)}"

    def stubborn_cmd(self) -> str:
        """A server plus a SIGTERM-ignoring child: the stop has to escalate."""
        return f"{self.server_cmd()} & '{PY}' '{self.plain}' \"{self.tmp}/ign-$PORT.pid\" ign & wait"

    def ignoring(self, port: int) -> int:
        """Wait for a stubborn_cmd's TERM-ignoring child. Its pid file is written
        after it installs SIG_IGN, so a stop that follows really must escalate."""
        return read_pid(self.tmp / f"ign-{port}.pid")

    def enroll(self, name: str, cmd: str) -> int:
        block = free_block(avoid=self.used)
        self.used |= set(range(block, block + 10))
        self.projects[name] = {
            "block": block, "runner": "process",
            "profiles": {"default": {"cmd": cmd, "cwd": str(self.cwd), "port_env": "PORT"}},
        }
        self.write_registry({"projects": self.projects})
        return block

    def lease(self, name: str, cwd: Path | None = None) -> Lease | None:
        return Lease.read_if_exists(self.paths.lease_file_for(name, str(cwd or self.cwd)))

    def events(self, kind: str | None = None) -> list[dict]:
        rows = EventLog(self.paths.events_file).read()
        return [r for r in rows if kind is None or r["event"] == kind]

    def cli(
        self, *argv: str, timeout: float = 30.0, stdin: str | None = None,
        session_env: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess, float]:
        """Run the real CLI. ``stdin`` is what a hook would pipe it (ADR-0017 §2);
        without it stdin is empty. The session variables of whatever session runs
        this suite are stripped, so identity is only what the test hands over."""
        env = {
            k: v for k, v in os.environ.items() if k not in service_mod.SESSION_ID_ENVS
        }
        env.update(
            RENTCTL_STATE_HOME=str(self.paths.state_home),
            RENTCTL_CONFIG_HOME=str(self.paths.config_home),
            **(session_env or {}),
        )
        t0 = time.monotonic()
        proc = subprocess.run([PY, "-m", "rentctl.cli", *argv], capture_output=True, text=True,
                              env=env, timeout=timeout, input=stdin or "")
        return proc, time.monotonic() - t0

    def track(self, ref: SupervisorRef | None) -> None:
        """Register a supervisor this process did not spawn (a CLI subprocess did)."""
        if ref is not None:
            self.sessions.append(WorkloadIdentity(ref.pid, ref.start_time, SID_OWNER_WORKLOAD))

    def members(self, lease: Lease) -> list[procutil.ProcRow]:
        ident = lease.ownership()
        return procutil.session_members(ident.sid, ident.owner_start, ident.exclude)

    def down_everything(self) -> None:
        for name in list(self.projects):
            self.svc.env_down(name, all_instances=True, wait_s=15)


@pytest.fixture
def env(devctl_home, tmp_path, write_registry, workload_sessions):
    e = Env(devctl_home, tmp_path, write_registry, workload_sessions)
    try:
        yield e
    finally:
        e.down_everything()


def sigkill_supervisor(lease: Lease) -> None:
    """SIGKILL a supervisor this test's service spawned — verified pid + start."""
    sup = lease.supervisor
    res = procutil.verified_signal(sup.pid, sup.start_time, sup.pid, signal.SIGKILL)
    assert res is procutil.SignalResult.SIGNALLED, res
    assert wait_until(lambda: procutil.observe_start_time(sup.pid) is None, 10)


# --- §6: env_up over a real supervisor ---------------------------------------------

def test_up_runs_under_a_supervisor_that_owns_the_session(env):
    env.enroll("demo", env.server_cmd())
    res = env.svc.env_up("demo", cwd=str(env.cwd))
    assert res["ok"] is True, res
    lease = env.lease("demo")
    assert lease.state == "running" and lease.supervisor.registered
    assert res["supervisor_pid"] == lease.supervisor.pid
    srv = read_pid(env.tmp / f"srv-{res['port']}.pid")
    assert os.getsid(srv) == lease.supervisor.pid  # born into S (arrangement A)
    assert answers(res["port"])
    (up,) = env.events("up")  # written once, by the supervisor
    assert up["supervisor_pid"] == lease.supervisor.pid
    down = env.svc.env_down("demo", cwd=str(env.cwd))
    assert down["stopped"] is True
    assert env.lease("demo") is None and not answers(res["port"])


# --- #7, recovery half ----------------------------------------------------------------

def test_supervisor_killed_while_running_becomes_unsupervised_then_down_recovers(env):
    env.enroll("demo", env.stubborn_cmd())
    res = env.svc.env_up("demo", cwd=str(env.cwd))
    assert res["ok"] is True, res
    env.ignoring(res["port"])
    lease = env.lease("demo")
    sigkill_supervisor(lease)

    (row,) = env.svc.env_ls()["environments"]
    assert row["state"] == "unsupervised"
    assert row["supervisor"]["alive"] is False
    (lost,) = env.events("supervisor_lost")
    assert lost["detected_by"] == "ls" and lost["state_before"] == "running"
    assert answers(res["port"])  # not killed early: the server keeps serving
    assert env.members(lease)

    down = env.svc.env_down("demo", cwd=str(env.cwd))
    assert down["stopped"] is True, down
    assert env.members(lease) == []
    rec = env.events("down")[-1]
    assert rec["mode"] == "recovery" and rec["cleanup"] == "verified" and rec["escalated"] is True
    assert env.lease("demo") is None


def test_supervisor_killed_after_launch_before_running_recovered_by_sweep(
    devctl_home, tmp_path, workload_sessions
):
    """#7: fault point `after_launch`, SIGKILL, then `sweep` recovers the session
    and writes `startup_failed`, which a later sweep cleans."""
    h = Harness(devctl_home, tmp_path, workload_sessions)
    srv = h.script("server", SERVER)
    run = h.start(f"'{PY}' '{srv}' '{tmp_path}/srv.pid'",
                  extra_env={"RENTCTL_TEST_FAULT": "after_launch"})
    try:
        assert wait_until(lambda: "sid" in (getattr(run.lease(), "handle", None) or {}), 15)
        read_pid(tmp_path / "srv.pid")
        run.sigkill()
        lease = run.lease()
        assert lease.state == "starting"

        class Clock:
            t = now()

            def __call__(self):
                return self.t

        clock = Clock()
        svc = Service(devctl_home, now_fn=clock, term_grace_s=TERM_GRACE, kill_grace_s=KILL_GRACE)
        res = svc.env_sweep()
        assert [s["action"] for s in res["swept"]] == ["recover"]
        failed = run.lease()
        assert failed.state == "startup_failed" and failed.error["phase"] == "supervisor_lost"
        ident = lease.ownership()
        assert procutil.session_members(ident.sid, ident.owner_start, ident.exclude) == []
        clock.t = clock.t + timedelta(seconds=31)
        svc.env_sweep()
        assert run.lease() is None
    finally:
        h.cleanup()


# --- #8 -------------------------------------------------------------------------------

def test_cli_sigkilled_during_startup_supervisor_completes(env):
    """The CLI is SIGKILLed once `starting` (with its supervisor) is on disk.
    Ownership is not abandoned: the supervisor reaches `running`, owned and
    with an expiry, and a later `down` stops it."""
    env.enroll("demo", f"sleep 1; {env.server_cmd()}")
    cli = subprocess.Popen(
        [PY, "-m", "rentctl.cli", "up", "demo", "--cwd", str(env.cwd)],
        env={**os.environ, "RENTCTL_STATE_HOME": str(env.paths.state_home),
             "RENTCTL_CONFIG_HOME": str(env.paths.config_home)},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        assert wait_until(
            lambda: (l := env.lease("demo")) is not None and l.state == "starting"
            and l.supervisor is not None, 20,
        )
        env.track(env.lease("demo").supervisor)
        cli.kill()
        cli.wait(timeout=10)
        assert cli.returncode == -signal.SIGKILL
    finally:
        if cli.poll() is None:  # pragma: no cover - cleanup on failure
            cli.kill()
            cli.wait()
    assert wait_until(lambda: getattr(env.lease("demo"), "state", None) == "running", 20)
    lease = env.lease("demo")
    assert supervision.supervisor_alive(lease.supervisor, lease_key("demo", str(env.cwd)))
    assert lease.expires > _now_local()
    assert answers(lease.port)
    assert env.svc.env_down("demo", cwd=str(env.cwd))["stopped"] is True


# --- #9 -------------------------------------------------------------------------------

def test_concurrent_downs_real(env):
    """Five parallel downs: one request, one `down` event, every caller told
    `stopped: true`."""
    env.enroll("demo", env.server_cmd())
    assert env.svc.env_up("demo", cwd=str(env.cwd))["ok"] is True
    results: list[dict] = []
    lock = threading.Lock()

    def call():
        res = env.svc.env_down("demo", cwd=str(env.cwd))
        with lock:
            results.append(res)

    threads = [threading.Thread(target=call) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(results) == 5
    assert [r["stopped"] for r in results] == [True] * 5, results
    assert len(env.events("stop_requested")) == 1
    assert len(env.events("down")) == 1


# --- #12 ------------------------------------------------------------------------------

def test_decoy_pid_with_wrong_start_time_never_signalled(env, tmp_path):
    """A real decoy placed as `supervisor.pid`, `handle.pid` and a legacy
    `watchdog_pid`: `down`, `sweep` and the wake never signal it."""
    marks = tmp_path / "decoy.signals"
    script = tmp_path / "decoy.py"
    script.write_text(DECOY)
    decoy = subprocess.Popen([PY, str(script), str(marks)])
    try:
        assert wait_until(marks.exists, 10)
        start = procutil.observe_start_time(decoy.pid)
        env.enroll("demo", env.server_cmd())
        key = lease_key("demo", str(env.cwd))
        path = env.paths.lease_file(key)

        # 1. A schema-2 lease naming the decoy as its supervisor and session:
        #    once with a wrong start time, once with the right one (then the
        #    cmdline check is what refuses it).
        for sup_start in (start + 3600.0, start):
            wrong = start + 3600.0
            lease = Lease(
                project="demo", profile="default", runner="process",
                handle={"pid": decoy.pid, "pid_start_time": wrong, "sid": decoy.pid,
                        "sid_owner_start_time": wrong, "sid_owner": "supervisor"},
                port=env.projects["demo"]["block"], session="s", cwd=str(env.cwd),
                created=_now_local(), expires=_now_local() + timedelta(hours=1), log="/dev/null",
                schema=2, generation="d" * 32, state="running", state_since=_now_local(),
                plan={"cmd": "true", "cwd": str(env.cwd), "port_env": "PORT"},
                supervisor=SupervisorRef(decoy.pid, sup_start, registered=True),
            )
            lease.write(path)
            assert supervision.wake_supervisor(lease.supervisor, key) is False
            assert env.svc.env_down("demo", cwd=str(env.cwd))["stopped"] is True
            lease.write(path)
            env.svc.env_sweep()
            assert not path.exists()

        # 2. A legacy lease naming it as leader and as a verifiable-looking watchdog.
        legacy = Lease(
            project="demo", profile="default", runner="process",
            handle={"pid": decoy.pid, "pid_start_time": start + 3600.0},
            port=env.projects["demo"]["block"], session="s", cwd=str(env.cwd),
            created=_now_local(), expires=_now_local() - timedelta(minutes=1), log="/dev/null",
            watchdog_pid=decoy.pid, watchdog_pid_start_time=start,
        )
        legacy.write(path)
        env.svc.env_sweep()
        legacy.write(path)
        env.svc.env_down("demo", cwd=str(env.cwd), reason="session-end")
        env.svc.env_down("demo", cwd=str(env.cwd))

        assert decoy.poll() is None, "the decoy died"
        assert marks.read_text() == "", f"the decoy was signalled: {marks.read_text()!r}"
    finally:
        decoy.kill()
        decoy.wait(10)


# --- #13 ------------------------------------------------------------------------------

def test_worktrees_isolated_under_supervisor(env, tmp_path):
    env.enroll("demo", env.server_cmd())
    lane_a, lane_b = tmp_path / "lane-a", tmp_path / "lane-b"
    lane_a.mkdir()
    lane_b.mkdir()
    a = env.svc.env_up("demo", cwd=str(lane_a))
    b = env.svc.env_up("demo", cwd=str(lane_b))
    assert a["ok"] and b["ok"] and a["port"] != b["port"]
    assert env.svc.env_down("demo", cwd=str(lane_a))["stopped"] is True
    assert not answers(a["port"])
    assert answers(b["port"])  # the sibling keeps serving
    lease_b = env.lease("demo", lane_b)
    assert lease_b.state == "running"
    assert supervision.supervisor_alive(lease_b.supervisor, lease_key("demo", str(lane_b)))
    assert env.svc.env_down("demo", cwd=str(lane_b))["stopped"] is True


def test_foreign_listener_never_killed(env, tmp_path):
    """A foreign server on a block port: the draw routes around it; a readiness
    probe it answers is attributed by session and fails the start without
    touching it; the board reports it as a squatter and nothing kills it."""
    block = env.enroll("demo", env.server_cmd())
    slow_block = env.enroll("slow", "sleep 30")
    foreign_cmd = [PY, LOOPSERVE]
    foreign = subprocess.Popen([*foreign_cmd, str(block)], cwd=tmp_path,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    foreign2 = None
    try:
        assert wait_until(lambda: answers(block), 15)
        res = env.svc.env_up("demo", cwd=str(env.cwd))
        assert res["ok"] is True and res["port"] != block

        # Readiness answered by a stranger: the slow project never binds, and
        # a foreign server takes its drawn port while it is `starting`.
        slow = env.service(readiness_timeout=3.0)
        out: list[dict] = []
        t = threading.Thread(target=lambda: out.append(slow.env_up("slow", cwd=str(env.cwd))))
        t.start()
        assert wait_until(lambda: (l := env.lease("slow")) is not None and l.state == "starting", 15)
        port = env.lease("slow").port
        assert port == slow_block
        foreign2 = subprocess.Popen([*foreign_cmd, str(port)], cwd=tmp_path,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        t.join(40)
        (up,) = out
        assert up["ok"] is False and up["error"] == "START_TIMEOUT"
        assert up["phase"] == "foreign_listener", up
        assert env.lease("slow") is None

        sweep = env.svc.env_sweep()
        squatters = {s["port"]: s for s in sweep.get("squatters", [])}
        assert squatters[block]["pid"] == foreign.pid
        assert "killed_squatters" not in sweep
        assert foreign.poll() is None and foreign2.poll() is None
        assert answers(block) and answers(port)
    finally:
        for p in (foreign, foreign2):
            if p is not None:
                p.terminate()
                p.wait(10)


def test_strict_reclaim_skips_starting_lease_listener(devctl_home, tmp_path, write_registry,
                                                     workload_sessions):
    """S4, real: a `starting` lease whose workload is already listening (parked
    after readiness, before the `running` write) is never touched by a strict
    sweep — neither from the board snapshot nor from a snapshot row that raced
    the lease (the row is re-decided under L and refused as `leased`)."""
    block = free_block()
    write_registry({
        "enforcement": "strict",
        "projects": {PROJECT: {
            "block": block, "runner": "process",
            "profiles": {"default": {"cmd": "true", "cwd": str(tmp_path), "port_env": "PORT"}},
        }},
    })
    h = Harness(devctl_home, tmp_path, workload_sessions)
    srv = h.script("server", SERVER)
    run = h.start(f"'{PY}' '{srv}' '{tmp_path}/srv.pid'", port=block,
                  extra_env={"RENTCTL_TEST_FAULT": "before_running_write"})
    try:
        assert wait_until(lambda: "before_running_write: parked" in run.log_text(), 30), (
            run.log_text()
        )
        pid = read_pid(tmp_path / "srv.pid")
        start = procutil.observe_start_time(pid)
        assert run.state() == "starting" and answers(block)

        svc = Service(devctl_home, term_grace_s=TERM_GRACE, kill_grace_s=KILL_GRACE,
                      port_owner_fn=probe_only({block}))
        res = svc.env_sweep()
        assert "killed_squatters" not in res and "squatters" not in res, res
        # The race the lock closes: a row snapshotted before the lease existed.
        sq = service_mod._Squatter(PROJECT, block, pid, "python", start, os.getsid(pid))
        (row,) = svc._kill_squatters([sq])
        assert row["killed"] is False and row["outcome"] == "leased"
        assert procutil.start_time_matches(start, procutil.observe_start_time(pid))
        assert answers(block) and run.state() == "starting"
        rec = [e for e in env_events(devctl_home) if e["event"] == "squatter_reclaim"]
        assert [r["outcome"] for r in rec] == ["leased"]
    finally:
        # The parked supervisor answers no stop request: SIGKILL it (verified),
        # then the §10 recovery stop empties its session.
        if run.alive():
            run.sigkill()
        Service(devctl_home, term_grace_s=TERM_GRACE, kill_grace_s=KILL_GRACE,
                port_owner_fn=probe_only({block})).env_sweep()
        h.cleanup()


def env_events(paths) -> list[dict]:
    return EventLog(paths.events_file).read()


def probe_only(ports: set[int]):
    """The real listener probe, blind outside ``ports``.

    Every strict-mode service in a real test goes through this. A test block is
    only *free* when drawn; other suites on the same machine draw from the same
    ephemeral range, so a stranger can bind inside it mid-test — and strict
    mode SIGTERMs whatever squats a registered block. Confining the probe to
    the ports this test's own processes hold means strict mode can never be
    shown, let alone signal, a listener the test did not start.
    """
    return lambda port: procutil.port_owner(port) if port in ports else None


def test_strict_reclaim_never_signals_a_listener_in_a_live_leases_session(env):
    """§11 precondition 2 under strict, real: the running workload's second
    listener is in its lease's session — not reported, and a raced snapshot row
    for it is refused under L as `lease_session`."""
    block = env.enroll("demo", "true")
    second = block + 7
    cmd = f"{env.server_cmd()} & PORT={second} '{PY}' '{env.srv}' '{env.tmp}/second.pid' & wait"
    env.projects["demo"]["profiles"]["default"]["cmd"] = cmd
    env.write_registry({"projects": env.projects})
    up = env.svc.env_up("demo", cwd=str(env.cwd))
    assert up["ok"] is True, up
    assert wait_until(lambda: answers(second), 15)
    pid = read_pid(env.tmp / "second.pid")
    start = procutil.observe_start_time(pid)
    env.write_registry({"enforcement": "strict", "projects": env.projects})
    strict = env.service(port_owner_fn=probe_only({up["port"], second}))
    sweep = strict.env_sweep()
    assert "killed_squatters" not in sweep and "squatters" not in sweep, sweep
    sq = service_mod._Squatter("demo", second, pid, "python", start, os.getsid(pid))
    (row,) = strict._kill_squatters([sq])
    assert row["killed"] is False and row["outcome"] == "lease_session"
    assert answers(second)
    assert procutil.start_time_matches(start, procutil.observe_start_time(pid))


def test_a_supervised_workloads_second_listener_is_not_a_squatter(env):
    """§11 precondition 2: a listener in a live lease's session is owned,
    whichever block port it bound."""
    block = env.enroll("demo", "true")
    second = block + 7
    cmd = f"{env.server_cmd()} & PORT={second} '{PY}' '{env.srv}' '{env.tmp}/second.pid' & wait"
    env.projects["demo"]["profiles"]["default"]["cmd"] = cmd
    env.write_registry({"projects": env.projects})
    assert env.svc.env_up("demo", cwd=str(env.cwd))["ok"] is True
    assert wait_until(lambda: answers(second), 15)
    board = env.svc.env_ls()
    assert [e for e in board["environments"] if e.get("status") == "squatter"] == []


# --- S5: the SessionEnd hook inside its 1.5 s budget (R0) ------------------------------

def test_session_end_down_all_returns_within_budget(env):
    """Four leases, each with a SIGTERM-ignoring child. The real hook command
    returns in under 1.5 s with every lease `pending`; afterwards every lease
    reaches `stopped` and every session is empty."""
    for name in ("s5a", "s5b", "s5c", "s5d"):
        env.enroll(name, env.stubborn_cmd())
    leases = []
    for name in env.projects:
        res = env.svc.env_up(name, cwd=str(env.cwd))
        assert res["ok"] is True, res
        env.ignoring(res["port"])
        leases.append(env.lease(name))

    proc, elapsed = env.cli("down", "--all", "--cwd", str(env.cwd), "--reason", "session-end",
                            stdin=json.dumps({"session_id": "itest"}))
    print(f"\nS5 hook return time: {elapsed * 1000:.0f} ms")
    assert proc.returncode == 0, proc.stderr
    assert elapsed < 1.5, f"the hook took {elapsed:.2f}s"
    out = json.loads(proc.stdout)
    assert len(out["downed"]) == 4
    assert all(d["pending"] is True and d["state"] == "stopping" for d in out["downed"]), out
    assert len(env.events("stop_requested")) == 4

    assert wait_until(lambda: all(env.lease(n) is None for n in env.projects), 30)
    for lease in leases:
        assert env.members(lease) == []
    downs = env.events("down")
    assert len(downs) == 4
    assert {d["reason"] for d in downs} == {"session-end"} and {d["layer"] for d in downs} == {2}
    assert all(d["escalated"] is True and d["cleanup"] == "verified" for d in downs)


def test_session_end_hands_a_legacy_lease_to_a_detached_recovery_supervisor(env):
    """R0 for a 1.0.x lease: the hook spawns a recovery supervisor and returns."""
    block = env.enroll("demo", env.server_cmd())
    runner = ProcessRunner(term_grace_s=TERM_GRACE, kill_grace_s=KILL_GRACE)
    prof = RegistryProfile(cmd=env.server_cmd(), cwd=str(env.cwd), port_env="PORT",
                           preferred_offset=0)
    handle = runner.start(prof, block, env.tmp / "legacy.log")
    assert wait_until(lambda: answers(block), 15)
    path = env.paths.lease_file_for("demo", str(env.cwd))
    Lease(
        project="demo", profile="default", runner="process",
        handle={"pid": handle.pid, "pid_start_time": handle.pid_start_time}, port=block,
        session="old", cwd=str(env.cwd), created=_now_local(),
        expires=_now_local() + timedelta(hours=1), log=str(env.tmp / "legacy.log"),
    ).write(path)

    proc, elapsed = env.cli("down", "--all", "--cwd", str(env.cwd), "--reason", "session-end",
                            stdin=json.dumps({"session_id": "old"}))
    assert proc.returncode == 0, proc.stderr
    assert elapsed < 1.5, elapsed
    (row,) = json.loads(proc.stdout)["downed"]
    assert row["pending"] is True and "recovery supervisor" in row["detail"]
    rec_pid = int(row["detail"].split("recovery supervisor ")[1].split()[0])
    started = procutil.observe_start_time(rec_pid)
    if started is not None:
        env.track(SupervisorRef(rec_pid, started))
    assert wait_until(lambda: not path.exists(), 30)
    assert not answers(block)
    down = env.events("down")[-1]
    assert down["mode"] == "recovery" and down["reason"] == "session-end"


# --- migration (§14) --------------------------------------------------------------------

# --- ADR-0017: two sessions, one checkout, the real CLI end to end ----------------------

def test_e2e_session_end_releases_and_only_the_last_release_stops(env):
    """Session A starts the server, session B shares it, each through the real
    `rent up`. A's real SessionEnd hook command (stdin id A) leaves B's server
    serving; B's then stops it and the port stops answering. Both hooks return
    inside the 1.5 s budget."""
    env.enroll("demo", env.server_cmd())

    proc, _ = env.cli("up", "demo", "--cwd", str(env.cwd),
                      session_env={"CLAUDE_CODE_SESSION_ID": "A"})
    assert proc.returncode == 0, proc.stderr
    up_a = json.loads(proc.stdout)
    env.track(env.lease("demo").supervisor)
    port = up_a["port"]
    assert up_a["already_running"] is False and answers(port)

    proc, _ = env.cli("up", "demo", "--cwd", str(env.cwd),
                      session_env={"CLAUDE_CODE_SESSION_ID": "B"})
    assert proc.returncode == 0, proc.stderr
    up_b = json.loads(proc.stdout)
    assert up_b["already_running"] is True and up_b["port"] == port
    assert up_b["shared_with"] == ["A"]
    assert set(env.lease("demo").claims) == {"A", "B"}

    # A's hook. The env names a different session on purpose: the hook's
    # contract channel is stdin, and it must win.
    proc, elapsed = env.cli("down", "--all", "--cwd", str(env.cwd), "--reason", "session-end",
                            stdin=json.dumps({"session_id": "A", "reason": "logout"}),
                            session_env={"CLAUDE_CODE_SESSION_ID": "not-A"})
    assert proc.returncode == 0, proc.stderr
    assert elapsed < 1.5, elapsed
    (row,) = json.loads(proc.stdout)["downed"]
    assert row["stopped"] is False and row["released"] is True and row["held_by"] == ["B"]
    time.sleep(1.5)  # a supervisor that had been asked to stop would be well into it
    lease = env.lease("demo")
    assert lease is not None and lease.state == "running" and lease.stop is None
    assert answers(port)
    assert env.events("down") == []

    proc, elapsed = env.cli("down", "--all", "--cwd", str(env.cwd), "--reason", "session-end",
                            stdin=json.dumps({"session_id": "B"}))
    assert proc.returncode == 0, proc.stderr
    assert elapsed < 1.5, elapsed
    (row,) = json.loads(proc.stdout)["downed"]
    assert row["released_by"] == "B"
    assert wait_until(lambda: env.lease("demo") is None, 20)
    assert wait_until(lambda: not answers(port), 10)
    assert env.members(lease) == []
    (down,) = env.events("down")
    assert down["reason"] == "session-end" and down["layer"] == 2
    assert down["killed"] is True and down["released_by"] == "B"


def test_legacy_lease_stop_uses_session_membership(env):
    """S1's shape on a 1.0.x lease: the leader shell exits, a SIGTERM-ignoring
    child keeps the session. `down` finds the child by the old leader's SID and
    stops it, verified — the 1.0.x runner returned once the shell was gone."""
    block = env.enroll("demo", env.server_cmd())
    runner = ProcessRunner(term_grace_s=TERM_GRACE, kill_grace_s=KILL_GRACE)
    cmd = f"'{PY}' '{env.plain}' '{env.tmp}/ign.pid' ign & exit 0"
    prof = RegistryProfile(cmd=cmd, cwd=str(env.cwd), port_env="PORT", preferred_offset=0)
    handle = runner.start(prof, block, env.tmp / "legacy.log")
    ign = read_pid(env.tmp / "ign.pid")
    assert wait_until(lambda: procutil.observe_start_time(handle.pid) is None, 10)
    assert os.getsid(ign) == handle.pid
    path = env.paths.lease_file_for("demo", str(env.cwd))
    Lease(
        project="demo", profile="default", runner="process",
        handle={"pid": handle.pid, "pid_start_time": handle.pid_start_time}, port=block,
        session="old", cwd=str(env.cwd), created=_now_local(),
        expires=_now_local() + timedelta(hours=1), log=str(env.tmp / "legacy.log"),
    ).write(path)

    res = env.svc.env_down("demo", cwd=str(env.cwd))
    assert res["stopped"] is True and res["was_running"] is True, res
    assert procutil.observe_start_time(ign) is None
    assert not path.exists()
    down = env.events("down")[-1]
    assert down["mode"] == "recovery" and down["escalated"] is True


def test_legacy_lease_renewed_in_legacy_format_real(env):
    block = env.enroll("demo", env.server_cmd())
    runner = ProcessRunner(term_grace_s=TERM_GRACE, kill_grace_s=KILL_GRACE)
    prof = RegistryProfile(cmd=env.server_cmd(), cwd=str(env.cwd), port_env="PORT",
                           preferred_offset=0)
    handle = runner.start(prof, block, env.tmp / "legacy.log")
    assert wait_until(lambda: answers(block), 15)
    path = env.paths.lease_file_for("demo", str(env.cwd))
    old_expires = _now_local() + timedelta(minutes=5)
    Lease(
        project="demo", profile="default", runner="process",
        handle={"pid": handle.pid, "pid_start_time": handle.pid_start_time}, port=block,
        session="old", cwd=str(env.cwd), created=_now_local(), expires=old_expires,
        log=str(env.tmp / "legacy.log"),
    ).write(path)
    res = env.svc.env_up("demo", cwd=str(env.cwd))
    assert res["already_running"] is True and res["pid"] == handle.pid
    raw = json.loads(path.read_text())
    assert "schema" not in raw
    assert Lease.read(path).expires > old_expires
    assert env.svc.env_down("demo", cwd=str(env.cwd))["stopped"] is True
    assert not answers(block)
