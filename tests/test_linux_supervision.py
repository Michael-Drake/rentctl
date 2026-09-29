"""ADR-0016 plan step 9: the Linux extras and the supervision level.

Two kinds of test live here.

* **Platform-neutral** tests drive the feature detection (a monkeypatched
  libc whose ``prctl`` fails, is missing, or lies), the descendant-tree
  membership through the fake table, the orphan reaper, the level recorded on
  the lease, and ``doctor``'s report. They run everywhere.
* **Linux-only** tests (``LINUX``) are the unit-testable items of the ADR's
  "What the Linux CI leg must confirm" list: L1 (``/proc`` session field),
  L5 (unprivileged subreaper; a ``setsid`` escaper reparents to it and is in
  its descendants) and L6 (pidfd signalling). They run on the ubuntu CI legs.
  Nothing here ever sets the subreaper attribute on the pytest process: every
  real ``prctl`` happens in a child interpreter, because the attribute would
  otherwise make pytest the parent of every orphan of every later test.
"""

from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import psutil
import pytest

from fakeproc import FakeClock, FakeProcessTable
from rentctl import supervisor as sup_mod
from rentctl.core import doctor, procutil
from rentctl.core.doctor import OK, UNKNOWN, WARN
from rentctl.core.leases import Lease, ProcessRef, SupervisorRef
from rentctl.core.lifecycle import Actor, ActorKind, Event, EventKind, new_starting_lease, transition
from rentctl.core.models import SID_OWNER_SUPERVISOR, WorkloadIdentity
from rentctl.core.paths import DevctlPaths
from rentctl.core.procutil import (
    SUPERVISION_SESSION,
    SUPERVISION_SUBREAPER,
    Membership,
    SignalResult,
)
from rentctl.core.workload import RECOVERY, stop_workload

PY = sys.executable

LINUX = pytest.mark.skipif(
    sys.platform != "linux",
    reason="Linux only (ADR-0016 L1/L5/L6): prctl(PR_SET_CHILD_SUBREAPER), pidfd and /proc",
)

S = 5000          # the supervisor's pid = session S (fake table)
SUP_START = 100.0


# ==========================================================================
# feature detection: enable_child_subreaper degrades, never raises
# ==========================================================================


class _FakePrctl:
    """A ``libc.prctl`` stand-in. ``set_rc``/``get_rc`` are the return codes;
    ``isset`` is what PR_GET writes back through the pointer it is given."""

    def __init__(self, set_rc: int = 0, get_rc: int = 0, isset: int = 1, raises: bool = False):
        self.set_rc, self.get_rc, self.isset, self.raises = set_rc, get_rc, isset, raises
        self.calls: list[tuple] = []
        self.argtypes = None
        self.restype = None

    def __call__(self, option, arg2, *rest):
        self.calls.append((option, arg2, *rest))
        if self.raises:
            raise OSError("seccomp said no")
        if option == procutil.PR_SET_CHILD_SUBREAPER:
            return self.set_rc
        if option == procutil.PR_GET_CHILD_SUBREAPER:
            if self.get_rc == 0:
                ctypes.c_int.from_address(arg2).value = self.isset
            return self.get_rc
        raise AssertionError(f"unexpected prctl option {option}")


class _FakeLibc:
    def __init__(self, prctl: _FakePrctl | None):
        if prctl is not None:
            self.prctl = prctl


def test_subreaper_is_never_attempted_off_linux():
    called = []
    assert procutil.enable_child_subreaper(
        platform="darwin", libc_loader=lambda: called.append(1)
    ) is False
    assert called == []  # not even loaded: macOS has no such prctl


def test_subreaper_enabled_when_set_and_read_back():
    prctl = _FakePrctl()
    assert procutil.enable_child_subreaper(platform="linux", libc_loader=lambda: _FakeLibc(prctl))
    assert prctl.calls[0][:2] == (procutil.PR_SET_CHILD_SUBREAPER, 1)
    assert prctl.calls[1][0] == procutil.PR_GET_CHILD_SUBREAPER
    # Declared full-width: prctl(2) takes unsigned longs, not C ints.
    assert prctl.argtypes == [ctypes.c_int] + [ctypes.c_ulong] * 4


@pytest.mark.parametrize(
    "loader",
    [
        pytest.param(lambda: (_ for _ in ()).throw(OSError("no libc")), id="libc-will-not-load"),
        pytest.param(lambda: _FakeLibc(None), id="no-prctl-symbol"),
        pytest.param(lambda: _FakeLibc(_FakePrctl(set_rc=-1)), id="set-refused"),
        pytest.param(lambda: _FakeLibc(_FakePrctl(get_rc=-1)), id="get-refused"),
        pytest.param(lambda: _FakeLibc(_FakePrctl(isset=0)), id="set-without-effect"),
        pytest.param(lambda: _FakeLibc(_FakePrctl(raises=True)), id="ctypes-raises"),
    ],
)
def test_subreaper_failure_degrades_to_session(loader):
    """Every way the prctl can fail is a plain ``False``: the supervisor then
    runs at the macOS guarantee rather than crashing or over-claiming."""
    assert procutil.enable_child_subreaper(platform="linux", libc_loader=loader) is False
    assert sup_mod.establish_supervision(
        lambda: procutil.enable_child_subreaper(platform="linux", libc_loader=loader)
    ) == SUPERVISION_SESSION


def test_establish_supervision_names_the_level(monkeypatch):
    monkeypatch.delenv("RENTCTL_TEST_NO_SUBREAPER", raising=False)
    assert sup_mod.establish_supervision(lambda: True) == SUPERVISION_SUBREAPER
    assert sup_mod.establish_supervision(lambda: False) == SUPERVISION_SESSION


def test_establish_supervision_test_seam_forces_degraded(monkeypatch):
    monkeypatch.setenv("RENTCTL_TEST_NO_SUBREAPER", "1")
    monkeypatch.setenv("RENTCTL_TESTING", "1")
    assert sup_mod.establish_supervision(lambda: True) == SUPERVISION_SESSION
    monkeypatch.delenv("RENTCTL_TESTING")  # honoured only under RENTCTL_TESTING=1
    assert sup_mod.establish_supervision(lambda: True) == SUPERVISION_SUBREAPER


@pytest.mark.skipif(sys.platform == "linux", reason="asserts the non-Linux answer")
def test_pidfd_is_unavailable_off_linux():
    assert procutil.pidfd_available() is False


# ==========================================================================
# descendant-tree membership (fake table)
# ==========================================================================


def _tree_table() -> FakeProcessTable:
    """S's supervisor; a server in S; a setsid escaper that is its grandchild
    (parent: the server) and one that was orphaned to it (parent: S)."""
    t = FakeProcessTable()
    t.add(S, S, SUP_START, name="supervisor", ppid=1)
    t.add(5001, S, 101.0, name="server", ppid=S)
    t.add(5002, 5002, 102.0, name="daemon", ppid=5001, pgid=5002)
    t.add(5003, 5003, 103.0, name="orphan", ppid=S, pgid=5003)
    t.add(6000, 6000, 50.0, name="stranger", ppid=1)  # not ours in any sense
    return t


def test_tree_members_join_the_session_members():
    t = _tree_table()
    plain = procutil.session_scan(S, SUP_START, {S}, table=t)
    assert [m.pid for m in plain.members] == [5001]  # the macOS answer
    tree = procutil.session_scan(S, SUP_START, {S}, table=t, tree_root=S)
    assert tree.state is Membership.MEMBERS
    assert [m.pid for m in tree.members] == [5001, 5002, 5003]


def test_tree_alone_keeps_the_workload_alive():
    """The daemonize shape on a subreaper: S has emptied (the launcher exited),
    but its daemon is still our descendant. That is MEMBERS, not EMPTY."""
    t = FakeProcessTable()
    t.add(S, S, SUP_START, ppid=1)
    t.add(5003, 5003, 103.0, ppid=S)
    assert procutil.session_scan(S, SUP_START, {S}, table=t).state is Membership.EMPTY
    assert procutil.session_scan(S, SUP_START, {S}, table=t, tree_root=S).state is Membership.MEMBERS


def test_tree_zombies_are_not_members():
    t = FakeProcessTable()
    t.add(S, S, SUP_START, ppid=1)
    t.add(5003, 5003, 103.0, ppid=S, status=psutil.STATUS_ZOMBIE)
    assert procutil.session_scan(S, SUP_START, {S}, table=t, tree_root=S).state is Membership.EMPTY


def test_verified_signal_accepts_a_descendant_only_with_tree_root():
    t = _tree_table()
    assert procutil.verified_signal(5002, 102.0, S, signal.SIGTERM, table=t) is SignalResult.LEFT_SESSION
    assert t.sent == []
    res = procutil.verified_signal(5002, 102.0, S, signal.SIGTERM, table=t, tree_root=S)
    assert res is SignalResult.SIGNALLED and t.sent == [(5002, signal.SIGTERM)]
    # A stranger outside S and outside the tree is refused even with tree_root.
    assert procutil.verified_signal(6000, 50.0, S, signal.SIGTERM, table=t, tree_root=S) \
        is SignalResult.LEFT_SESSION
    # Identity still comes first: a descendant with the wrong start time is not it.
    assert procutil.verified_signal(5003, 999.0, S, signal.SIGTERM, table=t, tree_root=S) \
        is SignalResult.IDENTITY_MISMATCH


def test_in_tree_walks_the_parent_chain_and_fails_closed():
    t = _tree_table()
    row = t.row(5002)
    assert procutil.in_tree(row, S, table=t)             # daemon → server → S
    assert not procutil.in_tree(t.row(6000), S, table=t)  # ends at init
    unknown_parent = replace(row, ppid=None)
    assert not procutil.in_tree(unknown_parent, S, table=t)
    t.add(7000, 7000, 1.0, ppid=7777)  # a parent that has vanished from the table
    assert not procutil.in_tree(t.row(7000), S, table=t)
    t.add(8000, 8000, 1.0, ppid=8001)
    t.add(8001, 8001, 1.0, ppid=8000)  # a cycle (a race, never real): bounded, refused
    assert not procutil.in_tree(t.row(8000), S, table=t)


def test_stop_workload_stops_tree_members():
    """A stop over a subreaper identity TERMs the escapers too, and verifies
    the whole tree empty. (Recovery mode: owner mode needs to *be* S.)"""
    # Escapers parented directly by S: the fake does not model reparenting, and
    # on a real subreaper an escaper whose parent dies is reparented to S anyway.
    t = FakeProcessTable()
    t.add(S, S, SUP_START, ppid=1)
    t.add(5001, S, 101.0, ppid=S)
    t.add(5002, 5002, 102.0, ppid=S, pgid=5002, ignores_term=True)
    t.add(5003, 5003, 103.0, ppid=S, pgid=5003)
    t.add(6000, 6000, 50.0, ppid=1)
    clock = FakeClock()
    ident = WorkloadIdentity(S, SUP_START, SID_OWNER_SUPERVISOR, tree_root=S)
    out = stop_workload(ident, RECOVERY, table=t, clock=clock, sleep=clock.sleep)
    assert out.verified and out.escalated, out
    assert {p for p, _ in t.sent} == {5001, 5002, 5003}
    assert t.signals_to(5002) == [signal.SIGTERM, signal.SIGKILL]
    assert 6000 in t.procs and S in t.procs


def test_lease_identity_never_carries_a_tree():
    """The subtree ends with the supervisor, so a lease's identity — which
    reconcilers use after it is gone — must never name one."""
    lease = Lease(
        project="p", profile="default", runner="process",
        handle={"pid": 1, "pid_start_time": 2.0}, port=1, session="s", cwd="/",
        created=_now(), expires=_now() + timedelta(hours=1), log="/dev/null",
    )
    assert lease.ownership().tree_root is None


# ==========================================================================
# the level on the lease
# ==========================================================================


def _now():
    from datetime import datetime

    return datetime.now().astimezone()


def test_supervisor_ref_round_trips_the_level_and_omits_it_when_unset():
    ref = SupervisorRef(10, 20.0, registered=True, supervision=SUPERVISION_SUBREAPER)
    assert SupervisorRef.from_dict(ref.to_dict()) == ref
    legacy = SupervisorRef(10, 20.0, registered=False)
    assert "supervision" not in legacy.to_dict()  # byte-identical to the pre-step-9 shape
    assert SupervisorRef.from_dict(legacy.to_dict()).supervision is None


def test_register_records_the_supervisors_level():
    t = _now()
    lease = new_starting_lease(
        generation="g" * 32, project="p", profile="default", runner="process", port=1,
        session="s", cwd="/", spawn_cwd="/", log="/dev/null", plan={"cmd": "true"},
        now=t, expires=t + timedelta(hours=1),
    )
    me = ProcessRef(4242, 42.0)
    actor = Actor(ActorKind.SUPERVISOR, "g" * 32, me)
    level = SupervisorRef(4242, 42.0, supervision=SUPERVISION_SESSION)
    got = transition(lease, Event(EventKind.REGISTER, t, supervisor=level), actor)
    assert isinstance(got, Lease)
    assert got.supervisor == SupervisorRef(4242, 42.0, registered=True, supervision=SUPERVISION_SESSION)
    # The payload's identity is ignored: the registering actor is who registers.
    forged = SupervisorRef(1, 1.0, supervision=SUPERVISION_SUBREAPER)
    got = transition(lease, Event(EventKind.REGISTER, t, supervisor=forged), actor)
    assert got.supervisor.pid == 4242 and got.supervisor.supervision == SUPERVISION_SUBREAPER
    # No payload (a pre-step-9 caller) registers with no level recorded.
    got = transition(lease, Event(EventKind.REGISTER, t), actor)
    assert got.supervisor.registered and got.supervisor.supervision is None


def test_supervisor_identity_names_its_tree_only_with_the_subreaper(tmp_path):
    paths = DevctlPaths(state_home=tmp_path / "s", config_home=tmp_path / "c")
    plain = sup_mod.Supervisor("p--x", "g", paths)
    assert plain.supervision == SUPERVISION_SESSION and plain.identity().tree_root is None
    sub = sup_mod.Supervisor("p--x", "g", paths, supervision=SUPERVISION_SUBREAPER)
    assert sub.identity().tree_root == os.getpid()


def test_supervisor_is_ours_pid_follows_the_level(tmp_path):
    """A child of this process in its own session: not ours at ``session``,
    ours at ``session+subreaper`` (it is our descendant)."""
    paths = DevctlPaths(state_home=tmp_path / "s", config_home=tmp_path / "c")
    child = subprocess.Popen([PY, "-c", "import time; time.sleep(20)"], start_new_session=True)
    try:
        assert not sup_mod.Supervisor("p--x", "g", paths)._is_ours_pid(child.pid)
        sub = sup_mod.Supervisor("p--x", "g", paths, supervision=SUPERVISION_SUBREAPER)
        assert sub._is_ours_pid(child.pid)
        assert not sub._is_ours_pid(1)
    finally:
        child.kill()
        child.wait()


# ==========================================================================
# real processes, any platform: the descendant table and the reaper
# ==========================================================================

# Spawns a child in its OWN session (a single-fork setsid escaper that stays
# our descendant), writes its pid, and waits for it. 30 s lifetime cap.
PARENT = """\
import subprocess, sys, os
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                         start_new_session=True)
tmp = sys.argv[1] + ".tmp"
open(tmp, "w").write(str(child.pid))
os.replace(tmp, sys.argv[1])
child.wait()
"""


def _read_pid(path: Path, timeout: float = 15.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return int(path.read_text())
        time.sleep(0.02)
    raise AssertionError(f"{path} never appeared")


def test_real_descendant_escaper_is_found_and_signalled_through_the_tree(tmp_path, workload_sessions):
    """The OS table's half of step 9, on real processes (runs on macOS too):
    a ``setsid`` escaper that is still P's descendant is a tree member of P's
    session, and only the tree check lets a verified signal reach it."""
    script = tmp_path / "parent.py"
    script.write_text(PARENT)
    parent = subprocess.Popen([PY, str(script), str(tmp_path / "kid.pid")], start_new_session=True)
    p_start = procutil.observe_start_time(parent.pid)
    workload_sessions.append(WorkloadIdentity(parent.pid, p_start))
    kid = _read_pid(tmp_path / "kid.pid")
    k_start = procutil.observe_start_time(kid)
    workload_sessions.append(WorkloadIdentity(kid, k_start))
    try:
        assert os.getsid(kid) == kid != parent.pid
        plain = procutil.session_scan(parent.pid, p_start, {parent.pid})
        assert plain.state is Membership.EMPTY
        tree = procutil.session_scan(parent.pid, p_start, {parent.pid}, tree_root=parent.pid)
        assert [m.pid for m in tree.members] == [kid]
        assert tree.members[0].ppid == parent.pid
        assert procutil.verified_signal(kid, k_start, parent.pid, signal.SIGTERM) \
            is SignalResult.LEFT_SESSION
        assert procutil.verified_signal(
            kid, k_start, parent.pid, signal.SIGTERM, tree_root=parent.pid
        ) is SignalResult.SIGNALLED
        assert parent.wait(timeout=10) == 0  # its child died, so it exited
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait()


def test_descendant_rows_of_a_missing_root_is_empty():
    assert procutil.OsProcessTable().descendant_rows(2**22 + 12345) == []


def test_reap_orphans_reaps_exited_children_but_not_kept_or_live_ones():
    others = {c.pid for c in psutil.Process().children()}
    dead = subprocess.Popen([PY, "-c", "pass"])
    kept = subprocess.Popen([PY, "-c", "pass"])
    live = subprocess.Popen([PY, "-c", "import time; time.sleep(20)"])
    try:
        def zombie(pid):
            try:
                return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
            except psutil.NoSuchProcess:
                return False
        deadline = time.monotonic() + 10
        while not (zombie(dead.pid) and zombie(kept.pid)) and time.monotonic() < deadline:
            time.sleep(0.02)
        reaped = procutil.reap_orphans(keep={kept.pid, *others})
        assert reaped == [dead.pid]
        assert live.poll() is None
        assert kept.wait(timeout=5) == 0  # its status was left for its Popen
        assert procutil.reap_orphans(keep={kept.pid, live.pid, *others}) == []
    finally:
        live.kill()
        live.wait()


def test_reap_orphans_skips_a_child_already_reaped_elsewhere(monkeypatch):
    class Kid:
        pid = 424242

    class Me:
        def children(self):
            return [Kid()]

    def echild(pid, flags):
        raise ChildProcessError(pid)

    monkeypatch.setattr(procutil.psutil, "Process", lambda *a: Me())
    monkeypatch.setattr(procutil.os, "waitpid", echild)
    assert procutil.reap_orphans() == []


# ==========================================================================
# doctor reports the level and its boundary
# ==========================================================================


def _runner(code: int, out: str = "", err: str = ""):
    seen = []

    def run(argv):
        seen.append(list(argv))
        return code, out, err

    run.seen = seen
    return run


@pytest.mark.skipif(sys.platform != "darwin", reason="asserts this Mac's answer")
def test_doctor_on_macos_reports_session_and_the_setsid_boundary():
    check = doctor.check_supervision()
    assert check.name == "supervision" and check.status == OK
    assert check.detail.startswith("supervision: session (darwin)")
    assert "setsid()" in check.detail and "escapes supervision" in check.detail
    assert "subreaper" not in check.detail
    assert check.probe == "sys.platform"


def test_doctor_reports_session_off_linux_without_running_anything():
    run = _runner(0, "{}")
    check = doctor.check_supervision(platform="darwin", runner=run)
    assert check.status == OK and "supervision: session" in check.detail
    assert run.seen == []


def test_doctor_linux_with_subreaper_is_ok_and_states_the_boundary():
    run = _runner(0, json.dumps({"subreaper": True, "pidfd": True}))
    check = doctor.check_supervision(platform="linux", runner=run)
    assert check.status == OK
    assert check.detail.startswith("supervision: session+subreaper")
    assert "double-forks and setsid()s is still reparented to the supervisor" in check.detail
    assert "systemd-run" in check.detail and "setns(2)" in check.detail
    assert "pidfd (race-free)" in check.detail
    # The capability is executed in a child, not inferred.
    assert run.seen and run.seen[0][:2] == [sys.executable, "-c"]
    assert "enable_child_subreaper" in run.seen[0][2]


def test_doctor_linux_without_subreaper_warns_with_the_macos_boundary():
    check = doctor.check_supervision(
        platform="linux", runner=_runner(0, json.dumps({"subreaper": False, "pidfd": False}))
    )
    assert check.status == WARN
    assert check.detail.startswith("supervision: session")
    assert "escapes supervision" in check.detail
    assert "kill() path" in check.detail


@pytest.mark.parametrize(
    "code,out,err",
    [(1, "", "ModuleNotFoundError: rentctl"), (0, "not json", ""), (0, "[1, 2]", ""), (124, "", "")],
)
def test_doctor_linux_probe_that_cannot_answer_is_unknown(code, out, err):
    check = doctor.check_supervision(platform="linux", runner=_runner(code, out, err))
    assert check.status == UNKNOWN
    assert "could not determine the supervision level" in check.detail


def test_diagnose_includes_the_supervision_check(devctl_home, monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda _c: None)
    names = [c.name for c in doctor.diagnose(devctl_home, runner=_runner(0, '{"ok": true}')).checks]
    assert "supervision" in names


@LINUX
def test_doctor_on_linux_executes_the_real_probe():
    """Never executed on the Mac this was written on. The real child-process
    probe on a Linux host: GitHub's ubuntu runners allow the prctl (ADR L5)."""
    check = doctor.check_supervision()
    assert check.status in (OK, WARN), check
    assert check.detail.startswith("supervision: session")


# ==========================================================================
# Linux only: ADR-0016 L1, L5, L6
# ==========================================================================

ESCAPER = """\
import os, sys, time
os.setsid()
tmp = sys.argv[1] + ".tmp"
open(tmp, "w").write(str(os.getpid()))
os.replace(tmp, sys.argv[1])
time.sleep(30)
"""

# Runs in its own interpreter, which becomes the subreaper — never pytest.
# The escaper is launched from a shell that exits, so it is orphaned and must
# reparent to this process; it is then killed through the tree check (pidfd
# path) and reaped by reap_orphans. Prints one JSON object.
SUBREAPER_ROLE = """\
import json, os, signal, subprocess, sys, time
from pathlib import Path
from rentctl.core import procutil
wd, esc_script = Path(sys.argv[1]), sys.argv[2]
res = {"subreaper": procutil.enable_child_subreaper(), "pidfd": procutil.pidfd_available()}
me, sid = os.getpid(), os.getsid(0)
subprocess.Popen(f"'{sys.executable}' '{esc_script}' '{wd}/esc.pid' & sleep 0.5; exit 0",
                 shell=True).wait()
pidfile = wd / "esc.pid"
deadline = time.monotonic() + 10
while not pidfile.exists() and time.monotonic() < deadline:
    time.sleep(0.02)
esc = int(pidfile.read_text())
table = procutil.OsProcessTable()
row = table.row(esc)
res["esc_sid_is_its_own"] = row.sid == esc != sid
res["esc_ppid_is_subreaper"] = row.ppid == me
res["in_descendants"] = esc in [r.pid for r in table.descendant_rows(me)]
scan = procutil.session_scan(sid, procutil.observe_start_time(me), {me}, tree_root=me)
res["tree_member"] = esc in [m.pid for m in scan.members]
res["without_tree"] = procutil.verified_signal(esc, row.start_time, sid, 0).value
res["with_tree"] = procutil.verified_signal(
    esc, row.start_time, sid, signal.SIGKILL, tree_root=me).value
reaped = []
deadline = time.monotonic() + 10
while esc not in reaped and time.monotonic() < deadline:
    reaped += procutil.reap_orphans()
    time.sleep(0.02)
res["reaped_by_reap_orphans"] = esc in reaped
res["gone"] = procutil.observe_start_time(esc) is None
print(json.dumps(res))
"""


@LINUX
def test_l1_proc_stat_session_field_matches_getsid(tmp_path):
    """L1: ``os.getsid`` agrees with field 6 of ``/proc/<pid>/stat``, for this
    process and for a child in another session."""
    child = subprocess.Popen([PY, "-c", "import time; time.sleep(20)"], start_new_session=True)
    try:
        for pid in (os.getpid(), child.pid):
            with open(f"/proc/{pid}/stat") as f:
                field6 = int(f.read().rsplit(")", 1)[1].split()[3])
            assert field6 == os.getsid(pid)
        assert os.getsid(child.pid) == child.pid
    finally:
        child.kill()
        child.wait()


@LINUX
def test_l5_l6_setsid_escaper_reparents_to_the_subreaper_and_dies_by_pidfd(tmp_path):
    """L5 + L6, in a child interpreter: the prctl succeeds unprivileged; a
    ``setsid`` escaper orphaned by its shell reparents to the subreaper and is
    in its descendants and its tree membership; the plain session check
    refuses it and the tree check signals it (through a pidfd); the subreaper
    then reaps it with ``reap_orphans``."""
    esc_script = tmp_path / "esc.py"
    esc_script.write_text(ESCAPER)
    role = tmp_path / "role.py"
    role.write_text(SUBREAPER_ROLE)
    try:
        out = subprocess.run(
            [PY, str(role), str(tmp_path), str(esc_script)],
            capture_output=True, text=True, timeout=60, start_new_session=True,
        )
        assert out.returncode == 0, out.stderr
        res = json.loads(out.stdout.strip().splitlines()[-1])
        assert res == {
            "subreaper": True,
            "pidfd": True,
            "esc_sid_is_its_own": True,
            "esc_ppid_is_subreaper": True,
            "in_descendants": True,
            "tree_member": True,
            "without_tree": "left_session",
            "with_tree": "signalled",
            "reaped_by_reap_orphans": True,
            "gone": True,
        }, res
    finally:
        pidfile = tmp_path / "esc.pid"
        if pidfile.exists():
            pid = int(pidfile.read_text())
            start = procutil.observe_start_time(pid)
            if start is not None:  # the role failed before killing it: the test started it
                procutil.verified_signal(pid, start, os.getsid(pid), signal.SIGKILL)


@LINUX
def test_l6_verified_signal_goes_through_a_pidfd(monkeypatch):
    """L6: on Linux the OS table pins the target with ``pidfd_open`` *before*
    verifying it, and signals through ``pidfd_send_signal`` — not ``kill``."""
    assert procutil.pidfd_available()
    opened, sent = [], []
    real_open, real_send = os.pidfd_open, signal.pidfd_send_signal

    def spy_open(pid, *a):
        opened.append(pid)
        return real_open(pid, *a)

    def spy_send(fd, sig, *a):
        sent.append(sig)
        return real_send(fd, sig, *a)

    real_kill = os.kill

    def no_kill(pid, sig):
        if sig == 0:  # an existence probe (psutil may use one): not a signal
            return real_kill(pid, 0)
        raise AssertionError("os.kill used on the pidfd path")

    child = subprocess.Popen([PY, "-c", "import time; time.sleep(20)"], start_new_session=True)
    try:
        start = procutil.observe_start_time(child.pid)
        monkeypatch.setattr(os, "pidfd_open", spy_open)
        monkeypatch.setattr(signal, "pidfd_send_signal", spy_send)
        monkeypatch.setattr(os, "kill", no_kill)
        res = procutil.verified_signal(child.pid, start, child.pid, signal.SIGTERM)
        monkeypatch.undo()
        assert res is SignalResult.SIGNALLED
        assert opened == [child.pid] and sent == [signal.SIGTERM]
        assert child.wait(timeout=10) == -signal.SIGTERM
        # A pid that has exited and been reaped: pidfd_open says ESRCH → GONE.
        assert procutil.verified_signal(child.pid, start, child.pid, signal.SIGTERM) \
            is SignalResult.GONE
    finally:
        monkeypatch.undo()
        if child.poll() is None:
            child.kill()
            child.wait()


@LINUX
def test_l5_enable_child_subreaper_really_works_unprivileged():
    """L5, the product function itself, in a throwaway child."""
    out = subprocess.run(
        [PY, "-c", "from rentctl.core import procutil; import sys; "
                   "sys.exit(0 if procutil.enable_child_subreaper() else 1)"],
        timeout=30,
    )
    assert out.returncode == 0
