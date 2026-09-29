"""ADR-0016 step 2 acceptance, at runner level, with REAL processes.

The process runner's ``stop`` is now the one stop algorithm in recovery mode and
``alive`` means "the workload session has members". These are the handoff's
acceptance scenarios 1–5 driven through ``ProcessRunner`` directly, plus the S1
regression from Astra's reproduction and the reconcile/watchdog readings that
used to call a live orphan dead.

Every helper process has a 60 s lifetime cap, binds nothing unless the test
says so (and then only an ephemeral loopback port), and is accounted for by the
``workload_sessions`` guard in conftest, which fails the test on any leftover.
"""

from __future__ import annotations

import os
import socket
import sys
import time
from datetime import timedelta
from pathlib import Path

import psutil
import pytest

from rentctl import watchdog
from rentctl.core import procutil
from rentctl.core.leases import Lease
from rentctl.core.models import ProcessHandle
from rentctl.core.paths import lease_key
from rentctl.core.reconcile import Action, decide
from rentctl.core.registry import RegistryProfile
from rentctl.core.runners import ProcessRunner
from rentctl.core.service import _now_local

pytestmark = pytest.mark.integration

PY = sys.executable

# Writes its pid atomically (argv[1]) and sleeps; the lifetime cap is the leak bound.
_PLAIN = """\
import os, sys, time
tmp = sys.argv[1] + ".tmp"
open(tmp, "w").write(str(os.getpid()))
os.replace(tmp, sys.argv[1])
time.sleep(60)
"""

# The same, but SIG_IGN for SIGTERM: the child in Astra's reproduction (S1).
_IGN = _PLAIN.replace("import os, sys, time\n", "import os, signal, sys, time\n"
                      "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n")

# On SIGTERM: start a TERM-ignoring child (argv[3] is the _IGN script, argv[2]
# its pidfile), record that child's pid itself, then exit. E3's `forker`.
_FORKER = """\
import os, signal, subprocess, sys, time
def on_term(signum, frame):
    child = subprocess.Popen([sys.executable, sys.argv[3], sys.argv[2] + ".self"])
    tmp = sys.argv[2] + ".tmp"
    open(tmp, "w").write(str(child.pid))
    os.replace(tmp, sys.argv[2])
    os._exit(0)
signal.signal(signal.SIGTERM, on_term)
tmp = sys.argv[1] + ".tmp"
open(tmp, "w").write(str(os.getpid()))
os.replace(tmp, sys.argv[1])
time.sleep(60)
"""

# Leaves its process group (setpgid) but not its session; ignores TERM. E3's `pgesc`.
_PGESC = _IGN.replace("import os, signal, sys, time\n", "import os, signal, sys, time\nos.setpgid(0, 0)\n")


def role(tmp: Path, name: str, src: str) -> Path:
    path = tmp / f"{name}.py"
    path.write_text(src)
    return path


def read_pid(path: Path, timeout: float = 10.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return int(path.read_text())
        time.sleep(0.02)
    raise AssertionError(f"{path} never appeared")


def wait_until(predicate, timeout: float = 10.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def gone(pid: int, start: float | None) -> bool:
    """True once ``pid`` no longer names the process that had ``start``."""
    return start is None or not procutil.start_time_matches(start, procutil.observe_start_time(pid))


def start(r: ProcessRunner, tmp: Path, cmd: str, port: int = 0) -> ProcessHandle:
    prof = RegistryProfile(cmd=cmd, cwd=str(tmp), port_env="PORT", preferred_offset=0)
    return r.start(prof, port, tmp / "server.log")


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    # Ephemeral range only: never a live enrolled project's block.
    assert not 5100 <= port <= 5129
    return port


# --- #2: the Astra reproduction, as a regression test (S1) -------------------

@pytest.mark.parametrize("shape", ["current", "legacy"])
def test_runner_stop_kills_term_ignoring_child_of_dead_leader(tmp_path, shape):
    """The shell dies on SIGTERM; its Python child ignores SIGTERM. 1.0.x's stop
    waited on the leader only, returned when the shell died, never escalated —
    and the lease was deleted over a live child. Now the stop is judged on the
    session, so the child is found after the shell's death and SIGKILLed.

    ``legacy`` drives the same stop through a 1.0.x-shaped handle ({pid,
    pid_start_time} only): the fix reaches leases written before it existed."""
    ign = role(tmp_path, "ign", _IGN)
    r = ProcessRunner(term_grace_s=1.0, kill_grace_s=2.0)
    h = start(r, tmp_path, f"'{PY}' '{ign}' '{tmp_path}/child.pid' & wait")
    child = read_pid(tmp_path / "child.pid")
    child_start = procutil.observe_start_time(child)
    assert os.getsid(child) == h.pid  # born into the leader's session
    if shape == "legacy":
        h = ProcessHandle(pid=h.pid, pid_start_time=h.pid_start_time)

    outcome = r.stop(h)

    assert outcome.verified, outcome
    assert outcome.escalated is True  # the child needed SIGKILL
    assert outcome.survivors == ()
    assert not procutil.is_alive(h)                  # parent dead
    assert gone(child, child_start)                  # AND child dead
    assert r.alive(h) is False


# --- #3: the launch parent exits before the stop -----------------------------

def test_runner_leader_exited_before_stop_still_stops_the_orphan(tmp_path):
    """npm exits and vite keeps the port. The orphan reparents away from the
    shell but keeps sid S, so the stop still finds it (E3/E4)."""
    plain = role(tmp_path, "plain", _PLAIN)
    r = ProcessRunner(term_grace_s=3.0, kill_grace_s=2.0)
    h = start(r, tmp_path, f"'{PY}' '{plain}' '{tmp_path}/child.pid' & sleep 0.3; exit 0")
    child = read_pid(tmp_path / "child.pid")
    child_start = procutil.observe_start_time(child)
    assert wait_until(lambda: not procutil.is_alive(h))  # the launch shell is gone
    assert psutil.Process(child).ppid() != h.pid          # orphaned, reparented
    assert r.alive(h) is True                             # ...and the workload lives

    outcome = r.stop(h)

    assert outcome.verified and not outcome.escalated  # it honoured TERM
    assert gone(child, child_start)


def test_leader_dead_member_alive_is_neither_clean_nor_dead(tmp_path, devctl_home):
    """The two 1.0.x readings of a live orphan: `reconcile` called it CLEAN and
    dropped the lease; the watchdog called it DEAD and exited. Both now read the
    session, which still has a member."""
    plain = role(tmp_path, "plain", _PLAIN)
    r = ProcessRunner(term_grace_s=3.0, kill_grace_s=2.0)
    h = start(r, tmp_path, f"'{PY}' '{plain}' '{tmp_path}/child.pid' & sleep 0.3; exit 0")
    read_pid(tmp_path / "child.pid")
    assert wait_until(lambda: not procutil.is_alive(h))

    now = _now_local()
    cwd = str(tmp_path)
    lease = Lease(
        project="demo",
        profile="default",
        runner="process",
        handle=h.to_dict(),
        port=free_port(),
        session="s",
        cwd=cwd,
        created=now,
        expires=now + timedelta(minutes=30),
        log=str(tmp_path / "server.log"),
    )
    lease.write(devctl_home.lease_file_for("demo", cwd))
    try:
        verdict = decide(lease, lambda l: r.alive(l.process_handle()), now)
        assert verdict.action is Action.KEEP
        tick = watchdog.watch_once(lease_key("demo", cwd), devctl_home, now, lambda name: r)
        assert tick == watchdog.CONTINUE
        assert devctl_home.lease_file_for("demo", cwd).exists()
    finally:
        assert r.stop(h).verified


# --- #4: a child spawns a child during shutdown ------------------------------

def test_runner_child_spawned_during_shutdown_is_stopped(tmp_path):
    ign = role(tmp_path, "ign", _IGN)
    forker = role(tmp_path, "forker", _FORKER)
    r = ProcessRunner(term_grace_s=1.0, kill_grace_s=2.0)
    h = start(
        r,
        tmp_path,
        f"'{PY}' '{forker}' '{tmp_path}/forker.pid' '{tmp_path}/forked.pid' '{ign}' & wait",
    )
    forker_pid = read_pid(tmp_path / "forker.pid")
    forker_start = procutil.observe_start_time(forker_pid)

    outcome = r.stop(h)

    assert outcome.verified, outcome
    # Born after the first TERM, into session S, and ignoring TERM: only a
    # re-scan that treats it as a new member — then escalation — ends it.
    forked = read_pid(tmp_path / "forked.pid", timeout=1.0)
    assert outcome.escalated is True
    assert gone(forker_pid, forker_start)
    assert procutil.observe_start_time(forked) is None
    assert r.alive(h) is False


# --- #5: the grace period expires and escalation is required ------------------

def test_runner_grace_expires_escalates_to_sigkill(tmp_path):
    ign = role(tmp_path, "ign", _IGN)
    r = ProcessRunner(term_grace_s=0.5, kill_grace_s=2.0)
    # exec: the leader itself is the TERM-ignoring process.
    h = start(r, tmp_path, f"exec '{PY}' '{ign}' '{tmp_path}/me.pid'")
    read_pid(tmp_path / "me.pid")

    t0 = time.monotonic()
    outcome = r.stop(h)
    elapsed = time.monotonic() - t0

    assert outcome.verified and outcome.escalated is True
    assert 0.5 <= elapsed < 3.0  # waited the (shortened) grace, then one kill round
    assert not procutil.is_alive(h)


# --- #1: a normal server and its ordinary children ---------------------------

def test_runner_normal_server_and_children_stop(tmp_path):
    plain = role(tmp_path, "plain", _PLAIN)
    port = free_port()
    r = ProcessRunner(term_grace_s=5.0, kill_grace_s=2.0)
    h = start(
        r,
        tmp_path,
        f"'{PY}' -m http.server \"$PORT\" --bind 127.0.0.1 & "
        f"'{PY}' '{plain}' '{tmp_path}/child.pid' & wait",
        port=port,
    )
    child = read_pid(tmp_path / "child.pid")
    child_start = procutil.observe_start_time(child)

    def answering() -> bool:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return True
        except OSError:
            return False

    assert wait_until(answering, timeout=30)
    members = procutil.session_members(h.pid, h.pid_start_time)
    assert len(members) >= 3  # shell, server, child

    t0 = time.monotonic()
    outcome = r.stop(h)

    assert outcome.verified and outcome.escalated is False
    assert time.monotonic() - t0 < 5.0  # did not wait out the grace
    assert gone(child, child_start)
    assert not answering()


# --- setpgid escapers stay in the session (E3) --------------------------------

def test_runner_stop_catches_a_setpgid_escaper(tmp_path):
    """A process that leaves the group but not the session is still ours: it is
    found by SID. A remembered-pgid killpg would have missed it entirely."""
    pgesc = role(tmp_path, "pgesc", _PGESC)
    r = ProcessRunner(term_grace_s=0.5, kill_grace_s=2.0)
    h = start(r, tmp_path, f"'{PY}' '{pgesc}' '{tmp_path}/esc.pid' & wait")
    esc = read_pid(tmp_path / "esc.pid")
    esc_start = procutil.observe_start_time(esc)
    assert wait_until(lambda: os.getpgid(esc) != h.pid)
    assert os.getsid(esc) == h.pid

    outcome = r.stop(h)

    assert outcome.verified and outcome.escalated
    assert gone(esc, esc_start)
