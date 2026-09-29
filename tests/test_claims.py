"""ADR-0017: sessions in one checkout share an environment through expiring claims.

Driven through the fake supervision seam (``fakesup.FakeSupervision``): the
real lifecycle, the real project lock and the real lease files, over a fake
process table. Each "session" is a :class:`Service` whose session id is fixed,
all over one state directory, one clock and one fake world — two agent
sessions in one checkout, as rentctl sees them.

The real-process, real-CLI version of the central scenario lives in
``test_service_supervised.py`` (``test_e2e_session_end_releases_...``).
"""

from __future__ import annotations

import io
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from fakesup import FakeSupervision
from rentctl import cli
from rentctl import mcp_server as mcp
from rentctl.core import doctor
from rentctl.core import events as ev
from rentctl.core import service as service_mod
from rentctl.core.doctor import OK, WARN
from rentctl.core.errors import PROFILE_MISMATCH
from rentctl.core.leases import Claim, Lease
from rentctl.core.service import (
    SESSION_SOURCE_STDIN,
    SESSION_SOURCE_UNKNOWN,
    Service,
    read_hook_session,
    resolve_session,
)

CDT = timezone(timedelta(hours=-5))
T0 = datetime(2026, 9, 29, 9, 0, tzinfo=CDT)
CWD = "/proj/webapp"


class Clock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw):
        self.now = self.now + timedelta(**kw)


@pytest.fixture
def clock():
    return Clock(T0)


@pytest.fixture
def world(devctl_home, clock):
    return FakeSupervision(devctl_home, clock)


@pytest.fixture
def session(devctl_home, write_registry, sample_registry_data, world, clock):
    """Factory: the service one session sees, by session id."""
    write_registry(sample_registry_data)

    def make(sid: str, **kw) -> Service:
        return Service(
            devctl_home, now_fn=clock, supervision=world, term_grace_s=0.05, kill_grace_s=0.05,
            session_id_fn=lambda: sid, port_owner_fn=lambda port: None,
            port_answering_fn=lambda port: False, **kw,
        )

    return make


def lease(paths) -> Lease | None:
    return Lease.read_if_exists(paths.lease_file_for("webapp", CWD))


def events(paths, kind: str | None = None) -> list[dict]:
    rows = ev.EventLog(paths.events_file).read()
    return [r for r in rows if kind is None or r["event"] == kind]


def session_end(svc: Service, sid: str) -> dict:
    """What the SessionEnd hook does: a declared session-end, stdin id ``sid``."""
    return svc.env_down(cwd=CWD, reason=ev.SESSION_END, session=sid)


# --- the ADR's test list ----------------------------------------------------------------

def test_a_up_then_b_up_shares_one_process_with_two_claims(session, devctl_home, world):
    a = session("A").env_up("webapp", cwd=CWD)
    assert a["ok"] is True and a["already_running"] is False
    assert [c["session"] for c in a["claims"]] == ["A"]
    assert "shared_with" not in a

    b = session("B").env_up("webapp", cwd=CWD)
    assert b["ok"] is True and b["already_running"] is True
    assert b["port"] == a["port"] and b["pid"] == a["pid"]
    assert b["shared_with"] == ["A"]
    assert {c["session"] for c in b["claims"]} == {"A", "B"}
    assert len(world.started) == 1  # one process, not two
    cur = lease(devctl_home)
    assert set(cur.claims) == {"A", "B"}
    assert cur.session == "A"  # attribution stays the starter's (§1)
    assert cur.claims["B"].via == "cli"


def test_session_end_releases_only_the_callers_claim_and_the_last_release_stops(
    session, devctl_home, world
):
    session("A").env_up("webapp", cwd=CWD)
    session("B").env_up("webapp", cwd=CWD)

    res = session_end(session("A"), "A")
    (row,) = res["downed"]
    assert row["released"] is True and row["stopped"] is False
    assert row["held_by"] == ["B"]
    world.settle()
    cur = lease(devctl_home)
    assert cur is not None and cur.state == "running"  # B's server is still up
    assert set(cur.claims) == {"B"}
    assert world.stopped == []
    assert events(devctl_home, ev.DOWN) == []  # a release is not a teardown
    (rel,) = events(devctl_home, ev.CLAIM_RELEASED)
    assert rel["released_by"] == "A" and rel["held_by"] == ["B"]

    res = session_end(session("B"), "B")
    (row,) = res["downed"]
    assert row["pending"] is True and row["released_by"] == "B"  # hook: wait 0 (R0)
    world.settle()
    assert lease(devctl_home) is None
    assert len(world.stopped) == 1
    (down,) = events(devctl_home, ev.DOWN)
    assert down["reason"] == ev.SESSION_END and down["layer"] == 2
    assert down["killed"] is True and down["released_by"] == "B"


def test_a_session_that_never_leased_anything_ends_as_a_no_op(session, devctl_home, world):
    session("A").env_up("webapp", cwd=CWD)
    before = events(devctl_home)
    snapshot = lease(devctl_home)

    res = session_end(session("C"), "C")
    (row,) = res["downed"]
    assert row["released"] is False and row["stopped"] is False and row["held_by"] == ["A"]
    world.settle()
    assert lease(devctl_home) == snapshot  # nothing written
    assert events(devctl_home) == before  # nothing logged
    assert world.stopped == []


def test_crashed_claim_keeps_the_lease_until_it_expires(session, devctl_home, world, clock):
    session("A").env_up("webapp", cwd=CWD, lease_minutes=120)  # A then crashes: no SessionEnd
    session("B").env_up("webapp", cwd=CWD, lease_minutes=30)

    (row,) = session_end(session("B"), "B")["downed"]
    assert row["held_by"] == ["A"] and row["stopped"] is False
    cur = lease(devctl_home)
    assert cur.expires == T0 + timedelta(minutes=120)  # A's claim is the lease's expiry

    clock.advance(minutes=119)
    world.settle()
    assert lease(devctl_home) is not None and world.stopped == []

    clock.advance(minutes=2)
    world.settle()
    assert lease(devctl_home) is None
    (down,) = events(devctl_home, ev.DOWN)
    assert down["reason"] == ev.EXPIRY and down["lapsed_claims"] == ["A"]


def test_b_up_renews_only_bs_claim(session, devctl_home, clock):
    session("A").env_up("webapp", cwd=CWD, lease_minutes=120)
    a_expires = lease(devctl_home).claims["A"].expires
    clock.advance(minutes=10)
    session("B").env_up("webapp", cwd=CWD, lease_minutes=30)
    cur = lease(devctl_home)
    assert cur.claims["A"].expires == a_expires  # untouched
    assert cur.claims["B"].expires == T0 + timedelta(minutes=40)
    assert cur.expires == max(c.expires for c in cur.claims.values()) == a_expires

    clock.advance(minutes=5)
    session("B").env_up("webapp", cwd=CWD, lease_minutes=300)
    cur = lease(devctl_home)
    assert cur.claims["A"].expires == a_expires
    assert cur.claims["B"].since == T0 + timedelta(minutes=10)  # a renewal, not a new claim
    assert cur.expires == cur.claims["B"].expires == T0 + timedelta(minutes=315)


def test_different_profile_is_refused_naming_the_holders_and_adds_no_claim(session, devctl_home):
    session("A").env_up("webapp", cwd=CWD)
    session("B").env_up("webapp", cwd=CWD)
    res = session("C").env_up("webapp", cwd=CWD, profile="api-only")
    assert res["ok"] is False and res["error"] == PROFILE_MISMATCH
    assert res["held_by"] == ["A", "B"]
    assert "A, B" in res["message"]
    assert set(lease(devctl_home).claims) == {"A", "B"}


def test_mcp_env_down_releases_by_default_and_force_stops_for_everyone(
    session, devctl_home, world, monkeypatch
):
    monkeypatch.setenv("DEVCTL_PROJECT_DIR", CWD)
    session("A").env_up("webapp", cwd=CWD)
    b = session("B", via="mcp")
    monkeypatch.setattr(mcp, "_service", b)
    mcp.env_up("webapp")
    assert lease(devctl_home).claims["B"].via == "mcp"

    res = mcp.env_down("webapp")  # force omitted: a release
    assert res["ok"] is True and res["stopped"] is False and res["held_by"] == ["A"]
    assert lease(devctl_home) is not None

    mcp.env_up("webapp")
    res = mcp.env_down("webapp", force=True)
    assert res["ok"] is True and res["stopped"] is True
    assert res["overrode_claims"] == ["A"]
    assert lease(devctl_home) is None
    (down,) = events(devctl_home, ev.DOWN)
    assert down["overrode_claims"] == ["A"] and down["reason"] == ev.EXPLICIT


def test_mcp_env_down_describes_release_versus_force():
    """The docstring is the description an agent reads before it calls the tool."""
    (tool,) = [t for t in mcp.mcp._tool_manager.list_tools() if t.name == "env_down"]
    text = tool.description.lower()
    assert "release" in text and "force" in text and "held_by" in text
    assert tool.parameters["properties"]["force"]["default"] is False


def test_cli_down_project_is_a_deliberate_stop_naming_overridden_claims(
    session, devctl_home, world, capsys
):
    session("A").env_up("webapp", cwd=CWD)
    session("B").env_up("webapp", cwd=CWD)
    rc = cli.main(["down", "webapp", "--cwd", CWD], service=session("human"))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["stopped"] is True
    assert out["overrode_claims"] == ["A", "B"]
    assert lease(devctl_home) is None
    (req,) = events(devctl_home, ev.STOP_REQUESTED)
    assert req["overrode_claims"] == ["A", "B"]
    (down,) = events(devctl_home, ev.DOWN)
    assert down["overrode_claims"] == ["A", "B"]


def test_cli_down_all_without_session_end_is_a_deliberate_stop(session, devctl_home, world, capsys):
    session("A").env_up("webapp", cwd=CWD)
    rc = cli.main(["down", "--all", "--cwd", CWD], service=session("A"))
    (row,) = json.loads(capsys.readouterr().out)["downed"]
    assert rc == 0 and row["stopped"] is True
    assert "overrode_claims" not in row  # only the caller's own claim was live


def test_unknown_release_never_touches_an_attributed_claim_and_vice_versa(
    session, devctl_home, world
):
    session("A").env_up("webapp", cwd=CWD)
    (row,) = session_end(session("unknown"), "unknown")["downed"]
    assert row["released"] is False and row["held_by"] == ["A"]

    session("unknown").env_up("webapp", cwd=CWD)  # a human in a terminal
    (row,) = session_end(session("A"), "A")["downed"]
    assert row["released"] is True and row["held_by"] == ["unknown"]
    world.settle()
    assert set(lease(devctl_home).claims) == {"unknown"}
    assert world.stopped == []


def _pre_upgrade_lease(paths, clock, world, sid: str) -> None:
    """A running schema-2 lease exactly as 1.1.0 wrote it: no ``claims`` field."""
    from rentctl.core.lifecycle import State

    Service(
        paths, now_fn=clock, supervision=world, term_grace_s=0.05, kill_grace_s=0.05,
        session_id_fn=lambda: sid, port_owner_fn=lambda p: None, port_answering_fn=lambda p: False,
    ).env_up("webapp", cwd=CWD)
    path = paths.lease_file_for("webapp", CWD)
    raw = json.loads(path.read_text())
    del raw["claims"]
    path.write_text(json.dumps(raw))
    cur = lease(paths)
    assert cur.claims is None and cur.state == State.RUNNING.value


def test_pre_upgrade_lease_is_released_by_its_sessions_session_end(session, devctl_home, world, clock):
    _pre_upgrade_lease(devctl_home, clock, world, "A")
    (row,) = session_end(session("A"), "A")["downed"]
    assert row["pending"] is True and row["released_by"] == "A"
    world.settle()
    assert lease(devctl_home) is None


def test_pre_upgrade_unknown_lease_is_not_released_by_an_identified_session_end(
    session, devctl_home, world, clock
):
    _pre_upgrade_lease(devctl_home, clock, world, "unknown")
    (row,) = session_end(session("A"), "A")["downed"]
    assert row["released"] is False and row["held_by"] == ["unknown"]
    world.settle()
    assert lease(devctl_home) is not None and world.stopped == []


def test_release_then_up_waits_for_the_stop_and_starts_fresh(session, devctl_home, world):
    """The race, release first: the stop was decided under L, so B's `up` never
    attaches to the dying process — it waits and starts a new generation."""
    session("A").env_up("webapp", cwd=CWD)
    old = lease(devctl_home)
    session_end(session("A"), "A")  # last claim: stop requested, not yet done
    assert lease(devctl_home).stop is not None

    res = session("B").env_up("webapp", cwd=CWD)
    assert res["ok"] is True and res["already_running"] is False
    new = lease(devctl_home)
    assert new.generation != old.generation
    assert set(new.claims) == {"B"}
    assert len(world.started) == 2 and world.stopped == [world.started[0]]


def test_up_then_release_keeps_the_environment(session, devctl_home, world):
    """The race, up first: B's claim is on the lease before A's release reads
    it under L, so the release is not the last one and nothing stops."""
    session("A").env_up("webapp", cwd=CWD)
    session("B").env_up("webapp", cwd=CWD)
    (row,) = session_end(session("A"), "A")["downed"]
    assert row["stopped"] is False and row["held_by"] == ["B"]
    assert lease(devctl_home).stop is None


def test_concurrent_up_and_last_release_never_leave_a_claim_on_a_dying_process(
    devctl_home, write_registry, sample_registry_data, clock
):
    """Both calls at once, many times, from threads contending for the real
    flock. Whatever the order, a live claim never sits on a stop-requested
    lease, and B ends up holding a running environment."""
    write_registry(sample_registry_data)
    orders: set[bool] = set()
    for i in range(20):
        world = FakeSupervision(devctl_home, clock)
        world_lock = threading.RLock()

        class Locked:
            """The fake world is single-threaded: serialize its own steps."""

            def __getattr__(self, name):
                attr = getattr(world, name)
                if not callable(attr):
                    return attr

                def call(*a, **kw):
                    with world_lock:
                        return attr(*a, **kw)

                return call

        def svc(sid):
            return Service(
                devctl_home, now_fn=clock, supervision=Locked(), term_grace_s=0.05,
                kill_grace_s=0.05, session_id_fn=lambda: sid, port_owner_fn=lambda p: None,
                port_answering_fn=lambda p: False,
            )

        svc("A").env_up("webapp", cwd=CWD)
        results: dict[str, dict] = {}
        gate = threading.Barrier(2)
        # Alternate who is let go first, so both orders are actually exercised.
        delay = {"A": 0.0 if i % 2 else 0.03, "B": 0.03 if i % 2 else 0.0}

        def run(name, call):
            gate.wait(5)
            time.sleep(delay[name])
            results[name] = call()

        ta = threading.Thread(target=run, args=("A", lambda: session_end(svc("A"), "A")))
        tb = threading.Thread(target=run, args=("B", lambda: svc("B").env_up("webapp", cwd=CWD)))
        ta.start(), tb.start()
        ta.join(30), tb.join(30)
        orders.add(results["B"]["already_running"])
        assert results["B"]["ok"] is True, results["B"]
        with world_lock:
            world.settle()
        cur = lease(devctl_home)
        assert cur is not None and cur.stop is None and cur.state == "running"
        assert "B" in cur.live_claims(clock())
        if results["B"]["already_running"]:
            # B claimed first: A's release was not the last, nothing stopped.
            assert world.stopped == [] and set(cur.claims) == {"B"}
        else:
            # A's release stopped it first: B started a fresh generation.
            assert world.stopped == [world.started[0]] and len(world.started) == 2
        svc("B").env_down("webapp", cwd=CWD)
        with world_lock:
            world.settle()
        assert lease(devctl_home) is None
        devctl_home.events_file.unlink(missing_ok=True)
    assert orders == {True, False}, "only one interleaving was exercised"


def test_clear_shape_lapses_by_expiry_and_is_never_killed_under_a_live_claim(
    session, devctl_home, world, clock
):
    """`/clear`: SessionEnd for the old id, then an `up` under the old env id
    (a surviving MCP server), then the new id's SessionEnd."""
    session("old").env_up("webapp", cwd=CWD, lease_minutes=60)
    session_end(session("old"), "old")
    world.settle()
    assert lease(devctl_home) is None

    session("old").env_up("webapp", cwd=CWD, lease_minutes=60)  # stale MCP id
    (row,) = session_end(session("new"), "new")["downed"]
    assert row["released"] is False and row["held_by"] == ["old"]
    world.settle()
    assert lease(devctl_home) is not None
    assert len(world.stopped) == 1  # only the first generation

    clock.advance(minutes=61)
    world.settle()
    assert lease(devctl_home) is None
    assert events(devctl_home, ev.DOWN)[-1]["reason"] == ev.EXPIRY


def test_subagent_shares_its_parents_claim(devctl_home, write_registry, sample_registry_data,
                                           world, clock, monkeypatch):
    """A subagent inherits the parent's id (plus CLAUDE_CODE_CHILD_SESSION=1):
    one claim, not two, and the parent's SessionEnd is the last release."""
    write_registry(sample_registry_data)
    for name in service_mod.SESSION_ID_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent")
    svc = Service(devctl_home, now_fn=clock, supervision=world, term_grace_s=0.05,
                  kill_grace_s=0.05, port_owner_fn=lambda p: None, port_answering_fn=lambda p: False)
    svc.env_up("webapp", cwd=CWD)
    monkeypatch.setenv("CLAUDE_CODE_CHILD_SESSION", "1")
    res = svc.env_up("webapp", cwd=CWD)
    assert "shared_with" not in res
    assert list(lease(devctl_home).claims) == ["parent"]


# --- visibility (§9) ------------------------------------------------------------------------

def test_env_ls_rows_carry_claims(session, devctl_home):
    session("A").env_up("webapp", cwd=CWD, lease_minutes=60)
    session("B", via="mcp").env_up("webapp", cwd=CWD, lease_minutes=30)
    (row,) = session("A").env_ls()["environments"]
    assert row["session"] == "A"
    assert [(c["session"], c["via"]) for c in row["claims"]] == [("B", "mcp"), ("A", "cli")]
    assert row["claims"][1]["expires"] == (T0 + timedelta(minutes=60)).isoformat()


def test_renewal_up_event_names_the_claimant(session, devctl_home):
    session("A").env_up("webapp", cwd=CWD)
    session("B").env_up("webapp", cwd=CWD)
    ups = events(devctl_home, ev.UP)
    assert ups[-1]["already_running"] is True and ups[-1]["claimed_by"] == "B"
    assert ups[-1]["session"] == "A"


def test_claim_released_is_not_counted_as_a_teardown(session, devctl_home):
    session("A").env_up("webapp", cwd=CWD)
    session("B").env_up("webapp", cwd=CWD)
    session_end(session("A"), "A")
    summary = ev.summarize(events(devctl_home))
    assert summary["layers"] == {} and summary["kills"] == 0
    assert summary["counts"][ev.CLAIM_RELEASED] == 1


def test_doctor_names_the_identity_source():
    c = doctor.check_session_identity({"CLAUDE_CODE_SESSION_ID": "x", "GEMINI_SESSION_ID": "y"})
    assert c.status == OK and c.source == "CLAUDE_CODE_SESSION_ID"
    c = doctor.check_session_identity({"DEVCTL_SESSION_ID": "d", "CLAUDE_CODE_SESSION_ID": "x"})
    assert c.source == "DEVCTL_SESSION_ID"
    c = doctor.check_session_identity({})
    assert c.status == OK and c.source == "unknown"
    c = doctor.check_session_identity({"CLAUDE_PROJECT_DIR": "/p"})
    assert c.status == WARN and c.source == "unknown"


def test_diagnose_includes_the_identity_check(devctl_home, write_registry, monkeypatch):
    write_registry({"projects": {}})
    monkeypatch.setattr(doctor.shutil, "which", lambda _c: None)
    report = doctor.diagnose(devctl_home, runner=lambda argv: (0, '{"ok": true}', ""))
    assert "session-identity" in [c.name for c in report.checks]


# --- the lease record (§1, §5) -------------------------------------------------------------

def _legacy(**over) -> Lease:
    base = dict(
        project="webapp", profile="default", runner="process",
        handle={"pid": 4242, "pid_start_time": 1.5}, port=5180, session="A", cwd=CWD,
        created=T0, expires=T0 + timedelta(hours=2), log="/dev/null",
    )
    base.update(over)
    return Lease(**base)


def test_pre_upgrade_lease_reads_as_one_claim_for_its_session():
    raw = _legacy().to_dict()
    assert "claims" not in raw  # written exactly as 1.0.x did
    back = Lease.from_dict(raw)
    assert back.claims is None
    assert back.claim_map() == {"A": Claim(expires=T0 + timedelta(hours=2))}
    assert list(Lease.from_dict(_legacy(session="unknown").to_dict()).claim_map()) == ["unknown"]


def test_claims_round_trip_derive_expiry_and_prune_lapsed_ones():
    lease0 = _legacy()
    a = lease0.claimed("B", expires=T0 + timedelta(hours=3), now=T0, via="mcp")
    assert set(a.claims) == {"A", "B"} and a.expires == T0 + timedelta(hours=3)
    assert Lease.from_dict(json.loads(json.dumps(a.to_dict()))) == a
    later = a.claimed("C", expires=T0 + timedelta(hours=4), now=T0 + timedelta(hours=2, minutes=1))
    assert set(later.claims) == {"B", "C"}  # A lapsed at 2h and was pruned on write
    gone = later.released("B", T0 + timedelta(hours=2, minutes=1)).released(
        "C", T0 + timedelta(hours=2, minutes=1)
    )
    assert gone.claims == {} and gone.expires == T0 + timedelta(hours=2, minutes=1)


def test_malformed_claims_make_the_lease_invalid():
    from rentctl.core.errors import DevctlError

    raw = _legacy().to_dict()
    raw["claims"] = {"A": "tomorrow"}
    with pytest.raises(DevctlError):
        Lease.from_dict(raw)
    raw["claims"] = ["A"]
    with pytest.raises(DevctlError):
        Lease.from_dict(raw)


# --- the resolver (§2) --------------------------------------------------------------------

@pytest.fixture
def no_session_env(monkeypatch):
    for name in service_mod.SESSION_ID_ENVS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _pipe(data: bytes, close: bool = True):
    r, w = os.pipe()
    if data:
        os.write(w, data)
    if close:
        os.close(w)
        w = None
    return os.fdopen(r, "rb"), w


def test_hook_stdin_valid_json():
    stream, _ = _pipe(b'{"session_id": "abc", "reason": "clear"}')
    with stream:
        assert read_hook_session(stream) == "abc"


@pytest.mark.parametrize("payload", [
    b"", b"not json", b"[1, 2]", b'{"session_id": 7}', b'{"session_id": ""}', b'{"other": "x"}',
    b'{"session_id": "a\\u0000b"}', b"\xff\xfe",
])
def test_hook_stdin_malformed_input_returns_none(payload):
    stream, _ = _pipe(payload)
    with stream:
        assert read_hook_session(stream) is None


def test_hook_stdin_oversized_payload_returns_none():
    big = json.dumps({"session_id": "abc", "pad": "x" * (70 * 1024)}).encode()
    r, w = os.pipe()
    writer = threading.Thread(target=lambda: (os.write(w, big), os.close(w)))
    writer.start()
    with os.fdopen(r, "rb") as stream:
        t0 = time.monotonic()
        assert read_hook_session(stream) is None
        assert time.monotonic() - t0 < 1.0
    writer.join(5)


def test_hook_stdin_that_never_closes_returns_inside_the_budget():
    stream, w = _pipe(b'{"session_id": "abc"}', close=False)
    try:
        t0 = time.monotonic()
        assert read_hook_session(stream) is None
        assert time.monotonic() - t0 < 1.0
    finally:
        os.close(w)
        stream.close()


def test_hook_stdin_tty_is_never_read():
    class Tty(io.StringIO):
        def isatty(self):
            return True

        def fileno(self):  # pragma: no cover - reaching this is the failure
            raise AssertionError("a terminal must not be read")

    assert read_hook_session(Tty('{"session_id": "abc"}')) is None


def test_hook_stdin_unusable_stream_returns_none():
    assert read_hook_session(io.StringIO('{"session_id": "abc"}')) is None  # no fileno


def test_stdin_wins_over_env_for_a_hook(no_session_env):
    no_session_env.setenv("CLAUDE_CODE_SESSION_ID", "from-env")
    stream, _ = _pipe(b'{"session_id": "from-stdin"}')
    with stream:
        assert resolve_session(from_hook=True, stream=stream) == ("from-stdin", SESSION_SOURCE_STDIN)


def test_stdin_is_ignored_off_the_hook_path(no_session_env):
    no_session_env.setenv("CLAUDE_CODE_SESSION_ID", "from-env")
    stream, _ = _pipe(b'{"session_id": "from-stdin"}')
    with stream:
        assert resolve_session(stream=stream) == ("from-env", "CLAUDE_CODE_SESSION_ID")


def test_malformed_stdin_falls_through_to_env(no_session_env):
    no_session_env.setenv("GEMINI_SESSION_ID", "gem")
    stream, _ = _pipe(b"{nope")
    with stream:
        assert resolve_session(from_hook=True, stream=stream) == ("gem", "GEMINI_SESSION_ID")


def test_env_order_is_devctl_then_claude_then_gemini_then_unknown(no_session_env):
    assert resolve_session() == ("unknown", SESSION_SOURCE_UNKNOWN)
    no_session_env.setenv("GEMINI_SESSION_ID", "g")
    assert resolve_session() == ("g", "GEMINI_SESSION_ID")
    no_session_env.setenv("CLAUDE_CODE_SESSION_ID", "c")
    assert resolve_session() == ("c", "CLAUDE_CODE_SESSION_ID")
    no_session_env.setenv("DEVCTL_SESSION_ID", "d")
    assert resolve_session() == ("d", "DEVCTL_SESSION_ID")


def test_a_runtime_appended_to_the_env_list_is_honoured(no_session_env):
    no_session_env.setattr(service_mod, "SESSION_ID_ENVS", (*service_mod.SESSION_ID_ENVS, "NEW_RT_ID"))
    no_session_env.setenv("NEW_RT_ID", "n")
    assert resolve_session() == ("n", "NEW_RT_ID")


def test_cli_session_end_reads_stdin_and_other_downs_do_not(monkeypatch, capsys):
    """The CLI hands the hook's stdin id to the service for session-end only."""
    from test_cli import RecordingService

    monkeypatch.setattr(cli, "read_hook_session", lambda: "hook-id")
    rec = RecordingService()
    cli.main(["down", "--all", "--cwd", "/p", "--reason", "session-end"], service=rec)
    cli.main(["down", "webapp", "--cwd", "/p"], service=rec)
    cli.main(["down", "--all", "--cwd", "/p"], service=rec)
    assert rec.sessions == ["hook-id", None, None]
