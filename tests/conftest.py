"""Shared test fixtures.

Points the whole system at a tmp config/state home so no test ever touches
the real ``~/.config/rentctl`` or ``~/.local/state/rentctl``.
"""

from __future__ import annotations

import json
import os
import signal
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# The integration tests spawn the watchdog as `python -m rentctl.watchdog`. pytest's
# `pythonpath = ["src"]` only touches *this* process's sys.path, so hand src/ to every
# child through the environment too — otherwise a bare checkout (the merge gate's
# throwaway worktree, ADR-0058 D4) imports devctl here but not in the subprocess.
_SRC = str(Path(__file__).resolve().parent.parent / "src")
# tests/ too, so a spawned script can `from loopserve import LoopbackHTTPServer`
# instead of restating the one test server (see tests/loopserve.py for why).
_TESTS = str(Path(__file__).resolve().parent)
os.environ["PYTHONPATH"] = os.pathsep.join(
    [_SRC, _TESTS, *(p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p)]
)

from rentctl.core import procutil, supervision  # noqa: E402
from rentctl.core.models import SID_OWNER_WORKLOAD, ProcessHandle, WorkloadIdentity  # noqa: E402
from rentctl.core.paths import DevctlPaths  # noqa: E402
from rentctl.core.runners import process as process_runner_mod  # noqa: E402

CDT = timezone(timedelta(hours=-5))

# How long a session may take to finish emptying after the test body returns.
# A stop that returned `verified` leaves nothing; this only covers a test whose
# own cleanup SIGKILLed a group a moment ago and the kernel is still reaping.
_LEFTOVER_SETTLE_S = 2.0


@pytest.fixture(autouse=True)
def workload_sessions():
    """Record every workload session a test creates; fail the test if any is left.

    ADR-0016's test-plan preamble: a survivor at teardown is a bug and must not
    be tidied up quietly. Every ``ProcessRunner.start`` and every in-process
    ``supervision.spawn_supervisor`` is recorded here, and a test that spawns a
    session some other way (a CLI subprocess, say) appends its
    :class:`WorkloadIdentity` to the returned list. At teardown each session is
    scanned; verified members still alive are SIGKILLed one pid at a time — the
    same verified signal the product uses, so this guard cannot hit a process
    the test did not start — and then the test FAILS naming them.

    An ``AMBIGUOUS`` session is never signalled (its owner cannot be proved),
    and it does not fail the test: that verdict is the product refusing
    correctly, which several tests set up on purpose.
    """
    created: list[WorkloadIdentity] = []
    real_start = process_runner_mod.ProcessRunner.start
    real_spawn = supervision.spawn_supervisor

    def tracking_start(self, entry, port, log_path):
        handle = real_start(self, entry, port, log_path)
        created.append(handle.identity())
        return handle

    def tracking_spawn(*args, **kwargs):
        # A supervisor IS its session (ADR-0016 §1). Recorded as a workload-owned
        # identity, so the supervisor itself counts as a member: a supervisor or
        # a workload process left running fails the test.
        ref = real_spawn(*args, **kwargs)
        created.append(WorkloadIdentity(ref.pid, ref.start_time, SID_OWNER_WORKLOAD))
        return ref

    process_runner_mod.ProcessRunner.start = tracking_start
    supervision.spawn_supervisor = tracking_spawn
    try:
        yield created
    finally:
        process_runner_mod.ProcessRunner.start = real_start
        supervision.spawn_supervisor = real_spawn
    leftovers = []
    for ident in created:
        deadline = time.monotonic() + _LEFTOVER_SETTLE_S
        scan = procutil.session_scan(ident.sid, ident.owner_start, ident.exclude)
        while scan.state is procutil.Membership.MEMBERS and time.monotonic() < deadline:
            time.sleep(0.05)
            scan = procutil.session_scan(ident.sid, ident.owner_start, ident.exclude)
        if scan.state is not procutil.Membership.MEMBERS:
            continue
        for m in scan.members:
            procutil.verified_signal(m.pid, m.start_time, ident.sid, signal.SIGKILL)
            leftovers.append(f"pid {m.pid} ({m.name}) in session {ident.sid}")
    if leftovers:
        pytest.fail("workload processes outlived the test (SIGKILLed now): " + "; ".join(leftovers))


class FakeRunner:
    """Deterministic stand-in for a runner. ``start_time == pid``, so a handle
    with a mismatched start time reads as recycled/dead — the PID-recycle guard
    without spawning real processes."""

    name = "process"

    def __init__(self) -> None:
        self.started: list[int] = []
        self.stopped: list[int] = []
        # The directory each start was handed. A fake that drops this cannot
        # catch ADR-0010's bug, whose entire symptom is a correct-looking lease
        # over a process spawned in the wrong place.
        self.start_cwds: list[str] = []
        self._alive: dict[int, bool] = {}
        self._next_pid = 1000

    def start(self, entry, port, log_path):
        pid = self._next_pid
        self._next_pid += 1
        self._alive[pid] = True
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(f"started {entry.cmd} on {port}\n")
        self.started.append(pid)
        self.start_cwds.append(entry.cwd)
        return ProcessHandle(pid=pid, pid_start_time=float(pid))

    def stop(self, handle):
        self.stopped.append(handle.pid)
        self._alive[handle.pid] = False

    def alive(self, handle):
        return self._alive.get(handle.pid, False) and handle.pid_start_time == float(handle.pid)

    def kill_pid(self, pid: int) -> None:
        self._alive[pid] = False

    def orphans(self):
        return []


class Clock:
    """A controllable clock for the injectable ``now_fn``."""

    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now = self.now + timedelta(**kw)


def install_plugin(claude_home: Path, name: str = "devctl", scope: str = "user",
                   project_path: Path | None = None) -> None:
    """Write Claude Code's install register as a real `claude plugin install`
    leaves it (WI-0028).

    Shared rather than repeated because the previous shape — a marker directory
    at `plugins/<name>` — was wrong, and each test carrying its own copy of it is
    what let `plugin_installed()` return a constant False for a month while three
    tests agreed it worked. One definition, so the next schema change breaks
    loudly in one place.
    """
    entry: dict = {"scope": scope}
    if project_path is not None:
        entry["projectPath"] = str(project_path)
    d = claude_home / "plugins"
    d.mkdir(parents=True, exist_ok=True)
    (d / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": {f"{name}@{name}": [entry]}})
    )


@pytest.fixture
def devctl_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DevctlPaths:
    """Redirect config + state roots to a tmp dir; return the resolved paths."""
    config = tmp_path / "config"
    state = tmp_path / "state"
    monkeypatch.setenv("RENTCTL_CONFIG_HOME", str(config))
    monkeypatch.setenv("RENTCTL_STATE_HOME", str(state))
    monkeypatch.delenv("DEVCTL_CONFIG_HOME", raising=False)
    monkeypatch.delenv("DEVCTL_STATE_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    paths = DevctlPaths.default()
    paths.ensure_dirs()
    return paths


@pytest.fixture
def write_registry(devctl_home: DevctlPaths):
    """Factory: write a registry.json under the tmp config home and return path."""

    def _write(data: dict) -> Path:
        reg = devctl_home.registry_file
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text(json.dumps(data))
        return reg

    return _write


@pytest.fixture
def sample_registry_data() -> dict:
    """A minimal valid registry with one process-runner project."""
    return {
        "port_blocks": {"comment": "each project owns a 10-port block"},
        "projects": {
            "webapp": {
                "block": 5180,
                "runner": "process",
                "profiles": {
                    "default": {"cmd": "npm run dev", "cwd": "/tmp/webapp", "port_env": "PORT"},
                    "api-only": {
                        "cmd": "npm run api",
                        "cwd": "/tmp/webapp",
                        "port_env": "PORT",
                        "offset": 1,
                    },
                },
            }
        },
    }
