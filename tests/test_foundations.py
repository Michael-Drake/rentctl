"""Unit tests for the core foundations: models, paths, procutil pure logic."""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from rentctl.core import procutil
from rentctl.core.errors import PORT_SQUATTED, DevctlError
from rentctl.core.models import ProcessHandle, ProcInfo
from rentctl.core.paths import DevctlPaths


# --- models round-trip ----------------------------------------------------

def test_process_handle_round_trip():
    h = ProcessHandle(pid=4242, pid_start_time=1784080000.12)
    assert ProcessHandle.from_dict(h.to_dict()) == h


def test_process_handle_from_dict_coerces_types():
    h = ProcessHandle.from_dict({"pid": "4242", "pid_start_time": "1784080000.12"})
    assert h.pid == 4242
    assert h.pid_start_time == pytest.approx(1784080000.12)


def test_procinfo_to_dict():
    p = ProcInfo(pid=5, name="node", cmdline=("node", "server.js"))
    assert p.to_dict() == {"pid": 5, "name": "node", "cmdline": ["node", "server.js"]}


# --- errors ---------------------------------------------------------------

def test_devctl_error_envelope_merges_details():
    err = DevctlError(PORT_SQUATTED, "port held", port=5180, owner={"pid": 9})
    env = err.to_envelope()
    assert env == {
        "ok": False,
        "error": "PORT_SQUATTED",
        "message": "port held",
        "port": 5180,
        "owner": {"pid": 9},
    }


# --- paths ----------------------------------------------------------------

def test_paths_rentctl_home_overrides_win(monkeypatch, tmp_path):
    monkeypatch.setenv("RENTCTL_CONFIG_HOME", str(tmp_path / "c"))
    monkeypatch.setenv("RENTCTL_STATE_HOME", str(tmp_path / "s"))
    monkeypatch.setenv("DEVCTL_CONFIG_HOME", "/should/not/win")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/should/not/win")
    p = DevctlPaths.default()
    assert p.registry_file == tmp_path / "c" / "devctl" / "registry.json"
    assert p.lease_file("webapp") == tmp_path / "s" / "devctl" / "leases" / "webapp.json"
    assert p.lock_file("webapp") == tmp_path / "s" / "devctl" / "leases" / "webapp.lock"
    assert p.log_file("webapp", "2026-07-14T0730") == (
        tmp_path / "s" / "devctl" / "logs" / "webapp-2026-07-14T0730.log"
    )


def test_paths_legacy_devctl_home_overrides_still_honored(monkeypatch, tmp_path):
    """`DEVCTL_*` is baked into shells and scripts that predate ADR-0009."""
    monkeypatch.delenv("RENTCTL_CONFIG_HOME", raising=False)
    monkeypatch.delenv("RENTCTL_STATE_HOME", raising=False)
    monkeypatch.setenv("DEVCTL_CONFIG_HOME", str(tmp_path / "c"))
    monkeypatch.setenv("DEVCTL_STATE_HOME", str(tmp_path / "s"))
    monkeypatch.setenv("XDG_CONFIG_HOME", "/should/not/win")
    p = DevctlPaths.default()
    assert p.config_dir == tmp_path / "c" / "devctl"
    assert p.state_dir == tmp_path / "s" / "devctl"


def test_paths_xdg_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("RENTCTL_CONFIG_HOME", raising=False)
    monkeypatch.delenv("RENTCTL_STATE_HOME", raising=False)
    monkeypatch.delenv("DEVCTL_CONFIG_HOME", raising=False)
    monkeypatch.delenv("DEVCTL_STATE_HOME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xc"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xs"))
    p = DevctlPaths.default()
    assert p.config_dir == tmp_path / "xc" / "devctl"
    assert p.state_dir == tmp_path / "xs" / "devctl"


def test_the_working_directory_is_not_renamed_with_the_product(monkeypatch, tmp_path):
    """The product is `rentctl`; the working folder stays `devctl` — a deliberate
    decision, not a leftover. Asserted rather than left implicit because the
    mismatch reads as an oversight to anyone who does not know it was a choice —
    and "fixing" it silently migrates every live lease and the whole event log."""
    monkeypatch.setenv("RENTCTL_CONFIG_HOME", str(tmp_path / "c"))
    monkeypatch.setenv("RENTCTL_STATE_HOME", str(tmp_path / "s"))
    p = DevctlPaths.default()
    assert p.config_dir.name == "devctl"
    assert p.state_dir.name == "devctl"


def test_ensure_dirs_creates_state(devctl_home):
    assert devctl_home.leases_dir.is_dir()
    assert devctl_home.logs_dir.is_dir()


# --- procutil: the pure PID-recycle comparison ----------------------------

def test_start_time_matches_exact():
    assert procutil.start_time_matches(1784080000.12, 1784080000.12) is True


def test_start_time_matches_none_observed_is_false():
    # PID gone → never a match. The core of "never kill what recycled away."
    assert procutil.start_time_matches(1784080000.12, None) is False


def test_start_time_matches_recycled_pid_is_false():
    # Same PID, wildly different start time = recycled → not ours.
    assert procutil.start_time_matches(1784080000.0, 1784090000.0) is False


def test_start_time_matches_within_tolerance():
    assert procutil.start_time_matches(1784080000.0, 1784080000.4, tol=1.0) is True


def test_start_time_matches_outside_tolerance():
    assert procutil.start_time_matches(1784080000.0, 1784080002.0, tol=1.0) is False


# --- procutil: observation against a real short-lived process -------------

def test_observe_and_is_alive_real_process():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        st = procutil.observe_start_time(proc.pid)
        assert st is not None
        handle = ProcessHandle(pid=proc.pid, pid_start_time=st)
        assert procutil.is_alive(handle) is True
        # A handle with the right PID but a wrong start time must read as dead.
        recycled = ProcessHandle(pid=proc.pid, pid_start_time=st + 10_000)
        assert procutil.is_alive(recycled) is False
    finally:
        proc.terminate()
        proc.wait(timeout=5)
    assert procutil.observe_start_time(proc.pid) is None


def test_snapshot_start_times_missing_pid_is_none():
    # PID 1 (launchd/init) is always alive on macOS; a huge PID never is.
    snap = procutil.snapshot_start_times([1, 2_000_000_000])
    assert snap[1] is not None
    assert snap[2_000_000_000] is None
