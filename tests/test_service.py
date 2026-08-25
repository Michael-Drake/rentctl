"""Service-layer tests: the four operations' decision logic, driven with a
FakeRunner + controllable clock so every branch is fast and deterministic.

Real-process end-to-end behavior (process-group kill, concurrency, PID-recycle
refusal against a live server) lives in test_integration.py.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from rentctl.core import procutil
from rentctl.core import service as service_mod
from rentctl.core.errors import (
    BLOCK_EXHAUSTED,
    INVALID_CWD,
    PROFILE_MISMATCH,
    REGISTRY_INVALID,
    START_TIMEOUT,
    UNKNOWN_PROJECT,
)
from rentctl.core.leases import Lease
from rentctl.core.models import ProcessHandle, ProcInfo, Readiness
from rentctl.core.paths import lease_key
from rentctl.core.service import Service

CDT = timezone(timedelta(hours=-5))


class FakeRunner:
    """Deterministic stand-in for the process runner. start_time == pid, so a
    handle with a mismatched start_time reads as recycled/dead."""

    name = "process"

    def __init__(self) -> None:
        self.started: list[int] = []
        self.stopped: list[int] = []
        # The directory each start was handed (ADR-0010). Without this a fake
        # runner agrees with a service that spawns in entirely the wrong place.
        self.start_cwds: list[str] = []
        self._alive: dict[int, bool] = {}
        self._next_pid = 1000

    def start(self, entry, port, log_path):
        pid = self._next_pid
        self._next_pid += 1
        self._alive[pid] = True
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(f"started {entry.cmd} on {port}\nline2\n")
        self.started.append(pid)
        self.start_cwds.append(entry.cwd)
        return ProcessHandle(pid=pid, pid_start_time=float(pid))

    def stop(self, handle):
        self.stopped.append(handle.pid)
        self._alive[handle.pid] = False

    def alive(self, handle):
        return self._alive.get(handle.pid, False) and handle.pid_start_time == float(handle.pid)

    def orphans(self):
        return []


class Clock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw):
        self.now = self.now + timedelta(**kw)


@pytest.fixture
def fake_runner():
    return FakeRunner()


@pytest.fixture
def clock():
    return Clock(datetime(2026, 7, 14, 8, 0, tzinfo=CDT))


@pytest.fixture
def service(devctl_home, write_registry, sample_registry_data, fake_runner, clock, monkeypatch):
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
    # webapp's dev server live on 5180, a fake runner's environment reported
    # itself healthy because *something else* answered.
    monkeypatch.setattr(service_mod, "_port_answering", lambda port: False)

    spawned: list[str] = []

    def watchdog_spawn(project):
        spawned.append(project)
        return 424242

    svc = Service(
        devctl_home,
        now_fn=clock,
        runner_factory=lambda name: fake_runner,
        readiness_fn=lambda port, timeout, pgid: Readiness.ANSWERED,
        watchdog_spawn=watchdog_spawn,
        session_id_fn=lambda: "sess-1",
    )
    svc._spawned = spawned  # type: ignore[attr-defined]
    return svc


# --- env_up ---------------------------------------------------------------

def test_up_fresh(service, devctl_home, fake_runner):
    res = service.env_up("webapp", cwd="/proj/webapp")
    assert res["ok"] is True
    assert res["already_running"] is False
    assert res["port"] == 5180
    assert res["url"] == "http://localhost:5180"
    assert res["pid"] == 1000
    lease = Lease.read(devctl_home.lease_file_for("webapp", "/proj/webapp"))
    assert lease.watchdog_pid == 424242
    assert lease.session == "sess-1"
    assert lease.cwd == "/proj/webapp"
    # The watchdog is spawned for the lease key, not the project (ADR-0007 §5).
    assert service._spawned == [lease_key("webapp", "/proj/webapp")]


def test_up_profile_port(service):
    res = service.env_up("webapp", profile="api-only")
    assert res["port"] == 5181


def test_up_already_running_renews(service, clock, fake_runner):
    first = service.env_up("webapp")
    clock.advance(minutes=30)
    second = service.env_up("webapp", lease_minutes=120)
    assert second["already_running"] is True
    assert second["pid"] == first["pid"]
    assert len(fake_runner.started) == 1  # not restarted
    assert second["lease_expires"] > first["lease_expires"]  # pushed out


def test_up_replaces_dead_lease(service, devctl_home, fake_runner):
    service.env_up("webapp")
    # Simulate the process dying: mark the pid dead in the fake runner.
    fake_runner._alive[1000] = False
    res = service.env_up("webapp")
    assert res["already_running"] is False
    assert res["pid"] == 1001            # a fresh process
    assert len(fake_runner.started) == 2


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


def test_up_start_timeout(devctl_home, write_registry, sample_registry_data, fake_runner, clock):
    write_registry(sample_registry_data)
    svc = Service(
        devctl_home,
        now_fn=clock,
        runner_factory=lambda name: fake_runner,
        readiness_fn=lambda port, timeout, pgid: Readiness.NOT_LISTENING,  # never came up
        watchdog_spawn=lambda p: None,
    )
    res = svc.env_up("webapp")
    assert res["ok"] is False
    assert res["error"] == START_TIMEOUT
    assert res["log_tail"]  # captured tail returned for diagnosis
    assert fake_runner.stopped == [1000]  # the failed process was stopped
    assert not devctl_home.lease_file("webapp").exists()  # no lease left behind


def test_up_unknown_project(service):
    res = service.env_up("ghost")
    assert res["ok"] is False
    assert res["error"] == UNKNOWN_PROJECT


def test_up_bad_registry(devctl_home, fake_runner, clock):
    # No registry file written → fail closed.
    svc = Service(
        devctl_home,
        now_fn=clock,
        runner_factory=lambda name: fake_runner,
        readiness_fn=lambda p, t, g: Readiness.ANSWERED,
        watchdog_spawn=lambda p: None,
    )
    res = svc.env_up("webapp")
    assert res["ok"] is False
    assert res["error"] == REGISTRY_INVALID


def test_up_clamps_lease_minutes(service, clock):
    res = service.env_up("webapp", lease_minutes=10_000)
    expires = datetime.fromisoformat(res["lease_expires"])
    assert expires == clock.now + timedelta(minutes=480)  # clamped to max


# --- concurrent worktrees (ADR-0007 lease identity + ADR-0004 port draw) ---

def test_two_worktrees_get_two_leases_and_two_ports(service, devctl_home, fake_runner):
    """The defect this pair of ADRs exists for: before, lane-2 was handed lane-1's
    server under already_running and never reached port selection."""
    one = service.env_up("webapp", cwd="/worktrees/lane-1")
    two = service.env_up("webapp", cwd="/worktrees/lane-2")

    assert one["already_running"] is False
    assert two["already_running"] is False       # not handed lane-1's server
    assert one["port"] == 5180                   # primary keeps the familiar port
    assert two["port"] == 5181                   # sibling draws the next one
    assert one["pid"] != two["pid"]              # two real processes
    assert len(fake_runner.started) == 2
    assert len(devctl_home.project_lease_files("webapp")) == 2


def test_a_lane_teardown_does_not_touch_its_sibling(service, devctl_home, fake_runner):
    """The false-kill path: lane-1's ordinary session end used to kill lane-2's
    server, because one lease carried one cwd."""
    one = service.env_up("webapp", cwd="/worktrees/lane-1")
    two = service.env_up("webapp", cwd="/worktrees/lane-2")

    service.env_down(cwd="/worktrees/lane-1", reason="session-end")

    assert one["pid"] in fake_runner.stopped
    assert two["pid"] not in fake_runner.stopped          # sibling untouched
    assert not devctl_home.lease_file_for("webapp", "/worktrees/lane-1").exists()
    assert devctl_home.lease_file_for("webapp", "/worktrees/lane-2").exists()


def test_each_lane_sees_its_own_env_as_already_running(service, fake_runner):
    service.env_up("webapp", cwd="/worktrees/lane-1")
    service.env_up("webapp", cwd="/worktrees/lane-2")
    again = service.env_up("webapp", cwd="/worktrees/lane-2")
    assert again["already_running"] is True
    assert again["port"] == 5181                          # its own, not lane-1's
    assert len(fake_runner.started) == 2                  # nothing restarted


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


def test_a_dead_lanes_port_is_reclaimable(service, fake_runner):
    """Free-ness is derived from liveness, not from a stored free-list."""
    first = service.env_up("webapp", cwd="/worktrees/lane-1")
    fake_runner._alive[first["pid"]] = False      # crashed, lease file lingers
    assert service.env_up("webapp", cwd="/worktrees/lane-2")["port"] == 5180


def test_symlinked_cwd_is_one_instance_not_two(service, tmp_path, fake_runner):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    first = service.env_up("webapp", cwd=str(real))
    second = service.env_up("webapp", cwd=str(link))
    assert second["already_running"] is True
    assert second["pid"] == first["pid"]
    assert len(fake_runner.started) == 1


# --- env_down -------------------------------------------------------------

def test_down_project(service, devctl_home, fake_runner):
    service.env_up("webapp", cwd="/proj/webapp")
    res = service.env_down("webapp", cwd="/proj/webapp")
    assert res["ok"] is True
    assert res["was_running"] is True
    assert 1000 in fake_runner.stopped
    assert not devctl_home.lease_file_for("webapp", "/proj/webapp").exists()


def test_down_not_running_is_idempotent(service):
    res = service.env_down("webapp")  # never upped
    assert res["ok"] is True
    assert res["was_running"] is False


def test_down_by_project_scopes_to_this_cwd(service, devctl_home, fake_runner):
    """An LLM finishing with its own dev server must not reach into a sibling
    lane (ADR-0007 §3)."""
    one = service.env_up("webapp", cwd="/worktrees/lane-1")
    two = service.env_up("webapp", cwd="/worktrees/lane-2")
    service.env_down("webapp", cwd="/worktrees/lane-1")
    assert one["pid"] in fake_runner.stopped
    assert two["pid"] not in fake_runner.stopped
    assert devctl_home.lease_file_for("webapp", "/worktrees/lane-2").exists()


def test_down_all_instances_is_opt_in(service, devctl_home, fake_runner):
    one = service.env_up("webapp", cwd="/worktrees/lane-1")
    two = service.env_up("webapp", cwd="/worktrees/lane-2")
    res = service.env_down("webapp", all_instances=True)
    assert res["ok"] is True
    assert {d["cwd"] for d in res["downed"]} == {"/worktrees/lane-1", "/worktrees/lane-2"}
    assert one["pid"] in fake_runner.stopped
    assert two["pid"] in fake_runner.stopped
    assert devctl_home.project_lease_files("webapp") == []


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
    assert not devctl_home.lease_file_for("webapp", "/proj/A").exists()
    assert devctl_home.lease_file_for("worldcup", "/proj/B").exists()  # untouched


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

def test_ls_lists_kept(service):
    service.env_up("webapp")
    res = service.env_ls()
    assert res["ok"] is True
    envs = {e["project"]: e for e in res["environments"]}
    assert envs["webapp"]["port"] == 5180
    assert envs["webapp"]["healthy"] is False  # fake server binds no real port


def test_ls_cleans_dead_lease(service, devctl_home, fake_runner):
    service.env_up("webapp")
    fake_runner._alive[1000] = False  # process died
    res = service.env_ls()
    assert res["environments"] == []
    assert not devctl_home.lease_file("webapp").exists()  # reconciled away


# --- env_sweep ------------------------------------------------------------

def test_sweep_removes_dead(service, devctl_home, fake_runner):
    service.env_up("webapp")
    fake_runner._alive[1000] = False
    res = service.env_sweep()
    assert [s["action"] for s in res["swept"]] == ["clean"]
    assert not devctl_home.lease_file("webapp").exists()


def test_sweep_expires_live_lease(service, devctl_home, clock, fake_runner):
    service.env_up("webapp", lease_minutes=120)
    clock.advance(minutes=121)  # past expiry, process still alive
    res = service.env_sweep()
    assert [s["action"] for s in res["swept"]] == ["expire"]
    assert 1000 in fake_runner.stopped
    assert not devctl_home.lease_file("webapp").exists()


def test_sweep_keeps_healthy(service):
    service.env_up("webapp")
    res = service.env_sweep()
    assert res["swept"] == []
    assert [k["project"] for k in res["kept"]] == ["webapp"]


# --- a teardown that did not work must not report one (WI-0031) -----------

class StubbornRunner(FakeRunner):
    """A runner whose `stop()` signals and achieves nothing.

    Not contrived: `ProcessRunner.stop()` returns None and swallows a failed
    signal, and a process in uninterruptible sleep (blocked on a mounted volume,
    which is how this machine serves projects) survives SIGKILL past the grace
    window. There was no test in the suite for a stop that fails."""

    def stop(self, handle):
        self.stopped.append(handle.pid)   # we tried…
        # …and the process is still there.


@pytest.fixture
def stubborn_service(devctl_home, write_registry, sample_registry_data, clock, monkeypatch):
    write_registry(sample_registry_data)
    monkeypatch.setattr(procutil, "port_owner", lambda port: None)
    monkeypatch.setattr(service_mod, "_port_answering", lambda port: False)
    runner = StubbornRunner()
    svc = Service(
        devctl_home,
        now_fn=clock,
        runner_factory=lambda name: runner,
        readiness_fn=lambda port, timeout, pgid: Readiness.ANSWERED,
        watchdog_spawn=lambda p: None,
        session_id_fn=lambda: "sess-1",
    )
    svc._runner = runner  # type: ignore[attr-defined]
    return svc


def test_a_failed_teardown_keeps_the_lease(stubborn_service, devctl_home):
    """The lease is the only record naming the process. Deleting it over a live
    server is precisely what manufactures an unattributable survivor holding a
    port — the failure devctl exists to prevent, reported as a clean teardown."""
    stubborn_service.env_up("webapp", cwd="/proj/A")
    res = stubborn_service.env_down(project="webapp", cwd="/proj/A")
    assert res["stopped"] is False
    assert "still alive" in res["detail"]
    assert devctl_home.lease_file_for("webapp", "/proj/A").exists()


def test_a_failed_teardown_is_not_recorded_as_a_kill(stubborn_service):
    """`killed` used to be the liveness reading taken BEFORE the attempt — a
    claim about intent. False layer-2 evidence is worse than no evidence."""
    stubborn_service.env_up("webapp", cwd="/proj/A")
    stubborn_service.env_down(project="webapp", cwd="/proj/A")
    down = [e for e in stubborn_service.events.read() if e["event"] == "down"][-1]
    assert down["killed"] is False
    assert down["stop_failed"] is True


def test_sweep_does_not_report_a_survivor_as_swept(stubborn_service, clock, devctl_home):
    """Reporting it as swept is how a live server becomes invisible: gone from
    `kept`, gone from the lease dir, and named nowhere."""
    stubborn_service.env_up("webapp", cwd="/proj/A")
    clock.advance(hours=3)
    res = stubborn_service.env_sweep()
    assert res["swept"] == []
    assert [k["project"] for k in res["kept"]] == ["webapp"]
    assert devctl_home.lease_file_for("webapp", "/proj/A").exists()


def test_a_successful_teardown_still_reports_a_kill(service):
    """The honest path must be unchanged — this is the regression guard for the
    fix itself."""
    service.env_up("webapp", cwd="/proj/A")
    res = service.env_down(project="webapp", cwd="/proj/A")
    assert "stopped" not in res
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
    service, devctl_home, write_registry, sample_registry_data, tmp_path, clock, fake_runner
):
    """The load-bearing property. Refusing to clean up because a config drifted
    would leave a real server running to guard against a command sweep was never
    going to execute — fail-closed and fail-safe point opposite ways here, and
    cleanup follows fail-safe."""
    service.env_up("webapp", cwd="/proj/A")
    root = tmp_path / "proj"
    root.mkdir()
    write_registry(_drifted_registry(sample_registry_data, root))
    clock.advance(hours=3)  # past the lease
    res = service.env_sweep()
    assert res["command_drift"]  # drift seen…
    assert [s["project"] for s in res["swept"]] == ["webapp"]  # …and the sweep still ran
    assert not devctl_home.lease_file_for("webapp", "/proj/A").exists()


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
    devctl_home, write_registry, sample_registry_data, fake_runner, clock, monkeypatch
):
    sample_registry_data["enforcement"] = "strict"
    write_registry(sample_registry_data)
    monkeypatch.setattr(
        procutil,
        "port_owner",
        lambda port: ProcInfo(pid=888, name="vite", cmdline=()) if port == 5180 else None,
    )
    killed: list[int] = []
    monkeypatch.setattr("rentctl.core.service.os.kill", lambda pid, sig: killed.append(pid))
    svc = Service(
        devctl_home,
        now_fn=clock,
        runner_factory=lambda name: fake_runner,
        readiness_fn=lambda p, t, g: Readiness.ANSWERED,
        watchdog_spawn=lambda p: None,
    )
    res = svc.env_sweep()
    assert 888 in killed
    assert any(k["killed"] for k in res["killed_squatters"])


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
    devctl_home, write_registry, sample_registry_data, fake_runner, clock, monkeypatch
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
    svc = Service(
        devctl_home,
        now_fn=clock,
        runner_factory=lambda name: fake_runner,
        readiness_fn=lambda p, t, g: Readiness.ANSWERED,
        watchdog_spawn=lambda p: None,
    )
    res = svc.env_sweep()
    assert killed == []
    assert "killed_squatters" not in res
    assert res["squatter_check"] == "unavailable"


# --- the event log: what actually happened, and which layer did it ---------
# (spec §8 cleanup layers; §11.1 G4 is scored from these records)

def events(service):
    return service.events.read()


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


def test_up_on_running_env_records_a_renewal(service, clock):
    service.env_up("webapp")
    clock.advance(minutes=30)
    service.env_up("webapp")
    first, second = events(service)
    assert first["already_running"] is False
    assert second["already_running"] is True


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
    that layer 2 fired."""
    service.env_up("webapp", cwd="/proj/A")
    service.env_down(cwd="/proj/A")
    rec = events(service)[-1]
    assert rec["reason"] == "session-end"
    assert rec["layer"] == 2
    assert rec["reason_source"] == "inferred"


def test_down_all_with_a_declared_reason_is_layer_2_evidence(service):
    service.env_up("webapp", cwd="/proj/A")
    service.env_down(cwd="/proj/A", reason="session-end")
    rec = events(service)[-1]
    assert rec["layer"] == 2
    assert rec["reason_source"] == "declared"
    assert rec["killed"] is True


def test_down_with_no_lease_records_nothing(service):
    """The SessionEnd hook fires in every session; the ones that leased nothing
    must not bury the real events."""
    service.env_down("webapp")
    service.env_down(cwd="/proj/nothing-here")
    assert events(service) == []


def test_sweep_expiry_is_layer_4_kill(service, clock, fake_runner):
    service.env_up("webapp", lease_minutes=120)
    clock.advance(minutes=121)
    service.env_sweep()
    rec = events(service)[-1]
    assert rec["reason"] == "sweep-expired"
    assert rec["layer"] == 4
    assert rec["op"] == "sweep"
    assert rec["killed"] is True


def test_sweep_of_dead_process_is_recorded_as_no_kill(service, fake_runner):
    service.env_up("webapp")
    fake_runner._alive[1000] = False  # died on its own
    service.env_sweep()
    rec = events(service)[-1]
    assert rec["reason"] == "sweep-dead"
    assert rec["layer"] == 4
    assert rec["killed"] is False


def test_ls_reconcile_is_recorded_under_its_own_op(service, fake_runner):
    """`ls` reconciles too, so it can tear down — the record says which command did."""
    service.env_up("webapp")
    fake_runner._alive[1000] = False
    service.env_ls()
    rec = events(service)[-1]
    assert rec["op"] == "ls"
    assert rec["layer"] == 4


def test_event_log_failure_does_not_break_a_teardown(service, devctl_home, fake_runner):
    """Fail open: if the log cannot be written, the kill still happens."""
    service.env_up("webapp")
    service.events.path = devctl_home.state_dir / "logs" / "webapp-blocked.log" / "events.jsonl"
    (devctl_home.state_dir / "logs" / "webapp-blocked.log").write_text("a file, not a dir")
    res = service.env_down("webapp")
    assert res["ok"] is True
    assert res["was_running"] is True
    assert 1000 in fake_runner.stopped
    assert not devctl_home.lease_file("webapp").exists()


def test_ls_registry_invalid_still_lists_leases(service, devctl_home, monkeypatch):
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
def worktree_service(tmp_path, devctl_home, write_registry, fake_runner, clock):
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
    svc = Service(
        devctl_home,
        now_fn=clock,
        runner_factory=lambda name: fake_runner,
        readiness_fn=lambda port, timeout, pgid: Readiness.ANSWERED,
        watchdog_spawn=lambda key: 424242,
        session_id_fn=lambda: "sess-1",
    )
    return svc, main, lane


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_up_from_a_lane_spawns_in_that_lane(worktree_service, fake_runner, devctl_home):
    """The reported bug: a lane's server must serve the lane, not the main checkout."""
    svc, main, lane = worktree_service
    res = svc.env_up("webapp", cwd=str(lane))
    assert res["ok"] is True
    # What actually got spawned — the whole point.
    assert fake_runner.start_cwds == [str((lane / "frontend").resolve())]
    # ...and it is visible to the caller rather than something they must infer.
    assert res["serving"] == str((lane / "frontend").resolve())
    # The lease still belongs to the lane it was requested from (ADR-0007),
    # which is what teardown matches on.
    lease = Lease.read(devctl_home.lease_file_for("webapp", str(lane)))
    assert lease.cwd == str(lane.resolve())
    assert lease.spawn_cwd == str((lane / "frontend").resolve())


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_up_from_the_main_checkout_is_unchanged(worktree_service, fake_runner):
    """No re-rooting when the caller is the enrolled checkout — same as before."""
    svc, main, lane = worktree_service
    res = svc.env_up("webapp", cwd=str(main))
    assert fake_runner.start_cwds == [str((main / "frontend").resolve())]
    assert res["serving"] == str((main / "frontend").resolve())


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_up_from_an_unrelated_directory_uses_the_approved_cwd(worktree_service, fake_runner, tmp_path):
    """Fail toward the operator-approved directory, never toward an unverified one."""
    svc, main, lane = worktree_service
    stranger = tmp_path / "stranger"
    stranger.mkdir()
    svc.env_up("webapp", cwd=str(stranger))
    assert fake_runner.start_cwds == [str((main / "frontend").resolve())]


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_two_lanes_get_two_ports_and_two_directories(worktree_service, fake_runner, tmp_path):
    """The full ADR-0007 + ADR-0010 promise, which the bug half-delivered."""
    svc, main, lane = worktree_service
    a = svc.env_up("webapp", cwd=str(lane))
    b = svc.env_up("webapp", cwd=str(main))
    assert a["port"] != b["port"]
    assert fake_runner.start_cwds == [
        str((lane / "frontend").resolve()),
        str((main / "frontend").resolve()),
    ]


# --- readiness: "not on loopback" is not "did not start" --------------------
#
# Reported from a pilot project, 2026-07-30. A server that binds a specific
# address (a tailnet address, a chosen interface) answers there and NEVER on
# loopback, so the old probe timed out and env_up killed a process that had
# started perfectly. The log line proving it was up travelled inside the
# failure payload: that is the tell these tests exist to keep.
#
# The fakes below run the REAL readiness probe and fake only the machine, so the
# branch under test is the shipped one rather than an injected stand-in.


@pytest.fixture
def readiness_service(devctl_home, write_registry, sample_registry_data, fake_runner, clock):
    """Build a Service whose readiness probe is the real one."""
    write_registry(sample_registry_data)

    def build(**kw):
        return Service(
            devctl_home,
            now_fn=clock,
            runner_factory=lambda name: fake_runner,
            # Small but non-zero: the NOT_LISTENING paths must actually poll out.
            readiness_timeout=0.25,
            watchdog_spawn=lambda key: None,
            session_id_fn=lambda: "sess-1",
            **kw,
        )

    return build


def _listens_after_start(fake_runner, pid):
    """Nothing is listening until the runner starts — then our child is.

    Stateful on purpose: the port DRAW asks the same probe, so a fake that
    always reports a listener would make the draw skip the port it is meant to
    hand out, and the test would pass for the wrong reason.
    """

    def owner(port):
        return ProcInfo(pid=pid, name="node", cmdline=()) if fake_runner.started else None

    return owner


def test_up_keeps_a_server_that_bound_a_non_loopback_address(
    readiness_service, fake_runner, monkeypatch
):
    """THE regression: started, listening, not on loopback → keep it."""
    monkeypatch.setattr(service_mod, "_port_answering", lambda port: False)
    # The listener is pid 31337 — a *child* (npm → node), not the spawned shell.
    # Its process group is the shell's pid, which is what proves it is ours.
    monkeypatch.setattr(procutil, "port_owner", _listens_after_start(fake_runner, 31337))
    monkeypatch.setattr(
        procutil, "process_group_of", lambda pid: 1000 if pid in (1000, 31337) else None
    )

    res = readiness_service().env_up("webapp", cwd="/proj/webapp")

    assert res["ok"] is True
    assert res["readiness"] == "listening"
    assert fake_runner.stopped == []  # it used to be killed right here
    assert "will not reach it" in res["readiness_detail"]


def test_up_still_kills_a_server_that_never_listened(
    readiness_service, fake_runner, monkeypatch
):
    """The real F8 is unchanged: probe ran, nothing of ours is there → stop it."""
    monkeypatch.setattr(service_mod, "_port_answering", lambda port: False)
    monkeypatch.setattr(procutil, "port_owner", lambda port: None)  # verified empty

    res = readiness_service().env_up("webapp", cwd="/proj/webapp")

    assert res["ok"] is False
    assert res["error"] == "START_TIMEOUT"
    assert fake_runner.stopped == [1000]
    assert res["log_tail"]  # the diagnosis still travels


def test_up_kills_when_a_foreign_process_holds_the_port(
    readiness_service, fake_runner, monkeypatch
):
    """A listener in another process group is not ours — ours did not come up."""
    monkeypatch.setattr(service_mod, "_port_answering", lambda port: False)
    monkeypatch.setattr(procutil, "port_owner", _listens_after_start(fake_runner, 999))
    monkeypatch.setattr(
        procutil, "process_group_of", lambda pid: 1000 if pid == 1000 else 4242
    )

    res = readiness_service().env_up("webapp", cwd="/proj/webapp")

    assert res["ok"] is False
    assert res["error"] == "START_TIMEOUT"
    assert fake_runner.stopped == [1000]


def test_up_does_not_kill_when_the_probe_could_not_run(
    readiness_service, fake_runner, monkeypatch
):
    """ProbeUnavailable is not evidence of absence — keep it, and say so.

    Killing on a probe that could not answer is killing on ignorance. The lease
    is written, so the process is tracked and swept rather than orphaned.
    """
    monkeypatch.setattr(service_mod, "_port_answering", lambda port: False)

    def unavailable(port):
        raise procutil.ProbeUnavailable("no usable port probe")

    monkeypatch.setattr(procutil, "port_owner", unavailable)

    res = readiness_service().env_up("webapp", cwd="/proj/webapp")

    assert res["ok"] is True
    assert res["readiness"] == "unknown"
    assert fake_runner.stopped == []
    assert "swept rather than orphaned" in res["readiness_detail"]


def test_up_does_not_kill_when_the_listener_cannot_be_attributed(
    readiness_service, fake_runner, monkeypatch
):
    """Something listens but its group is unreadable → unknown, not absent."""
    monkeypatch.setattr(service_mod, "_port_answering", lambda port: False)
    monkeypatch.setattr(procutil, "port_owner", _listens_after_start(fake_runner, 31337))
    monkeypatch.setattr(
        procutil, "process_group_of", lambda pid: 1000 if pid == 1000 else None
    )

    res = readiness_service().env_up("webapp", cwd="/proj/webapp")

    assert res["ok"] is True
    assert res["readiness"] == "unknown"
    assert fake_runner.stopped == []


def test_answering_on_loopback_never_asks_who_owns_the_port(
    readiness_service, fake_runner, monkeypatch
):
    """The common path must not pay for the rare one.

    `port_owner` shells out to `lsof` on macOS. If the fast path consulted it,
    every ordinary start would spawn a subprocess for nothing.
    """
    monkeypatch.setattr(service_mod, "_port_answering", lambda port: True)
    asked: list[int] = []

    def owner(port):
        asked.append(port)
        return None

    monkeypatch.setattr(procutil, "port_owner", owner)

    from rentctl.core.registry import BLOCK_SIZE

    res = readiness_service().env_up("webapp", cwd="/proj/webapp")

    assert res["readiness"] == "answered"
    # The draw scans the whole block once, to build its holders report. What
    # matters here is that readiness adds nothing on top of that.
    assert asked == list(range(5180, 5180 + BLOCK_SIZE))


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


def test_up_does_not_kill_when_our_own_process_group_is_unreadable(
    readiness_service, fake_runner, monkeypatch
):
    """The other side of attribution: we cannot read OUR group, so no comparison.

    Distinct from the case above, where theirs was unreadable. Same rule: no
    comparison is possible, so nothing is proved, so nothing gets killed.
    """
    monkeypatch.setattr(service_mod, "_port_answering", lambda port: False)
    monkeypatch.setattr(procutil, "port_owner", _listens_after_start(fake_runner, 31337))
    monkeypatch.setattr(procutil, "process_group_of", lambda pid: None)

    res = readiness_service().env_up("webapp", cwd="/proj/webapp")

    assert res["ok"] is True
    assert res["readiness"] == "unknown"
    assert fake_runner.stopped == []


def test_up_refuses_when_our_process_died_and_a_stranger_answers(
    readiness_service, fake_runner, monkeypatch
):
    """Readiness says the port is served; this says it is served by US.

    The 2026-08-01 audit rated this its highest finding and I had not verified
    it. It reproduces: a foreign listener answering loopback satisfies the
    probe, so if our command dies on EADDRINUSE env_up returns ok:true over a
    lease naming a pid that is already gone — and every later reconcile reads
    that dead pid as a crashed server of ours.
    """
    monkeypatch.setattr(service_mod, "_port_answering", lambda port: True)  # a stranger
    monkeypatch.setattr(procutil, "port_owner", lambda port: None)

    svc = readiness_service()
    real_start = fake_runner.start

    def start_then_die(entry, port, log_path):
        handle = real_start(entry, port, log_path)
        fake_runner._alive[handle.pid] = False  # EADDRINUSE, exits immediately
        return handle

    monkeypatch.setattr(fake_runner, "start", start_then_die)

    res = svc.env_up("webapp", cwd="/proj/webapp")

    assert res["ok"] is False
    assert res["error"] == START_TIMEOUT
    assert "another process" in res["message"]
