"""Lease schema 2 (ADR-0016 §12, §14): round trips, the legacy mapping, the poison."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from rentctl.core.errors import LEASE_INVALID, DevctlError
from rentctl.core.leases import (
    KNOWN_STATES,
    POISON_WATCHDOG_PID,
    SCHEMA_SUPERVISED,
    SID_OWNER_SUPERVISOR,
    SID_OWNER_WORKLOAD,
    CleanupRecord,
    Lease,
    OwnershipIdentity,
    ProcessRef,
    RecoveryClaim,
    StopRequest,
    SupervisorRef,
    Survivor,
)

CDT = timezone(timedelta(hours=-5))
T0 = datetime(2026, 9, 28, 9, 0, tzinfo=CDT)

# The exact key set 1.0.2-and-earlier writes. A legacy lease renewed by 1.1 must
# come out in this shape, or a still-running 1.0.x watchdog would lose it (§14).
LEGACY_KEYS = {
    "project", "profile", "runner", "handle", "port", "watchdog_pid",
    "watchdog_pid_start_time", "session", "cwd", "created", "expires", "log", "spawn_cwd",
}


def legacy_lease(**over) -> Lease:
    base = dict(
        project="webapp",
        profile="default",
        runner="process",
        handle={"pid": 4242, "pid_start_time": 1784080000.12},
        port=5180,
        session="abc123",
        cwd="/path/to/exampleorg",
        created=T0,
        expires=T0 + timedelta(hours=2),
        log="/logs/webapp.log",
        watchdog_pid=4250,
        watchdog_pid_start_time=1784080001.5,
    )
    base.update(over)
    return Lease(**base)


def schema2_lease(**over) -> Lease:
    base = dict(
        project="webapp",
        profile="default",
        runner="process",
        handle={
            "pid": 124, "pid_start_time": 1759000001.0,
            "sid": 123, "sid_owner_start_time": 1759000000.0, "sid_owner": SID_OWNER_SUPERVISOR,
        },
        port=5183,
        session="sess-1",
        cwd="/work/éxample",
        spawn_cwd="/work/éxample-wt",
        created=T0,
        expires=T0 + timedelta(hours=2),
        log="/logs/webapp.log",
        schema=SCHEMA_SUPERVISED,
        generation="9f1c0000000000000000000000000000",
        state="cleanup_incomplete",
        state_since=T0 + timedelta(minutes=5),
        plan={"cmd": "npm run dev", "cwd": "/work/éxample-wt", "port_env": "PORT"},
        supervisor=SupervisorRef(pid=123, start_time=1759000000.0, registered=True),
        readiness="answered",
        stop=StopRequest(
            generation="9f1c0000000000000000000000000000",
            reason="explicit",
            reason_source="declared",
            op="down",
            requested_at=T0 + timedelta(minutes=4),
            requested_by=ProcessRef(pid=777, start_time=1759000100.0),
        ),
        cleanup=CleanupRecord(
            attempts=2,
            last_attempt=T0 + timedelta(minutes=5),
            survivors=(Survivor(pid=130, start_time=1759000002.0, name="node", status="running"),),
            identity_ambiguous=False,
            phase=None,
            retry_requested=T0 + timedelta(minutes=6),
        ),
        recovery=RecoveryClaim(pid=888, start_time=1759000200.0, since=T0 + timedelta(minutes=5)),
        error=None,
    )
    base.update(over)
    return Lease(**base)


# --- schema 2 round trips ----------------------------------------------------------

def test_schema2_dict_round_trip():
    lease = schema2_lease()
    assert Lease.from_dict(lease.to_dict()) == lease


def test_schema2_disk_round_trip(devctl_home):
    lease = schema2_lease()
    path = devctl_home.lease_file("webapp")
    lease.write(path)
    assert Lease.read(path) == lease


def test_schema2_minimal_starting_round_trip():
    lease = schema2_lease(
        handle={}, state="starting", supervisor=None, readiness=None, stop=None, cleanup=None,
        recovery=None, state_since=None, plan=None,
    )
    assert Lease.from_dict(lease.to_dict()) == lease


def test_schema2_startup_failed_error_round_trip():
    err = {"code": "START_TIMEOUT", "message": "no answer", "log_tail": ["boom"], "phase": "readiness"}
    lease = schema2_lease(state="startup_failed", error=err, cleanup=None, recovery=None, stop=None)
    assert Lease.from_dict(lease.to_dict()).error == err


def test_schema2_file_carries_schema_and_poison():
    d = schema2_lease().to_dict()
    assert d["schema"] == 2
    assert d["watchdog_pid"] == POISON_WATCHDOG_PID == "supervised"
    # Not a schema-2 field; the poison replaces the whole watchdog concept.
    assert "watchdog_pid_start_time" not in d


def test_schema2_json_is_plain_json():
    # Everything is JSON-native: no tuples-as-lists surprises, no datetimes.
    text = json.dumps(schema2_lease().to_dict())
    assert json.loads(text)["cleanup"]["survivors"][0]["name"] == "node"


def test_schema2_reads_without_poison_key():
    raw = schema2_lease().to_dict()
    raw.pop("watchdog_pid")
    assert Lease.from_dict(raw).generation == schema2_lease().generation


# --- legacy (schema 1) -------------------------------------------------------------------

def test_legacy_lease_writes_exactly_the_1_0_x_shape():
    d = legacy_lease().to_dict()
    assert set(d) == LEGACY_KEYS
    assert "schema" not in d


def test_legacy_lease_round_trips_as_legacy():
    lease = legacy_lease()
    back = Lease.from_dict(lease.to_dict())
    assert back == lease
    assert back.is_legacy and back.generation is None and back.state is None


def test_legacy_renewal_stays_legacy():
    renewed = legacy_lease().renewed(T0 + timedelta(hours=4))
    assert set(renewed.to_dict()) == LEGACY_KEYS


def test_legacy_handle_maps_to_leader_session():
    """§2/§14: the 1.0.x shell was its own session leader, so sid == its pid."""
    assert legacy_lease().ownership() == OwnershipIdentity(
        sid=4242, owner_start=1784080000.12, owner=SID_OWNER_WORKLOAD
    )
    assert legacy_lease().ownership().excludes_owner is False


def test_legacy_handle_without_pid_names_nothing():
    assert legacy_lease(handle={}).ownership() is None


def test_schema2_handle_sid_is_the_identity():
    ident = schema2_lease().ownership()
    assert ident == OwnershipIdentity(123, 1759000000.0, SID_OWNER_SUPERVISOR)
    assert ident.excludes_owner is True


def test_schema2_registered_supervisor_names_session_before_launch_write():
    """§6 step 2: if the after-launch write fails, S = supervisor.pid is on disk already."""
    lease = schema2_lease(handle={})
    assert lease.ownership() == OwnershipIdentity(123, 1759000000.0, SID_OWNER_SUPERVISOR)


def test_schema2_unregistered_start_names_nothing():
    """I1: an unregistered starting lease launched nothing, so it names no session."""
    unreg = schema2_lease(handle={}, supervisor=SupervisorRef(123, 1759000000.0, registered=False))
    assert unreg.ownership() is None
    assert schema2_lease(handle={}, supervisor=None).ownership() is None


# --- rejection -------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda d: d.update(schema=3), id="future-schema"),
        pytest.param(lambda d: d.update(state="hibernating"), id="unknown-state"),
        pytest.param(lambda d: d.update(watchdog_pid=4250), id="schema2-with-numeric-watchdog"),
        pytest.param(lambda d: d.pop("generation"), id="no-generation"),
        pytest.param(lambda d: d.update(supervisor={"pid": "x"}), id="bad-supervisor"),
        pytest.param(lambda d: d.update(stop={"generation": "g"}), id="bad-stop"),
        pytest.param(lambda d: d.update(cleanup=[1, 2]), id="bad-cleanup"),
        pytest.param(lambda d: d.update(recovery={"pid": 1}), id="bad-recovery"),
    ],
)
def test_malformed_schema2_is_lease_invalid(mutate):
    raw = schema2_lease().to_dict()
    mutate(raw)
    with pytest.raises(DevctlError) as ei:
        Lease.from_dict(raw)
    assert ei.value.code == LEASE_INVALID


def test_poison_without_schema_is_rejected_by_this_reader_too():
    raw = legacy_lease().to_dict()
    raw["watchdog_pid"] = POISON_WATCHDOG_PID
    with pytest.raises(DevctlError) as ei:
        Lease.from_dict(raw)
    assert ei.value.code == LEASE_INVALID


# --- the 1.0.1 reader fails closed ------------------------------------------------------------

@dataclass(frozen=True)
class _Lease101:
    """1.0.1's ``Lease.from_dict``, transcribed from the ``release: cut 1.0.1``
    commit (cedd2eb, core/leases.py:66-84). The real cross-version test installs
    ``rentctl==1.0.1`` in CI (plan step 11); this keeps the property checked in
    every local run."""

    project: str
    profile: str
    runner: str
    handle: dict[str, Any]
    port: int
    session: str
    cwd: str
    created: datetime
    expires: datetime
    log: str
    watchdog_pid: int | None = None
    spawn_cwd: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "_Lease101":
        try:
            return cls(
                project=d["project"],
                profile=d["profile"],
                runner=d["runner"],
                handle=d["handle"],
                port=int(d["port"]),
                session=d.get("session", "unknown"),
                cwd=d["cwd"],
                created=datetime.fromisoformat(d["created"]),
                expires=datetime.fromisoformat(d["expires"]),
                log=d["log"],
                watchdog_pid=(None if d.get("watchdog_pid") is None else int(d["watchdog_pid"])),
                spawn_cwd=d.get("spawn_cwd"),
            )
        except (KeyError, ValueError, TypeError) as e:
            raise DevctlError(LEASE_INVALID, f"malformed lease: {e}") from e


def test_1_0_1_reader_rejects_schema2_lease(devctl_home):
    path = devctl_home.lease_file("webapp")
    schema2_lease().write(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    with pytest.raises(DevctlError) as ei:
        _Lease101.from_dict(raw)
    assert ei.value.code == LEASE_INVALID


@pytest.mark.parametrize("state", sorted(KNOWN_STATES))
def test_1_0_1_reader_rejects_every_schema2_state(state):
    raw = schema2_lease(state=state).to_dict()
    with pytest.raises(DevctlError):
        _Lease101.from_dict(raw)


def test_1_0_1_reader_still_accepts_a_legacy_lease_we_write():
    """The other half of §14: a legacy lease renewed by 1.1 stays readable by 1.0.x."""
    raw = legacy_lease().renewed(T0 + timedelta(hours=3)).to_dict()
    assert _Lease101.from_dict(raw).watchdog_pid == 4250
