"""ADR-0016 §4: the one stop algorithm, driven through the fake process table.

``stop_workload`` is what the process runner's ``stop`` runs today (recovery
mode) and what the supervisor will run tomorrow (owner mode). The races that
decide whether it is safe — a member forked mid-shutdown, a pid recycled between
the scan and the signal, the session number changing hands — are driven here
deterministically, on a fake clock, so no test waits out a real grace period.
"""

from __future__ import annotations

import signal

import pytest

from fakeproc import FakeClock, FakeProcessTable
from rentctl.core import workload
from rentctl.core.models import SID_OWNER_SUPERVISOR, WorkloadIdentity

S = 7_000_000  # far above any real pid (macOS wraps at 99999)
OWNER_START = 1000.0


# --- stop_workload against the seam (ADR-0016 §4) -----------------------------

def ident(**kw):
    return WorkloadIdentity(sid=S, owner_start=OWNER_START, **kw)


def run_stop(t, *, identity=None, mode=workload.RECOVERY, term=1.0, kill=0.5, **kw):
    clk = FakeClock()
    out = workload.stop_workload(
        identity or ident(),
        mode,
        term_grace_s=term,
        kill_grace_s=kill,
        table=t,
        clock=clk,
        sleep=clk.sleep,
        **kw,
    )
    return out, clk


def test_stop_of_an_empty_session_sends_nothing():
    out, _ = run_stop(FakeProcessTable())
    assert out.verified and not out.signalled and not out.escalated


def test_stop_of_a_cooperative_workload_is_verified_without_escalation():
    t = FakeProcessTable()
    t.add(S, sid=S, start=OWNER_START)
    t.add(S + 1, sid=S, start=OWNER_START + 1)
    out, clk = run_stop(t)
    assert out.verified and out.signalled and not out.escalated
    assert sum(clk.slept) <= workload.RESCAN_S  # did not wait out the grace


def test_a_term_ignoring_member_is_escalated_per_pid():
    t = FakeProcessTable()
    t.add(S + 1, sid=S, start=OWNER_START + 1, ignores_term=True)
    out, clk = run_stop(t, term=1.0)
    assert out.verified and out.escalated
    assert t.signals_to(S + 1) == [signal.SIGTERM, signal.SIGKILL]
    assert sum(clk.slept) >= 1.0  # the whole term grace was given first


def test_a_member_forked_during_shutdown_gets_its_own_term():
    """E3's `forker`: on TERM it starts a new child in S, then exits."""
    t = FakeProcessTable()

    def fork_on_term(table, proc):
        table.add(S + 2, sid=S, start=OWNER_START + 5, ignores_term=True)

    t.add(S + 1, sid=S, start=OWNER_START + 1, on_term=fork_on_term)
    out, _ = run_stop(t)
    assert out.verified and out.escalated
    assert t.signals_to(S + 2) == [signal.SIGTERM, signal.SIGKILL]


def test_an_unkillable_survivor_is_incomplete_and_named():
    """A UID-changed child or a D-state process: the stop must say so, not return
    as though it worked. Survivors carry pid, start time, name and status."""
    t = FakeProcessTable()
    t.add(S + 1, sid=S, start=OWNER_START + 1, ignores_term=True, unkillable=True, name="stuck")
    out, _ = run_stop(t)
    assert not out.verified and out.escalated
    assert out.survivor_dicts() == [
        {"pid": S + 1, "start_time": OWNER_START + 1, "name": "stuck", "status": "sleeping"}
    ]
    assert "survived" in out.detail


def test_ambiguous_owner_is_refused_with_no_signal_at_all():
    t = FakeProcessTable()
    t.add(S, sid=S, start=OWNER_START + 500)  # PID S is someone else now
    t.add(S + 1, sid=S, start=OWNER_START + 501)
    out, _ = run_stop(t)
    assert not out.verified and out.identity_ambiguous and not out.signalled
    assert t.sent == []


def test_ambiguity_arising_mid_stop_halts_all_signalling():
    """If PID S changes hands while we wait, we stop before the SIGKILL round."""
    t = FakeProcessTable()

    def reuse_owner_pid(table, proc):
        table.add(S, sid=S, start=OWNER_START + 900, name="stranger")

    t.add(S + 1, sid=S, start=OWNER_START + 1, ignores_term=True, on_term=reuse_owner_pid)
    out, _ = run_stop(t)
    assert out.identity_ambiguous and not out.verified
    assert t.signals_to(S + 1) == [signal.SIGTERM]  # no SIGKILL was sent
    assert t.signals_to(S) == []


def test_decoy_pid_with_wrong_start_time_never_signalled():
    """#12 (seam): a process in session S that is older than S's owner — the
    shape of a reused number — is not a member and gets nothing, while the
    real member beside it is stopped."""
    t = FakeProcessTable()
    t.add(S + 1, sid=S, start=OWNER_START + 1)
    t.add(S + 2, sid=S, start=OWNER_START - 300, name="decoy", ignores_term=True)
    out, _ = run_stop(t)
    assert out.verified
    assert t.signals_to(S + 1) == [signal.SIGTERM]
    assert t.signals_to(S + 2) == []


def test_identity_rechecked_between_enumerate_and_signal():
    """#12 (seam): the member exits and its pid is taken by an unrelated process
    after the scan and before the signal. The re-check refuses it."""
    t = FakeProcessTable()
    t.add(S + 1, sid=S, start=OWNER_START + 1)

    def recycle(pid):
        if pid == S + 1:
            t.add(S + 1, sid=4242, start=OWNER_START + 800, name="unrelated")

    t.before_verify = recycle
    out, _ = run_stop(t)
    assert t.sent == []
    assert out.verified  # S is empty: the member is gone, the stranger is not in S
    assert S + 1 in t.procs  # and the stranger was left alone


def test_owner_mode_killpgs_its_own_group_and_terms_escapers(monkeypatch):
    """Plan step 4's supervisor: killpg(S) is safe only from inside S. A member
    that setpgid()'d out of group S still gets its own verified TERM."""
    t = FakeProcessTable()
    t.add(S, sid=S, start=OWNER_START)                          # the supervisor
    t.add(S + 1, sid=S, start=OWNER_START + 1, pgid=S)          # in group S
    t.add(S + 2, sid=S, start=OWNER_START + 1, pgid=S + 2)      # setpgid escaper
    groups: list[tuple[int, int]] = []

    def fake_killpg(pgid, sig):
        groups.append((pgid, sig))
        for p in [p for p in t.procs.values() if p.pgid == pgid and p.pid != S]:
            t.kill_now(p.pid)

    monkeypatch.setattr(workload.os, "getpgid", lambda pid: S)
    monkeypatch.setattr(workload.os, "killpg", fake_killpg)
    reaped: list[int] = []
    out, _ = run_stop(
        t,
        identity=ident(owner=SID_OWNER_SUPERVISOR),
        mode=workload.OWNER,
        reap=lambda: reaped.append(1),
    )
    assert out.verified and not out.escalated
    assert groups == [(S, signal.SIGTERM)]
    assert t.signals_to(S + 2) == [signal.SIGTERM]
    assert t.signals_to(S + 1) == []  # the group signal covered it
    assert t.signals_to(S) == []      # the supervisor is never signalled
    assert reaped


def test_owner_mode_from_outside_the_session_is_refused(monkeypatch):
    t = FakeProcessTable()
    t.add(S + 1, sid=S, start=OWNER_START + 1)
    monkeypatch.setattr(workload.os, "getpgid", lambda pid: 12345)
    with pytest.raises(ValueError, match="outside session"):
        run_stop(t, mode=workload.OWNER)
    assert t.sent == []


def test_unknown_stop_mode_is_refused():
    with pytest.raises(ValueError, match="unknown stop mode"):
        run_stop(FakeProcessTable(), mode="bogus")


