"""Readiness is the supervisor's now (ADR-0016 §6 step 3): the branches, on seams.

These are the service-level readiness tests of 1.0.x, moved to where the probe
runs since plan step 5. The questions are the same — "not on loopback" is not
"did not start", a probe that cannot run is not evidence of absence — but the
listener is now attributed by SESSION (``getsid(owner) == S``), not by process
group, so a listener outside S is never taken as ours (§5, R2).

``Supervisor.await_ready`` runs in this process against a real lease file with
the machine faked: the loopback dial, the owner probe, the session scan and
the attribution. Nothing is launched and nothing is signalled.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from rentctl import supervisor as sup_mod
from rentctl.core import events as ev
from rentctl.core import procutil
from rentctl.core.errors import START_TIMEOUT, UNSUPERVISABLE
from rentctl.core.leases import StopRequest
from rentctl.core.lifecycle import new_starting_lease
from rentctl.core.models import ProcInfo, Readiness
from rentctl.core.paths import lease_key
from rentctl.core.procutil import Membership
from rentctl.core.supervision import self_ref

CDT = timezone(timedelta(hours=-5))
T0 = datetime(2026, 9, 28, 9, 0, tzinfo=CDT)
GEN = "b" * 32
PORT = 47123
OURS = 31337        # a listener in session S (npm → node: a child, not the shell)
STRANGER = 999


@pytest.fixture
def sup(devctl_home, monkeypatch):
    key = lease_key("rdy", "/tmp/rdy")
    lease = new_starting_lease(
        generation=GEN, project="rdy", profile="default", runner="process", port=PORT,
        session="s", cwd="/tmp/rdy", spawn_cwd="/tmp/rdy", log="/dev/null",
        plan={"cmd": "true", "cwd": "/tmp", "port_env": "PORT"}, now=T0,
        expires=T0 + timedelta(hours=1),
    )
    lease.write(devctl_home.lease_file(key))
    s = sup_mod.Supervisor(key, GEN, devctl_home)
    s.readiness_timeout_s = 0.25
    s.lease = lease
    s.scan = lambda: Membership.MEMBERS          # our workload is alive…
    s._is_ours = lambda pid: pid == OURS          # …and OURS is in session S
    s._born_after_launch = lambda pid: False
    monkeypatch.setattr(sup_mod, "_port_answering", lambda port: False)
    monkeypatch.setattr(procutil, "port_owner", lambda port: None)
    monkeypatch.setattr(sup_mod, "EMPTY_LISTENER_GRACE_S", 0.1)
    return s


def owner(pid: int, name: str = "node"):
    return lambda port: ProcInfo(pid=pid, name=name, cmdline=())


def test_a_server_that_bound_a_non_loopback_address_is_kept(sup, monkeypatch):
    """THE regression: started, listening, not on loopback → keep it."""
    monkeypatch.setattr(procutil, "port_owner", owner(OURS))
    assert sup.await_ready() is Readiness.LISTENING


def test_a_server_that_starts_listening_during_the_owner_probe_is_answered(sup, monkeypatch):
    """The race found under load: the loopback dial fails, the owner probe (an
    lsof subprocess) runs long enough for the server to start listening, and the
    probe then sees our listener. Deciding on the stale dial reported LISTENING —
    "http://localhost will not reach it" — for a server that was merely late."""
    listening = {"now": False}

    def probe(port):
        listening["now"] = True        # the server came up while lsof ran
        return ProcInfo(pid=OURS, name="node", cmdline=())

    monkeypatch.setattr(sup_mod, "_port_answering", lambda port: listening["now"])
    monkeypatch.setattr(procutil, "port_owner", probe)
    assert sup.await_ready() is Readiness.ANSWERED


def test_a_server_that_never_listened_fails(sup):
    """F8 unchanged: the probe ran, nothing of ours is there → a failure, which
    the supervisor answers by stopping its own session."""
    verdict = sup.await_ready()
    assert isinstance(verdict, sup_mod._Failure)
    assert verdict.code == START_TIMEOUT and verdict.phase == ev.PHASE_READINESS
    assert "nothing of ours is listening" in verdict.message


def test_a_foreign_listener_is_not_ours(sup, monkeypatch):
    """A listener outside session S did not come up for us — whoever it is."""
    monkeypatch.setattr(sup_mod, "_port_answering", lambda port: True)
    monkeypatch.setattr(procutil, "port_owner", owner(STRANGER))
    verdict = sup.await_ready()
    assert verdict.code == START_TIMEOUT and verdict.phase == ev.PHASE_FOREIGN_LISTENER
    assert str(STRANGER) in verdict.message


def test_a_probe_that_could_not_run_keeps_the_server(sup, monkeypatch):
    """ProbeUnavailable is not evidence of absence: keep it, and say so."""

    def unavailable(port):
        raise procutil.ProbeUnavailable("no usable port probe")

    monkeypatch.setattr(procutil, "port_owner", unavailable)
    assert sup.await_ready() is Readiness.UNKNOWN


def test_answering_with_an_unrunnable_owner_probe_is_unknown_not_a_failure(sup, monkeypatch):
    def unavailable(port):
        raise procutil.ProbeUnavailable("no usable port probe")

    monkeypatch.setattr(sup_mod, "_port_answering", lambda port: True)
    monkeypatch.setattr(procutil, "port_owner", unavailable)
    assert sup.await_ready() is Readiness.UNKNOWN


def test_an_unattributable_listener_is_never_taken_as_ours(sup, monkeypatch):
    """1.0.x read an unreadable process group as UNKNOWN and kept the start.
    Attribution is by session now, and a listener whose session cannot be
    read is not in S: an occupied port is never evidence of ownership (§5)."""
    monkeypatch.setattr(procutil, "port_owner", owner(OURS + 1))
    verdict = sup.await_ready()
    assert isinstance(verdict, sup_mod._Failure) and verdict.phase == ev.PHASE_FOREIGN_LISTENER


def test_answering_on_loopback_is_attributed_with_one_owner_probe(sup, monkeypatch):
    """1.0.x skipped the owner probe when loopback answered; that is exactly how
    a stranger's answer passed as ours. One probe settles it, and only one."""
    monkeypatch.setattr(sup_mod, "_port_answering", lambda port: True)
    asked: list[int] = []

    def ours(port):
        asked.append(port)
        return ProcInfo(pid=OURS, name="node", cmdline=())

    monkeypatch.setattr(procutil, "port_owner", ours)
    assert sup.await_ready() is Readiness.ANSWERED
    assert asked == [PORT]


def test_our_process_died_and_a_stranger_answers(sup, monkeypatch):
    """The 2026-08-01 audit's top finding, now by session: our command died on
    EADDRINUSE and a foreign listener answers. Not ours, so not up."""
    monkeypatch.setattr(sup_mod, "_port_answering", lambda port: True)
    monkeypatch.setattr(procutil, "port_owner", owner(STRANGER))
    sup.scan = lambda: Membership.EMPTY
    verdict = sup.await_ready()
    assert verdict.code == START_TIMEOUT and "exited during startup" in verdict.message


def test_a_known_empty_workload_is_never_declared_started_on_an_unknown_answer(sup, monkeypatch):
    """Astra's P2, the COMBINED case the two tests above only cover apart: our
    session is EMPTY, something answers the port, and the owner probe cannot
    run. The permissive "cannot tell is up" branch must not fire for a workload
    already known to be gone — the answer is someone else's. It fails the start
    through the bounded empty-session check instead."""

    def unavailable(port):
        raise procutil.ProbeUnavailable("no usable port probe")

    monkeypatch.setattr(sup_mod, "_port_answering", lambda port: True)
    monkeypatch.setattr(procutil, "port_owner", unavailable)
    sup.scan = lambda: Membership.EMPTY
    verdict = sup.await_ready()
    assert verdict is not Readiness.UNKNOWN
    assert isinstance(verdict, sup_mod._Failure)
    assert verdict.code == START_TIMEOUT and "exited during startup" in verdict.message


def test_a_listener_that_left_the_session_after_launch_is_unsupervisable(sup, monkeypatch):
    """R2: positive evidence only — our session is empty and the port is
    answered from outside it by a process born after we launched."""
    monkeypatch.setattr(sup_mod, "_port_answering", lambda port: True)
    monkeypatch.setattr(procutil, "port_owner", owner(STRANGER, "daemon"))
    sup.scan = lambda: Membership.EMPTY
    sup._born_after_launch = lambda pid: pid == STRANGER
    verdict = sup.await_ready()
    assert verdict.code == UNSUPERVISABLE and verdict.phase == ev.PHASE_UNSUPERVISABLE


def test_a_stop_requested_during_startup_abandons_it(sup, devctl_home):
    lease = sup.lease
    stopped = lease.__class__(**{**lease.__dict__, "stop": StopRequest(
        generation=GEN, reason=ev.EXPLICIT, reason_source=ev.DECLARED, op="down",
        requested_at=T0, requested_by=self_ref(),
    )})
    stopped.write(sup.lease_path)
    verdict = sup.await_ready()
    assert verdict.code == START_TIMEOUT and "startup abandoned" in verdict.message


def test_a_lost_lease_ends_readiness(sup):
    sup.lease_path.unlink()
    assert sup.await_ready() is sup_mod._LOST
