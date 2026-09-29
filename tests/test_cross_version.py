"""The real cross-version test (ADR-0016 §14, test plan "Migration"; WI-0085).

``test_lease_schema2.py`` checks the poison against a *transcription* of 1.0.1's
``Lease.from_dict``. This module checks it against the thing itself: it installs
the published ``rentctl==1.0.1`` from PyPI into a throwaway venv and runs that
package's own code — the watchdog's ``watch_once``, its ``main`` loop, and the
``ls``/``sweep`` CLI — against schema-2 leases written by the *current* code.

The property under test is §14's "old code never races new code": every 1.0.1
actor must fail closed on a schema-2 file. Concretely, for each lease state the
new code writes, 1.0.1 must

* return ``exit-lease-corrupt`` from the watchdog (touching nothing),
* leave the lease file byte-identical, and
* signal nothing: the leases below are all **expired** and name a live
  workload session, so a 1.0.1 reader that managed to parse one would stop it.

Isolation: every child runs with ``-I`` and no ``PYTHONPATH`` (``conftest.py``
puts ``src/`` on it for every subprocess, which would silently import the
current code instead of 1.0.1), and with ``RENTCTL_*_HOME`` pointed at tmp dirs.
Each child also reports ``rentctl.__file__`` and its installed version, and the
test asserts both, so a mis-isolated run cannot pass by accident.

The venv is built with ``uv`` using the default uv cache — reusing it is what
keeps a local re-run to a second or two; the cache is content-addressed, so a
1.0.1 wheel in it cannot be confused with the checkout. The tests skip (with a
reason) when ``uv`` is missing or PyPI does not answer a probe, and carry the
``network`` marker so ``-m 'not network'`` deselects them.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psutil
import pytest

from rentctl.core.leases import (
    SCHEMA_SUPERVISED,
    CleanupRecord,
    Lease,
    ProcessRef,
    StopRequest,
    SupervisorRef,
    Survivor,
)
from rentctl.core.models import SID_OWNER_SUPERVISOR, SID_OWNER_WORKLOAD, WorkloadIdentity
from rentctl.core.paths import DevctlPaths, lease_key
from rentctl.core.procutil import SUPERVISION_SESSION

pytestmark = [pytest.mark.network, pytest.mark.integration]

OLD_VERSION = "1.0.1"
PYPI_PROBE_URL = "https://pypi.org/simple/rentctl/"
CORRUPT = "exit-lease-corrupt"  # 1.0.1 rentctl/watchdog.py: CORRUPT
STATES = ["starting", "running", "stopping", "cleanup_incomplete", "unsupervised"]
CHILD_TIMEOUT_S = 60


def _pypi_unreachable_reason() -> str | None:
    try:
        with urllib.request.urlopen(PYPI_PROBE_URL, timeout=10) as resp:
            if resp.status != 200:
                return f"PyPI probe {PYPI_PROBE_URL} answered HTTP {resp.status}"
    except OSError as e:  # URLError, timeouts and DNS failures are all OSError
        return f"PyPI unreachable ({PYPI_PROBE_URL}): {e}"
    return None


def _child_env(state: Path, config: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("RENTCTL_", "DEVCTL_", "XDG_"))}
    env.pop("PYTHONPATH", None)
    env.pop("VIRTUAL_ENV", None)
    env["RENTCTL_STATE_HOME"] = str(state)
    env["RENTCTL_CONFIG_HOME"] = str(config)
    return env


@pytest.fixture(scope="module")
def old_python(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A venv with ``rentctl==1.0.1`` from PyPI; returns its interpreter."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH; the cross-version test needs it to build a 1.0.1 venv")
    reason = _pypi_unreachable_reason()
    if reason is not None:
        pytest.skip(reason)
    venv = tmp_path_factory.mktemp("rentctl-1.0.1") / "venv"
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV")}
    subprocess.run(
        [uv, "venv", "--quiet", "--python", sys.executable, str(venv)],
        check=True, env=env, timeout=120,
    )
    py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    # The probe answered, so an install failure here is a real failure, not a skip.
    subprocess.run(
        [uv, "pip", "install", "--quiet", "--python", str(py), f"rentctl=={OLD_VERSION}"],
        check=True, env=env, timeout=300,
    )
    return py


@pytest.fixture
def isolated(tmp_path: Path) -> tuple[DevctlPaths, dict[str, str]]:
    state, config = tmp_path / "state", tmp_path / "config"
    paths = DevctlPaths(config_home=config, state_home=state)
    paths.ensure_dirs()
    return paths, _child_env(state, config)


@pytest.fixture
def workload(workload_sessions):
    """A real disposable session: a leader (the supervisor's stand-in) and one child.

    Yields ``(leader, leader_start, child_pid, child_start)``. The session is
    registered with the autouse leftover guard as well, and killed here in
    ``finally`` — the leader is our unreaped child, so its pid cannot have been
    recycled and ``killpg`` on it can only hit this session.
    """
    script = textwrap.dedent(
        """
        import subprocess, sys, time
        c = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        print(c.pid, flush=True)
        time.sleep(600)
        """
    )
    leader = subprocess.Popen(
        [sys.executable, "-I", "-c", script],
        stdout=subprocess.PIPE, text=True, start_new_session=True,
    )
    try:
        child_pid = int(leader.stdout.readline())
        leader_start = psutil.Process(leader.pid).create_time()
        child_start = psutil.Process(child_pid).create_time()
        workload_sessions.append(WorkloadIdentity(leader.pid, leader_start, SID_OWNER_WORKLOAD))
        yield leader, leader_start, child_pid, child_start
    finally:
        try:
            os.killpg(leader.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            # Already gone. macOS answers EPERM, not ESRCH, for a group whose
            # only remaining member is our unreaped (zombie) leader.
            pass
        leader.wait(timeout=10)
        leader.stdout.close()


def _schema2_lease(state: str, cwd: Path, log: Path, wl) -> Lease:
    """A schema-2 lease as the current code writes it, **expired**, over ``wl``."""
    leader, leader_start, child_pid, child_start = wl
    now = datetime.now(timezone.utc).astimezone()
    generation = "c0ffee" + "0" * 26
    registered = state != "starting"
    stop = cleanup = None
    if state in ("stopping", "cleanup_incomplete"):
        stop = StopRequest(
            generation=generation, reason="explicit", reason_source="declared", op="down",
            requested_at=now - timedelta(minutes=2),
            requested_by=ProcessRef(pid=os.getpid(), start_time=psutil.Process().create_time()),
        )
    if state == "cleanup_incomplete":
        cleanup = CleanupRecord(
            attempts=1, last_attempt=now - timedelta(minutes=1),
            survivors=(Survivor(pid=child_pid, start_time=child_start, name="python", status="running"),),
        )
    handle = (
        {
            "pid": child_pid, "pid_start_time": child_start,
            "sid": leader.pid, "sid_owner_start_time": leader_start,
            "sid_owner": SID_OWNER_SUPERVISOR,
        }
        if registered
        else {}
    )
    return Lease(
        project="xver",
        profile="default",
        runner="process",
        handle=handle,
        port=5199,  # never bound: nothing listens, and it is outside 5100-5129
        session="xver-session",
        cwd=str(cwd),
        spawn_cwd=str(cwd),
        created=now - timedelta(hours=2),
        expires=now - timedelta(minutes=5),  # expired: a parsing 1.0.1 reader would stop it
        log=str(log),
        schema=SCHEMA_SUPERVISED,
        generation=generation,
        state=state,
        state_since=now - timedelta(minutes=3),
        plan={"cmd": "sleep 600", "cwd": str(cwd), "port_env": "PORT"},
        supervisor=SupervisorRef(
            pid=leader.pid, start_time=leader_start, registered=registered,
            supervision=SUPERVISION_SESSION if registered else None,
        ),
        readiness="answered" if state == "running" else None,
        stop=stop,
        cleanup=cleanup,
    )


def _write(state: str, paths: DevctlPaths, tmp_path: Path, wl) -> tuple[str, Path, bytes]:
    cwd = tmp_path / "proj"
    cwd.mkdir(exist_ok=True)
    key = lease_key("xver", str(cwd))
    path = paths.lease_file(key)
    lease = _schema2_lease(state, cwd, paths.logs_dir / "xver.log", wl)
    lease.write(path)
    raw = path.read_bytes()
    assert json.loads(raw)["watchdog_pid"] == "supervised"  # the poison is on disk
    return key, path, raw


def _run_old(py: Path, env: dict[str, str], cwd: Path, script: str, *args: str):
    return subprocess.run(
        [str(py), "-I", "-c", script, *args],
        env=env, cwd=cwd, capture_output=True, text=True, timeout=CHILD_TIMEOUT_S,
    )


def _assert_is_old(report: dict, py: Path) -> None:
    assert report["version"] == OLD_VERSION
    assert Path(report["file"]).resolve().is_relative_to(py.parent.parent.resolve())


def _assert_untouched(wl) -> None:
    leader, leader_start, child_pid, child_start = wl
    for pid, start in ((leader.pid, leader_start), (child_pid, child_start)):
        p = psutil.Process(pid)  # raises NoSuchProcess if it was killed
        assert p.create_time() == start
        assert p.status() not in (psutil.STATUS_ZOMBIE, psutil.STATUS_STOPPED)
    assert leader.poll() is None


_WATCH_ONCE = textwrap.dedent(
    """
    import json, sys
    from importlib.metadata import version
    import rentctl
    from rentctl import watchdog
    from rentctl.core.paths import DevctlPaths
    from rentctl.core.service import _now_local
    outcome = watchdog.watch_once(sys.argv[1], DevctlPaths.default(), _now_local())
    print(json.dumps({"version": version("rentctl"), "file": rentctl.__file__, "outcome": outcome}))
    """
)

_WATCHDOG_MAIN = "import sys; from rentctl.watchdog import main; sys.exit(main(sys.argv[1:]))"

_CLI = textwrap.dedent(
    """
    import json, sys
    from importlib.metadata import version
    import rentctl
    from rentctl.cli import main
    print(json.dumps({"version": version("rentctl"), "file": rentctl.__file__}))
    sys.exit(main(sys.argv[1:]))
    """
)


@pytest.mark.parametrize("state", STATES)
def test_1_0_1_watchdog_exits_on_schema2_lease(state, old_python, isolated, workload, tmp_path):
    paths, env = isolated
    key, path, before = _write(state, paths, tmp_path, workload)

    # One tick, called the way 1.0.1's own ``run`` calls it.
    proc = _run_old(old_python, env, tmp_path, _WATCH_ONCE, key)
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    _assert_is_old(report, old_python)
    assert report["outcome"] == CORRUPT
    assert path.read_bytes() == before

    # The deployed entry point (``python -m rentctl.watchdog <key>``): the loop
    # must end on the first tick with the same outcome, not keep babysitting.
    proc = _run_old(old_python, env, tmp_path, _WATCHDOG_MAIN, key, "--interval", "0.1")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == CORRUPT
    assert path.read_bytes() == before

    _assert_untouched(workload)
    assert not paths.events_file.exists() or paths.events_file.read_text() == ""


@pytest.mark.parametrize("state", STATES)
def test_1_0_1_ls_and_sweep_leave_schema2_lease_alone(state, old_python, isolated, workload, tmp_path):
    paths, env = isolated
    _key, path, before = _write(state, paths, tmp_path, workload)

    for cmd in ("ls", "sweep"):
        proc = _run_old(old_python, env, tmp_path, _CLI, cmd)
        assert proc.returncode == 0, f"{cmd}: {proc.stderr}"
        lines = proc.stdout.strip().splitlines()
        _assert_is_old(json.loads(lines[0]), old_python)
        result = json.loads("\n".join(lines[1:]))
        assert result["ok"] is True, result
        if cmd == "ls":
            assert result["environments"] == []  # the lease is invisible to 1.0.1
        else:
            assert result["swept"] == [] and result["kept"] == []
            assert "killed_squatters" not in result
        assert path.read_bytes() == before, f"1.0.1 {cmd} changed the lease"
        _assert_untouched(workload)

    assert not paths.events_file.exists() or paths.events_file.read_text() == ""
