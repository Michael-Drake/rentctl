"""Tests for the process runner: spawn, verify-then-kill, PID-recycle refusal.

These spawn real (short-lived) processes but run in well under a second each,
so they live with the unit suite. The heavier process-group and concurrency
scenarios are in the integration suite (spec §10).
"""

from __future__ import annotations

import time

import pytest

from rentctl.core import procutil
from rentctl.core.errors import REGISTRY_INVALID, DevctlError
from rentctl.core.models import ProcessHandle
from rentctl.core.registry import RegistryProfile
from rentctl.core.runners import ProcessRunner, get_runner


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
    r.stop(h)
    assert wait_dead(h)
    assert r.alive(h) is False
    r.stop(h)  # second stop on a dead handle is a no-op, not an error


def test_stop_refuses_recycled_pid(tmp_path):
    """A handle with the right PID but wrong start time must never be killed."""
    r = runner()
    h = r.start(profile("sleep 30", str(tmp_path)), 5180, tmp_path / "srv.log")
    try:
        recycled = ProcessHandle(pid=h.pid, pid_start_time=h.pid_start_time + 10_000)
        assert r.alive(recycled) is False
        r.stop(recycled)  # must NOT kill the real process
        assert r.alive(h) is True  # still alive — we refused to touch it
    finally:
        r.stop(h)
    assert wait_dead(h)


def test_stop_on_never_started_handle_is_noop():
    r = runner()
    r.stop(ProcessHandle(pid=2_000_000_000, pid_start_time=1.0))  # no such pid


def test_orphans_empty(tmp_path):
    assert runner().orphans() == []


def test_pgid_of_dead_pid_is_none():
    # getpgid on a nonexistent pid raises → process_group_of swallows it to None.
    # Lives in procutil now rather than on the runner: the readiness probe needs
    # the same answer to tell our listener from a squatter, and two copies of an
    # OS-boundary call is exactly the duplication that drifts.
    from rentctl.core import procutil as pu

    assert pu.process_group_of(2_000_000_000) is None


def test_wait_gone_times_out_while_alive(tmp_path):
    r = runner()
    h = r.start(profile("sleep 30", str(tmp_path)), 5180, tmp_path / "srv.log")
    try:
        # Zero-budget wait against a live process returns False (still there).
        assert r._wait_gone(h, timeout=0.0) is False
    finally:
        r.stop(h)


def test_stop_escalates_to_sigkill_deterministic(monkeypatch):
    """The escalation branch: TERM leaves it alive → SIGKILL. Signal-independent."""
    import signal as _signal

    from rentctl.core import procutil as pu

    r = ProcessRunner()
    monkeypatch.setattr(pu, "is_alive", lambda h, tol=1.0: True)  # stays alive through TERM
    monkeypatch.setattr(pu, "process_group_of", lambda pid: 4242)
    sent: list[int] = []
    monkeypatch.setattr(r, "_signal_group", lambda pgid, sig: sent.append(sig))
    waits = iter([False, True])  # TERM grace: still alive; after KILL: gone
    monkeypatch.setattr(r, "_wait_gone", lambda h, t: next(waits))

    r.stop(ProcessHandle(pid=1, pid_start_time=1.0))
    assert sent == [_signal.SIGTERM, _signal.SIGKILL]


def test_stop_returns_when_pgid_gone(monkeypatch):
    """Race: process vanished between the alive check and getpgid → no signals."""
    from rentctl.core import procutil as pu

    r = ProcessRunner()
    monkeypatch.setattr(pu, "is_alive", lambda h, tol=1.0: True)
    monkeypatch.setattr(pu, "process_group_of", lambda pid: None)  # gone before we could signal
    sent: list[int] = []
    monkeypatch.setattr(r, "_signal_group", lambda pgid, sig: sent.append(sig))

    r.stop(ProcessHandle(pid=1, pid_start_time=1.0))
    assert sent == []


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
