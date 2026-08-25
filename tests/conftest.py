"""Shared test fixtures.

Points the whole system at a tmp config/state home so no test ever touches
the real ``~/.config/rentctl`` or ``~/.local/state/rentctl``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# The integration tests spawn the watchdog as `python -m rentctl.watchdog`. pytest's
# `pythonpath = ["src"]` only touches *this* process's sys.path, so hand src/ to every
# child through the environment too — otherwise a bare checkout (the merge gate's
# throwaway worktree, ADR-0058 D4) imports devctl here but not in the subprocess.
_SRC = str(Path(__file__).resolve().parent.parent / "src")
os.environ["PYTHONPATH"] = os.pathsep.join(
    [_SRC, *(p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p)]
)

from rentctl.core.models import ProcessHandle  # noqa: E402
from rentctl.core.paths import DevctlPaths  # noqa: E402

CDT = timezone(timedelta(hours=-5))


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
