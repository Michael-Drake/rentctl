"""Harness for driving a REAL supervisor process directly (ADR-0016 plan step 4).

Each test writes a ``starting`` lease into isolated state, spawns
``python -m rentctl.supervisor <key> <generation>`` exactly as §6 describes (the
spawn and the ``SPAWNED`` write under L), and then acts on the lease the way a
CLI would: stop requests on disk plus the verified wake.

Every supervisor's session S is registered with the ``workload_sessions`` guard
as a *workload-owned* identity, so the supervisor itself counts as a member: a
supervisor or workload process left behind fails the test.
"""

from __future__ import annotations

import os
import random
import signal
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from rentctl.core import events as ev
from rentctl.core import procutil
from rentctl.core.events import EventLog
from rentctl.core.leases import Lease, ProcessRef, SupervisorRef
from rentctl.core.lifecycle import (
    Actor,
    ActorKind,
    Event,
    EventKind,
    new_starting_lease,
    transition,
)
from rentctl.core.locking import project_lock
from rentctl.core.models import SID_OWNER_SUPERVISOR, SID_OWNER_WORKLOAD, WorkloadIdentity
from rentctl.core.paths import DevctlPaths, lease_key, project_from_key
from rentctl.core.supervision import self_ref, supervisor_argv, wake_supervisor

PY = sys.executable
# `python -m http.server`'s replacement; see loopserve.py for why.
LOOPSERVE = str(Path(__file__).resolve().parent / "loopserve.py")
PROJECT = "suptest"

# A loopback HTTP server with a 60 s lifetime cap (the leak bound). Writes its
# pid atomically to argv[1]. `--ignore-term` makes it SIG_IGN SIGTERM; `--life N`
# shortens the cap (a server that exits on its own).
SERVER = """\
import http.server, os, signal, sys, threading, time
from loopserve import LoopbackHTTPServer
if "--ignore-term" in sys.argv:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
life = 60.0
if "--life" in sys.argv:
    life = float(sys.argv[sys.argv.index("--life") + 1])
srv = LoopbackHTTPServer(("127.0.0.1", int(os.environ["PORT"])),
                         http.server.SimpleHTTPRequestHandler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
tmp = sys.argv[1] + ".tmp"
open(tmp, "w").write(str(os.getpid()))
os.replace(tmp, sys.argv[1])
time.sleep(life)
"""

# Writes its pid and sleeps; `ign` in argv[2] makes it SIG_IGN SIGTERM.
PLAIN = """\
import os, signal, sys, time
if len(sys.argv) > 2 and sys.argv[2] == "ign":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
tmp = sys.argv[1] + ".tmp"
open(tmp, "w").write(str(os.getpid()))
os.replace(tmp, sys.argv[1])
time.sleep(60)
"""

# On SIGTERM: spawn a TERM-ignoring child (pid → argv[2]), then exit. E3's forker.
FORKER = """\
import os, signal, subprocess, sys, time
def on_term(signum, frame):
    child = subprocess.Popen([sys.executable, sys.argv[3], sys.argv[2] + ".self", "ign"])
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

# Double-fork + setsid, then serve: the daemonizer the macOS guarantee excludes (§5).
DAEMON = """\
import http.server, os, sys, threading, time
from loopserve import LoopbackHTTPServer
if os.fork():
    os._exit(0)
os.setsid()
if os.fork():
    os._exit(0)
srv = LoopbackHTTPServer(("127.0.0.1", int(os.environ["PORT"])),
                         http.server.SimpleHTTPRequestHandler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
tmp = sys.argv[1] + ".tmp"
open(tmp, "w").write(str(os.getpid()))
os.replace(tmp, sys.argv[1])
time.sleep(30)
"""


# A process that must never be signalled: it records every TERM and WINCH it
# receives in argv[1] (one line each) and keeps running. Lifetime-capped at 60 s.
DECOY = """\
import signal, sys, time
def note(signum, frame):
    with open(sys.argv[1], "a") as f:
        f.write(f"{signum}\\n")
signal.signal(signal.SIGTERM, note)
signal.signal(signal.SIGWINCH, note)
open(sys.argv[1], "a").close()
time.sleep(60)
"""


def free_block(size: int = 10, avoid: set[int] | None = None) -> int:
    """A base port with ``size`` consecutive free loopback ports, clear of the
    live enrolled blocks (5100–5129) and of any port in ``avoid``."""
    avoid = avoid or set()
    for _ in range(100):
        # NOT from the ephemeral range. `free_port()` binds port 0, and the kernel
        # hands out that same range (macOS 49152–65535, Linux 32768–60999) as the
        # local port of every outgoing connection on the machine — uv downloads,
        # lsof, other test runs. A block found empty there was repeatedly taken a
        # moment later, and the test's own server then failed to bind: an
        # intermittent failure under load that read as a supervisor bug. Below
        # both ranges nothing is handed out implicitly; only an explicit bind can
        # collide, and the probe below catches that.
        base = random.randrange(20000, 32000 - size)
        span = set(range(base, base + size))
        if base + size - 1 > 65535 or span & avoid or span & set(range(5100, 5130)):
            continue
        socks = []
        try:
            for port in span:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.bind(("127.0.0.1", port))
                socks.append(s)
            return base
        except OSError:
            continue
        finally:
            for s in socks:
                s.close()
    raise RuntimeError(f"no free {size}-port block found")  # pragma: no cover


def free_port() -> int:
    """An ephemeral loopback port; never a live enrolled block (5100–5129)."""
    while True:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        if not 5100 <= port <= 5129:
            return port


def read_pid(path: Path, timeout: float = 15.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return int(path.read_text())
        time.sleep(0.02)
    raise AssertionError(f"{path} never appeared")


def wait_until(predicate, timeout: float = 15.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def gone(pid: int, start: float | None) -> bool:
    return start is None or not procutil.start_time_matches(start, procutil.observe_start_time(pid))


def answers(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def now() -> datetime:
    return datetime.now().astimezone()


@dataclass
class Run:
    """One supervised environment under test."""

    h: "Harness"
    key: str
    generation: str
    port: int
    proc: subprocess.Popen
    start_time: float

    @property
    def pid(self) -> int:
        return self.proc.pid

    @property
    def ref(self) -> ProcessRef:
        return ProcessRef(self.proc.pid, self.start_time)

    @property
    def path(self) -> Path:
        return self.h.paths.lease_file(self.key)

    def lease(self) -> Lease | None:
        return Lease.read_if_exists(self.path)

    def state(self) -> str | None:
        lease = self.lease()
        return None if lease is None else lease.state

    def wait_state(self, state: str, timeout: float = 20.0) -> Lease:
        assert wait_until(lambda: self.state() == state, timeout), (
            f"lease never reached {state!r}; it is {self.state()!r}; log:\n{self.log_text()}"
        )
        lease = self.lease()
        assert lease is not None
        return lease

    def wait_gone(self, timeout: float = 20.0) -> None:
        assert wait_until(lambda: not self.path.exists(), timeout), (
            f"lease still {self.state()!r}; log:\n{self.log_text()}"
        )

    def wait_exit(self, timeout: float = 20.0) -> int:
        return self.proc.wait(timeout=timeout)

    def alive(self) -> bool:
        return self.proc.poll() is None

    def members(self) -> list[procutil.ProcRow]:
        ident = WorkloadIdentity(self.pid, self.start_time, SID_OWNER_SUPERVISOR)
        return procutil.session_members(ident.sid, ident.owner_start, ident.exclude)

    def request_stop(self, reason: str = ev.EXPLICIT, *, wake: bool = True) -> Any:
        me = self_ref()
        with project_lock(self.h.paths.lock_file(PROJECT)):
            lease = self.lease()
            assert lease is not None
            result = transition(
                lease,
                Event(EventKind.STOP_REQUEST, now(), reason=reason, reason_source=ev.DECLARED, op="down"),
                Actor(ActorKind.CLI, self.generation, me),
            )
            if isinstance(result, Lease) and result is not lease:
                result.write(self.path)
        if wake:
            woke = wake_supervisor(self.ref, self.key)
            assert woke, "the verified wake refused a live supervisor"
        return result

    def renew(self, expires: datetime) -> None:
        me = self_ref()
        with project_lock(self.h.paths.lock_file(PROJECT)):
            lease = self.lease()
            assert lease is not None
            result = transition(
                lease, Event(EventKind.RENEW, now(), expires=expires),
                Actor(ActorKind.CLI, self.generation, me),
            )
            assert isinstance(result, Lease), result
            result.write(self.path)

    def sigkill(self) -> None:
        """SIGKILL the supervisor this test started — verified by pid + start time."""
        res = procutil.verified_signal(self.pid, self.start_time, self.pid, signal.SIGKILL)
        assert res is procutil.SignalResult.SIGNALLED, res
        self.proc.wait(timeout=10)

    def log_text(self) -> str:
        try:
            return (self.h.tmp / f"{self.key}.log").read_text(errors="replace")[-3000:]
        except OSError:
            return "<no log>"

    def events(self, kind: str | None = None) -> list[dict]:
        rows = EventLog(self.h.paths.events_file).read(project=PROJECT)
        rows = [r for r in rows if r.get("generation") in (None, self.generation)]
        return [r for r in rows if kind is None or r.get("event") == kind]

    def stop_all(self) -> None:
        """Belt and braces for a failing test: ask for a stop and wait it out."""
        if self.alive() and self.path.exists():
            try:
                self.request_stop()
            except AssertionError:
                pass
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            pass


class Harness:
    def __init__(self, paths: DevctlPaths, tmp: Path, sessions: list) -> None:
        self.paths = paths
        self.tmp = tmp
        self.sessions = sessions
        self.runs: list[Run] = []

    def script(self, name: str, src: str) -> Path:
        path = self.tmp / f"{name}.py"
        path.write_text(src)
        return path

    def write_starting(
        self,
        cmd: str,
        *,
        generation: str | None = None,
        port: int | None = None,
        cwd: Path | None = None,
        expires_in: timedelta = timedelta(minutes=30),
        plan_extra: dict | None = None,
    ) -> tuple[str, str, int]:
        cwd = cwd or (self.tmp / "proj")
        cwd.mkdir(parents=True, exist_ok=True)
        key = lease_key(PROJECT, str(cwd))
        assert project_from_key(key) == PROJECT
        generation = generation or uuid.uuid4().hex
        port = port or free_port()
        t = now()
        lease = new_starting_lease(
            generation=generation, project=PROJECT, profile="default", runner="process",
            port=port, session="test", cwd=str(cwd), spawn_cwd=str(cwd),
            log=str(self.tmp / f"{key}.log"),
            plan={"cmd": cmd, "cwd": str(cwd), "port_env": "PORT",
                  "term_grace_s": 2.0, "kill_grace_s": 2.0, "readiness_timeout_s": 15.0,
                  **(plan_extra or {})},
            now=t, expires=t + expires_in,
        )
        with project_lock(self.paths.lock_file(PROJECT)):
            lease.write(self.paths.lease_file(key))
        return key, generation, port

    def spawn(self, key: str, generation: str, *, extra_env: dict | None = None,
              record_spawned: bool = True) -> tuple[subprocess.Popen, float]:
        """§6 up-step 2: spawn and record the supervisor, both under L."""
        env = {
            **os.environ,
            "RENTCTL_STATE_HOME": str(self.paths.state_home),
            "RENTCTL_CONFIG_HOME": str(self.paths.config_home),
            "RENTCTL_TESTING": "1",
            **(extra_env or {}),
        }
        with project_lock(self.paths.lock_file(PROJECT)):
            proc = subprocess.Popen(
                supervisor_argv(key, generation), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, env=env,
            )
            start = procutil.observe_start_time(proc.pid)
            assert start is not None
            # The guard owns cleanup: a workload-owned identity counts the
            # supervisor itself as a member, so a leftover supervisor fails too.
            self.sessions.append(WorkloadIdentity(proc.pid, start, SID_OWNER_WORKLOAD))
            path = self.paths.lease_file(key)
            lease = Lease.read_if_exists(path)
            if record_spawned and lease is not None and lease.generation == generation:
                spawned = transition(
                    lease, Event(EventKind.SPAWNED, now(), supervisor=SupervisorRef(proc.pid, start)),
                    Actor(ActorKind.CLI, generation, self_ref()),
                )
                if isinstance(spawned, Lease):
                    spawned.write(path)
        return proc, start

    def start(self, cmd: str, **kw) -> Run:
        extra_env = kw.pop("extra_env", None)
        key, generation, port = self.write_starting(cmd, **kw)
        proc, start = self.spawn(key, generation, extra_env=extra_env)
        run = Run(self, key, generation, port, proc, start)
        self.runs.append(run)
        return run

    def cleanup(self) -> None:
        for run in self.runs:
            run.stop_all()
