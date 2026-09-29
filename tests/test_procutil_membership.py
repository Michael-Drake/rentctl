"""ADR-0016 step 1: session membership, owner verification, verified signals.

The decision logic runs against ``fakeproc.FakeProcessTable`` so the cases it
exists for — a reused session number, a pid that changed identity, a process
that left the session — are driven deterministically. A few tests at the bottom
touch the real table to pin the OS facts the design leans on (E1: pid 0 is
skipped, a zombie holds its pid). The stop algorithm built on these is in
``test_workload_stop.py``.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from fakeproc import FakeProcessTable
from rentctl.core import procutil
from rentctl.core.procutil import (
    IdentityAmbiguous,
    Membership,
    OsProcessTable,
    ProcRow,
    SignalResult,
    classify_session,
    owner_reused,
    session_members,
    session_scan,
    verified_signal,
)

# Session numbers far above any real pid (macOS wraps at 99999), so a fake
# entry can never coincide with this test process's own pid.
S = 7_000_000
OWNER_START = 1000.0


def row(pid, sid=S, start=OWNER_START + 1, status="sleeping", name="p", pgid=None):
    return ProcRow(pid=pid, sid=sid, start_time=start, pgid=pgid, name=name, status=status)


# --- classify_session: the pure decision --------------------------------------

def test_no_live_process_in_the_session_is_empty():
    assert classify_session([], None, S, OWNER_START).state is Membership.EMPTY


def test_zombies_are_never_members():
    rows = [row(S + 1, status="zombie")]
    assert classify_session(rows, None, S, OWNER_START).state is Membership.EMPTY


def test_members_include_a_workload_owner_and_its_children_sorted():
    owner = row(S, start=OWNER_START)
    rows = [row(S + 2), owner, row(S + 1)]
    scan = classify_session(rows, owner, S, OWNER_START)
    assert scan.state is Membership.MEMBERS
    assert [m.pid for m in scan.members] == [S, S + 1, S + 2]


def test_a_supervisor_owner_is_excluded_from_its_own_members():
    sup = row(S, start=OWNER_START)
    scan = classify_session([sup, row(S + 1)], sup, S, OWNER_START, exclude={S})
    assert [m.pid for m in scan.members] == [S + 1]
    # Only the supervisor left: the workload is gone.
    assert classify_session([sup], sup, S, OWNER_START, exclude={S}).state is Membership.EMPTY


def test_a_process_older_than_the_owner_is_not_a_member():
    """Membership condition 3: born no earlier than the owner, less 1 s."""
    rows = [row(S + 1, start=OWNER_START - 5), row(S + 2, start=OWNER_START - 0.5)]
    scan = classify_session(rows, None, S, OWNER_START)
    assert [m.pid for m in scan.members] == [S + 2]  # inside the tolerance


def test_the_leader_dead_leaves_its_members_findable():
    """E2: the 1.0.x shell died; its TERM-ignoring child still reports sid S."""
    scan = classify_session([row(S + 1)], None, S, OWNER_START)
    assert scan.state is Membership.MEMBERS


def test_owner_pid_held_by_a_different_process_is_ambiguous():
    """A process that took PID S and called setsid() would itself be 'in S' and
    newer than our owner — it would pass the membership test. So once PID S is
    someone else, NOTHING under that number is ours to signal."""
    stranger = row(S, start=OWNER_START + 500, name="stranger")
    scan = classify_session([stranger, row(S + 1)], stranger, S, OWNER_START)
    assert scan.state is Membership.AMBIGUOUS
    assert scan.members == ()
    assert "stranger" in scan.reason


def test_an_unrecorded_owner_start_is_ambiguous_not_empty():
    scan = classify_session([row(S + 1)], None, S, 0.0)
    assert scan.state is Membership.AMBIGUOUS


def test_a_reused_number_is_moot_when_the_session_is_empty():
    """Nobody in S at all → EMPTY, whoever holds PID S now. Otherwise a lease
    whose workload ended long ago would be stuck as ambiguous forever."""
    stranger = row(S, sid=12345, start=OWNER_START + 500)
    assert classify_session([], stranger, S, OWNER_START).state is Membership.EMPTY


def test_a_member_with_an_unreadable_start_time_is_kept_not_dropped():
    """With the owner verified everything in S is ours; dropping it would read
    as 'verified empty' over a live process. Kept, it surfaces as a survivor."""
    scan = classify_session([row(S + 1, start=None)], None, S, OWNER_START)
    assert scan.state is Membership.MEMBERS


# --- the impure wrappers, through the seam ------------------------------------

def test_session_scan_and_members_through_the_table():
    t = FakeProcessTable()
    t.add(S, sid=S, start=OWNER_START)
    t.add(S + 1, sid=S, start=OWNER_START + 1)
    t.add(S + 9, sid=S + 9, start=OWNER_START + 1)  # another session entirely
    assert session_scan(S, OWNER_START, table=t).state is Membership.MEMBERS
    assert [m.pid for m in session_members(S, OWNER_START, table=t)] == [S, S + 1]
    assert [m.pid for m in session_members(S, OWNER_START, exclude={S}, table=t)] == [S + 1]


def test_session_members_raises_rather_than_returning_an_empty_list():
    """[] means verified empty. Ambiguity must not wear that shape."""
    t = FakeProcessTable()
    t.add(S, sid=S, start=OWNER_START + 500)
    with pytest.raises(IdentityAmbiguous):
        session_members(S, OWNER_START, table=t)


def test_owner_reused():
    t = FakeProcessTable()
    assert owner_reused(S, OWNER_START, table=t) is False            # pid free
    t.add(S, sid=S, start=OWNER_START)
    assert owner_reused(S, OWNER_START, table=t) is False            # still the owner
    assert owner_reused(S, 0.0, table=t) is True                     # unverifiable
    t.add(S, sid=S, start=OWNER_START + 500)
    assert owner_reused(S, OWNER_START, table=t) is True             # someone else


# --- verified_signal ---------------------------------------------------------

def test_verified_signal_sends_only_to_the_same_process_in_the_same_session():
    t = FakeProcessTable()
    t.add(S + 1, sid=S, start=OWNER_START + 1)
    assert verified_signal(S + 1, OWNER_START + 1, S, signal.SIGTERM, table=t) is SignalResult.SIGNALLED
    assert t.sent == [(S + 1, signal.SIGTERM)]


@pytest.mark.parametrize(
    "setup, expected",
    [
        (lambda t: None, SignalResult.GONE),
        (lambda t: t.add(S + 1, sid=S, start=OWNER_START + 1, status="zombie"), SignalResult.GONE),
        (lambda t: t.add(S + 1, sid=S, start=OWNER_START + 99), SignalResult.IDENTITY_MISMATCH),
        (lambda t: t.add(S + 1, sid=S + 1, start=OWNER_START + 1), SignalResult.LEFT_SESSION),
    ],
    ids=["gone", "zombie", "pid-reused", "setsid-escaper"],
)
def test_verified_signal_refuses(setup, expected):
    t = FakeProcessTable()
    setup(t)
    assert verified_signal(S + 1, OWNER_START + 1, S, signal.SIGKILL, table=t) is expected
    assert t.sent == []


@pytest.mark.parametrize("pid", [0, -1, -S, os.getpid()])
def test_verified_signal_never_targets_a_group_everything_or_itself(pid):
    """kill(0) is "my group", kill(-1) "everything", kill(-n) a group."""
    t = FakeProcessTable()
    assert verified_signal(pid, OWNER_START, S, signal.SIGKILL, table=t) is SignalResult.IDENTITY_MISMATCH
    assert t.sent == []


def test_verified_signal_without_a_captured_start_time_refuses():
    t = FakeProcessTable()
    t.add(S + 1, sid=S, start=None)
    assert verified_signal(S + 1, None, S, signal.SIGKILL, table=t) is SignalResult.IDENTITY_MISMATCH
    assert t.sent == []


# --- the real table: the OS facts the design leans on (E1) -------------------

def test_real_table_finds_this_process_in_its_own_session():
    rows = OsProcessTable().session_rows(os.getsid(0))
    assert os.getpid() in {r.pid for r in rows}
    assert 0 not in {r.pid for r in rows}  # getsid(0) would be the caller: skipped


def test_real_table_row_for_no_such_pid_is_none():
    t = OsProcessTable()
    assert t.row(0) is None
    assert t.row(2_000_000_000) is None


def test_real_table_row_for_a_zombie_still_holds_its_start_time():
    """A zombie holds its pid, and psutil still reports its create_time on
    macOS: that is what lets an unreaped legacy leader read as 'the owner' and
    not as a reused number."""
    child = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    try:
        zombie = None
        for _ in range(200):
            zombie = OsProcessTable().row(child.pid)
            if zombie is not None and zombie.zombie:
                break
            time.sleep(0.02)
        assert zombie is not None and zombie.zombie
        assert zombie.start_time is not None
        assert procutil.session_scan(child.pid, zombie.start_time).state is Membership.EMPTY
    finally:
        child.wait()


def test_real_table_access_denied_leaves_the_start_time_unknown(monkeypatch):
    class Denied:
        def __init__(self, pid):
            pass

        def create_time(self):
            raise procutil.psutil.AccessDenied(1)

        def name(self):
            raise procutil.psutil.AccessDenied(1)

        def ppid(self):
            raise procutil.psutil.AccessDenied(1)

    monkeypatch.setattr(procutil.psutil, "Process", Denied)
    r = OsProcessTable().row(os.getpid())
    assert r is not None and r.start_time is None and r.name == "" and r.ppid is None


def test_real_table_vanished_mid_read_is_none(monkeypatch):
    class Vanished:
        def __init__(self, pid):
            pass

        def create_time(self):
            raise procutil.psutil.NoSuchProcess(1)

    monkeypatch.setattr(procutil.psutil, "Process", Vanished)
    assert OsProcessTable().row(os.getpid()) is None


def test_real_send_reports_gone_and_denied(monkeypatch):
    t = OsProcessTable()
    monkeypatch.setattr(procutil, "_HAVE_PIDFD", False)  # the plain-kill path, on every OS

    def gone(pid, sig):
        raise ProcessLookupError

    def denied(pid, sig):
        raise PermissionError

    monkeypatch.setattr(procutil.os, "kill", gone)
    assert t.send(1, 0, lambda: None) is SignalResult.GONE
    monkeypatch.setattr(procutil.os, "kill", denied)
    assert t.send(1, 0, lambda: None) is SignalResult.DENIED
    assert t.send(1, 0, lambda: SignalResult.LEFT_SESSION) is SignalResult.LEFT_SESSION
