"""Denied process inspection reads as unknown, never as "no processes" (Astra P2).

Against the REAL ``OsProcessTable``, with only the refusal injected: the Codex
macOS sandbox raises ``PermissionError`` from ``psutil.pids()`` ("Operation not
permitted (originated from sysctl())"), and a partial denial surfaces as EPERM
from ``getsid`` on some pids. Either way the scan must say AMBIGUOUS — skipping
a refused pid would return fewer members, or none, and "none" removes a lease
over a live process. ESRCH (the process is gone) is still skipped.

Each denial test is paired with its baseline: the same scan, unrefused, finds
this test process as a member, so the denial is what changes the answer.
"""

from __future__ import annotations

import os

import psutil
import pytest

from rentctl.core import procutil
from rentctl.core.procutil import Membership

STRANGER = 999_999  # a pid that is not ours; getsid on it is what gets refused


@pytest.fixture
def own_session():
    sid = os.getsid(0)
    return sid, procutil.observe_start_time(sid)


def scan(own_session, **kw):
    sid, start = own_session
    return procutil.session_scan(sid, start, **kw)


def test_baseline_the_unrefused_scan_finds_this_process(own_session):
    got = scan(own_session)
    assert got.state is Membership.MEMBERS
    assert os.getpid() in {m.pid for m in got.members}
    assert procutil.inspection_denied() is None


def test_a_refused_process_list_is_ambiguous_not_empty(own_session, monkeypatch):
    def refuse():
        raise PermissionError(1, "Operation not permitted (originated from sysctl() malloc 1/3)")

    monkeypatch.setattr(procutil.psutil, "pids", refuse)
    got = scan(own_session)
    assert got.state is Membership.AMBIGUOUS and got.members == ()
    assert "denied" in got.reason and "sysctl" in got.reason
    assert "sysctl" in procutil.inspection_denied()


def _getsid_refusing(error: OSError):
    real = os.getsid

    def getsid(pid: int) -> int:
        if pid == STRANGER:
            raise error
        return real(pid)

    return getsid


def test_a_refused_getsid_on_one_pid_is_ambiguous_not_a_shorter_list(own_session, monkeypatch):
    monkeypatch.setattr(procutil.psutil, "pids", lambda: [os.getpid(), STRANGER])
    monkeypatch.setattr(procutil.os, "getsid", _getsid_refusing(PermissionError(1, "EPERM")))
    got = scan(own_session)
    assert got.state is Membership.AMBIGUOUS
    assert f"getsid({STRANGER})" in got.reason


def test_a_vanished_pid_is_still_skipped(own_session, monkeypatch):
    """ESRCH is 'gone', which cannot be a member: the scan stays exact."""
    monkeypatch.setattr(procutil.psutil, "pids", lambda: [os.getpid(), STRANGER])
    monkeypatch.setattr(procutil.os, "getsid", _getsid_refusing(ProcessLookupError(3, "ESRCH")))
    got = scan(own_session)
    assert got.state is Membership.MEMBERS
    assert os.getpid() in {m.pid for m in got.members}


def test_refused_descendants_are_ambiguous_not_empty(own_session, monkeypatch):
    """The Linux subreaper path adds descendants; a refusal there is the same rule."""

    def refuse(self, recursive=False):
        raise psutil.AccessDenied(self.pid)

    monkeypatch.setattr(procutil.psutil.Process, "children", refuse)
    got = scan(own_session, tree_root=os.getpid())
    assert got.state is Membership.AMBIGUOUS
