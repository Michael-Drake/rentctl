# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Drake

"""Lease files — annotations claiming ownership and expiry (spec §5.2).

A lease is a *claim*, not ground truth: it can be stale, corrupt, or point at a
dead or recycled PID. The OS process table is what's real; the reconciler
(``reconcile.py``) checks every lease against it before acting. This module is
just the typed serialization + safe read/write of the claim.

Writes are atomic (temp file + ``os.replace``) so a crash mid-write can never
leave a half-written lease that later reads as corrupt.

**Two schemas live side by side (ADR-0016 §12, §14).** A file with no
``schema`` key is a *legacy* (1.0.x) lease and is read and written in exactly
the shape 1.0.x wrote — ``up`` renews a legacy lease in place, in legacy format,
so a 1.0.x watchdog still babysitting it keeps working. A ``"schema": 2`` file
carries the supervisor lifecycle: generation, state, supervisor identity, the
stop request, cleanup record and recovery claim. The *meaning* of those fields
(which transitions are legal, who may write them) lives in ``lifecycle.py``;
this module is still only the typed serialization.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from .errors import LEASE_INVALID, DevctlError
from .models import SID_OWNER_SUPERVISOR, SID_OWNER_WORKLOAD, ProcessHandle, Survivor, WorkloadIdentity

SCHEMA_LEGACY = 1       # never written as a key: absence of ``schema`` *is* legacy
SCHEMA_SUPERVISED = 2

# Every value a schema-2 ``state`` may hold (ADR-0016 §7). ``lifecycle.State``
# is the typed view; it lives there because the rules do. A test pins the two
# together. An unknown state is refused at read time: a reader that guessed
# what a newer state meant could signal something it should not.
KNOWN_STATES = frozenset(
    {
        "starting",
        "running",
        "stopping",
        "stopped",
        "cleanup_incomplete",
        "unsupervised",
        "exited",
        "startup_failed",
        "abandoned",
    }
)

# The deliberate poison (ADR-0016 §14). Deployed 1.0.x code ignores unknown keys,
# so ``"schema": 2`` alone cannot stop an old watchdog or a long-lived 1.0.x
# ``rent-mcp`` from acting on a new lease. A string here can: 1.0.1's
# ``Lease.from_dict`` runs ``int(d["watchdog_pid"])`` (leases.py:81 at the
# ``release: cut 1.0.1`` commit, cedd2eb), which raises ``ValueError`` →
# ``DevctlError(LEASE_INVALID)``, and every 1.0.1 reader of an invalid lease
# leaves the file in place:
#   * watchdog.py:76      ``return CORRUPT`` — the watchdog exits, touching nothing;
#   * service.py:768      ``_reconcile_all``: ``continue  # corrupt/vanished — leave it``;
#   * service.py:850      ``_read_lease_quiet``: ``return None  # corrupt lease — skip``
#                         (the ``ls``/``sweep``/``_squatters`` path);
#   * ``env_up``/``env_down`` surface ``LEASE_INVALID`` as an error envelope.
# So every old actor fails closed on a schema-2 file. Do not "tidy" this into an
# int or remove it: the cross-version CI test (ADR-0016 test plan, Migration)
# exists to catch exactly that.
POISON_WATCHDOG_PID = "supervised"

# ``handle.sid_owner`` values (ADR-0016 §2) are ``models.SID_OWNER_SUPERVISOR``
# and ``models.SID_OWNER_WORKLOAD``, re-exported from here for lease readers.


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _dt(value: Any) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


@dataclass(frozen=True)
class ProcessRef:
    """A process named by PID *and* start time — never by PID alone (role doc §5)."""

    pid: int
    start_time: float

    def to_dict(self) -> dict[str, Any]:
        return {"pid": self.pid, "start_time": self.start_time}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ProcessRef":
        return cls(pid=int(d["pid"]), start_time=float(d["start_time"]))


@dataclass(frozen=True)
class SupervisorRef:
    """The lease's supervisor (§6). ``registered`` is false between the CLI's
    spawn write and the supervisor's own registration write; until it is true,
    I1 guarantees nothing has been launched."""

    pid: int
    start_time: float
    registered: bool = False
    # The guarantee this supervisor established at registration (ADR-0016 §5,
    # plan step 9): ``procutil.SUPERVISION_SESSION`` or
    # ``SUPERVISION_SUBREAPER``. ``None`` until it registers, and on a record
    # written before the field existed.
    supervision: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "pid": self.pid, "start_time": self.start_time, "registered": self.registered,
        }
        if self.supervision is not None:
            out["supervision"] = self.supervision
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SupervisorRef":
        supervision = d.get("supervision")
        return cls(
            pid=int(d["pid"]), start_time=float(d["start_time"]), registered=bool(d["registered"]),
            supervision=None if supervision is None else str(supervision),
        )

    @property
    def ref(self) -> ProcessRef:
        return ProcessRef(self.pid, self.start_time)


@dataclass(frozen=True)
class StopRequest:
    """An on-disk stop request (§8). Honoured only while ``generation`` matches
    the lease's; the first one written is the teardown's reason.

    ``released_by`` and ``overrode_claims`` are ADR-0017 §9's attribution: the
    session whose release of the last claim caused this stop, or the other
    sessions' live claims a deliberate stop overrode. They ride on the request
    so the ``down`` event, written later by whoever verifies the stop, can say
    them. Both are left out of the file when unset.
    """

    generation: str
    reason: str
    reason_source: str
    op: str
    requested_at: datetime
    requested_by: ProcessRef
    released_by: str | None = None
    overrode_claims: tuple[str, ...] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "generation": self.generation,
            "reason": self.reason,
            "reason_source": self.reason_source,
            "op": self.op,
            "requested_at": self.requested_at.isoformat(),
            "requested_by": self.requested_by.to_dict(),
        }
        if self.released_by is not None:
            out["released_by"] = self.released_by
        if self.overrode_claims is not None:
            out["overrode_claims"] = list(self.overrode_claims)
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "StopRequest":
        released = d.get("released_by")
        overrode = d.get("overrode_claims")
        return cls(
            generation=str(d["generation"]),
            reason=str(d["reason"]),
            reason_source=str(d["reason_source"]),
            op=str(d["op"]),
            requested_at=datetime.fromisoformat(d["requested_at"]),
            requested_by=ProcessRef.from_dict(d["requested_by"]),
            released_by=None if released is None else str(released),
            overrode_claims=None if overrode is None else tuple(str(x) for x in overrode),
        )


# The claim key for a caller rentctl could not identify (ADR-0017 §5): one
# shared, anonymous bucket. The same string as ``events.UNATTRIBUTED``.
UNKNOWN_SESSION = "unknown"


@dataclass(frozen=True)
class Claim:
    """One session's entitlement to a lease's environment (ADR-0017 §1).

    ``since`` and ``via`` are ``None`` only on the claim read from a
    pre-upgrade lease, which recorded neither.
    """

    expires: datetime
    since: datetime | None = None
    via: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"since": _iso(self.since), "expires": self.expires.isoformat(), "via": self.via}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Claim":
        via = d.get("via")
        return cls(
            expires=datetime.fromisoformat(d["expires"]),
            since=_dt(d.get("since")),
            via=None if via is None else str(via),
        )


def _claims_from(raw: Any) -> "dict[str, Claim] | None":
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"claims is not an object: {raw!r}")
    return {str(k): Claim.from_dict(v) for k, v in raw.items()}


# ``Survivor`` is ``models.Survivor`` (re-exported above): the row a stop
# returns and the row a lease records are one fact, so they are one type.


@dataclass(frozen=True)
class CleanupRecord:
    """Why a lease is ``cleanup_incomplete`` and what is left (§7, §12).

    ``phase`` is ``"startup"`` when the failed stop was a startup teardown
    (§7's ``cleanup_incomplete{phase: startup}``). ``retry_requested`` is the
    time a later stop request asked for an immediate retry (§8: a request on
    ``cleanup_incomplete`` "triggers an immediate retry") — the §12 sketch has
    no field for that, and a pure transition needs somewhere to say it.
    """

    attempts: int = 0
    last_attempt: datetime | None = None
    survivors: tuple[Survivor, ...] = ()
    identity_ambiguous: bool = False
    phase: str | None = None
    retry_requested: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "last_attempt": _iso(self.last_attempt),
            "survivors": [s.to_dict() for s in self.survivors],
            "identity_ambiguous": self.identity_ambiguous,
            "phase": self.phase,
            "retry_requested": _iso(self.retry_requested),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CleanupRecord":
        return cls(
            attempts=int(d.get("attempts", 0)),
            last_attempt=_dt(d.get("last_attempt")),
            survivors=tuple(Survivor.from_dict(s) for s in d.get("survivors") or ()),
            identity_ambiguous=bool(d.get("identity_ambiguous", False)),
            phase=d.get("phase"),
            retry_requested=_dt(d.get("retry_requested")),
        )


@dataclass(frozen=True)
class RecoveryClaim:
    """A CLI's claim to run a recovery stop without holding L (§10)."""

    pid: int
    start_time: float
    since: datetime

    def to_dict(self) -> dict[str, Any]:
        return {"pid": self.pid, "start_time": self.start_time, "since": self.since.isoformat()}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RecoveryClaim":
        return cls(
            pid=int(d["pid"]),
            start_time=float(d["start_time"]),
            since=datetime.fromisoformat(d["since"]),
        )

    @property
    def ref(self) -> ProcessRef:
        return ProcessRef(self.pid, self.start_time)


# The lease-side name for ``models.WorkloadIdentity``. Two lanes each wrote one
# (plan steps 1 and 3); they carried the same three facts, so there is now one
# class and this alias keeps the ADR §12 name readable at lease call sites.
OwnershipIdentity = WorkloadIdentity


def _opt(parse, value: Any):
    return None if value is None else parse(value)


@dataclass(frozen=True)
class Lease:
    """A claim over one running environment (spec §5.2; schema 2 per ADR-0016 §12).

    Every schema-2 field defaults to "absent", so a lease built the 1.0.x way is
    a legacy lease and serializes byte-for-byte as it did before.
    """

    project: str
    profile: str
    runner: str
    handle: dict[str, Any]        # runner-specific; process → {pid, pid_start_time}
    port: int
    session: str
    cwd: str
    created: datetime
    expires: datetime
    log: str
    watchdog_pid: int | None = None
    # Where the process was actually spawned (ADR-0010). Differs from ``cwd``
    # only when the caller was a sibling git worktree and the registry's
    # directory was re-rooted onto it. ``None`` on leases written before
    # re-rooting existed — absent is not "same as cwd", it is "unrecorded".
    spawn_cwd: str | None = None
    # The watchdog's ``create_time()``, captured at spawn (WI-0069) — the same
    # PID-recycle guard the server's handle carries, so teardown can prove the
    # pid still names our watchdog before signalling it. ``None`` on leases
    # written by 1.0.1: absent means "unverifiable", and an unverifiable pid is
    # never signalled.
    watchdog_pid_start_time: float | None = None

    # --- schema 2 (ADR-0016 §12) -------------------------------------------
    # ``schema`` is ``SCHEMA_LEGACY`` for a file with no ``schema`` key. None of
    # the fields below is read from or written to a legacy file.
    schema: int = SCHEMA_LEGACY
    # uuid4 hex minted by the CLI at ``starting``; never reused. The stop
    # request, the supervisor's argv and every waiter are checked against it.
    generation: str | None = None
    state: str | None = None
    state_since: datetime | None = None
    # What the supervisor runs — ``{cmd, cwd, port_env}`` — so it never
    # re-reads the registry (no TOCTOU between approval and launch).
    plan: dict[str, Any] | None = None
    supervisor: SupervisorRef | None = None
    readiness: str | None = None
    stop: StopRequest | None = None
    cleanup: CleanupRecord | None = None
    recovery: RecoveryClaim | None = None
    # ``startup_failed`` only: ``{code, message, log_tail, phase}`` for the waiting ``up``.
    error: dict[str, Any] | None = None

    # --- claims (ADR-0017 §1) ------------------------------------------------
    # session id → Claim. ``None`` is a lease written before claims existed; it
    # reads as one claim ``{session: {expires}}`` (§5) through
    # :meth:`claim_map`. Once set, ``expires`` above is derived from it on every
    # claim write: the latest unexpired claim's expiry. Everything that reads
    # ``expires`` (the supervisor, reconcile, a 1.0.x watchdog) is unchanged.
    claims: dict[str, Claim] | None = None

    @property
    def is_legacy(self) -> bool:
        return self.schema == SCHEMA_LEGACY

    # --- serialization ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        if self.is_legacy:
            return self._legacy_dict()
        return {
            "schema": SCHEMA_SUPERVISED,
            "generation": self.generation,
            "state": self.state,
            "state_since": _iso(self.state_since),
            "project": self.project,
            "profile": self.profile,
            "runner": self.runner,
            "port": self.port,
            "session": self.session,
            "cwd": self.cwd,
            "spawn_cwd": self.spawn_cwd,
            "created": self.created.isoformat(),
            "expires": self.expires.isoformat(),
            "log": self.log,
            "plan": self.plan,
            "supervisor": None if self.supervisor is None else self.supervisor.to_dict(),
            "handle": self.handle,
            "readiness": self.readiness,
            "stop": None if self.stop is None else self.stop.to_dict(),
            "cleanup": None if self.cleanup is None else self.cleanup.to_dict(),
            "recovery": None if self.recovery is None else self.recovery.to_dict(),
            "error": self.error,
            "watchdog_pid": POISON_WATCHDOG_PID,
            **self._claims_dict(),
        }

    def _claims_dict(self) -> dict[str, Any]:
        if self.claims is None:
            return {}
        return {"claims": {k: c.to_dict() for k, c in self.claims.items()}}

    def _legacy_dict(self) -> dict[str, Any]:
        # Exactly the 1.0.x key set: a legacy lease renewed by 1.1 must still
        # parse for the 1.0.x watchdog that may be babysitting it (§14).
        return {
            "project": self.project,
            "profile": self.profile,
            "runner": self.runner,
            "handle": self.handle,
            "port": self.port,
            "watchdog_pid": self.watchdog_pid,
            "watchdog_pid_start_time": self.watchdog_pid_start_time,
            "session": self.session,
            "cwd": self.cwd,
            "created": self.created.isoformat(),
            "expires": self.expires.isoformat(),
            "log": self.log,
            "spawn_cwd": self.spawn_cwd,
            # Absent until a claim is written (ADR-0017). A 1.0.x reader ignores
            # unknown keys, and ``expires`` above is still the one it acts on.
            **self._claims_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Lease":
        schema = d.get("schema")
        if schema is None:
            return cls._from_legacy_dict(d)
        if schema != SCHEMA_SUPERVISED:
            # A newer rentctl's file. Reading it with guessed semantics could
            # stop something this version does not understand; fail closed.
            raise DevctlError(LEASE_INVALID, f"unsupported lease schema {schema!r}")
        try:
            if d.get("watchdog_pid") not in (None, POISON_WATCHDOG_PID):
                # A schema-2 file with a numeric watchdog pid is self-contradictory:
                # 1.0.x would parse it and act on it. Refuse rather than guess.
                raise ValueError(f"schema-2 lease with watchdog_pid {d['watchdog_pid']!r}")
            if d["state"] not in KNOWN_STATES:
                raise ValueError(f"unknown lease state {d['state']!r}")
            return cls(
                project=d["project"],
                profile=d["profile"],
                runner=d["runner"],
                handle=dict(d.get("handle") or {}),
                port=int(d["port"]),
                session=d.get("session", "unknown"),
                cwd=d["cwd"],
                created=datetime.fromisoformat(d["created"]),
                expires=datetime.fromisoformat(d["expires"]),
                log=d["log"],
                spawn_cwd=d.get("spawn_cwd"),
                schema=SCHEMA_SUPERVISED,
                generation=str(d["generation"]),
                state=str(d["state"]),
                state_since=_dt(d.get("state_since")),
                plan=d.get("plan"),
                supervisor=_opt(SupervisorRef.from_dict, d.get("supervisor")),
                readiness=d.get("readiness"),
                stop=_opt(StopRequest.from_dict, d.get("stop")),
                cleanup=_opt(CleanupRecord.from_dict, d.get("cleanup")),
                recovery=_opt(RecoveryClaim.from_dict, d.get("recovery")),
                error=d.get("error"),
                claims=_claims_from(d.get("claims")),
            )
        except (KeyError, ValueError, TypeError, AttributeError) as e:
            raise DevctlError(LEASE_INVALID, f"malformed lease: {e}") from e

    @classmethod
    def _from_legacy_dict(cls, d: dict[str, Any]) -> "Lease":
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
                watchdog_pid_start_time=(
                    None
                    if d.get("watchdog_pid_start_time") is None
                    else float(d["watchdog_pid_start_time"])
                ),
                claims=_claims_from(d.get("claims")),
            )
        except (KeyError, ValueError, TypeError, AttributeError) as e:
            raise DevctlError(LEASE_INVALID, f"malformed lease: {e}") from e

    # --- disk I/O ---------------------------------------------------------

    def write(self, path: Path) -> None:
        """Atomically write the lease to ``path`` (temp + ``os.replace``)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        # A lease carries filesystem paths (`cwd`, `spawn_cwd`, `log`), which on
        # any Mac may contain non-ASCII. Escaping them is not wrong, only
        # unreadable — and this file is what a human opens when a teardown has
        # gone strange.
        tmp.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    @classmethod
    def read(cls, path: Path) -> "Lease":
        """Read + parse a lease. Corrupt content → ``DevctlError(LEASE_INVALID)``.

        Raises ``FileNotFoundError`` if the file is absent — callers that treat
        "no lease" as normal should use :meth:`read_if_exists`.
        """
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise DevctlError(LEASE_INVALID, f"unparseable lease {path}: {e}") from e
        if not isinstance(raw, dict):
            raise DevctlError(LEASE_INVALID, f"lease {path} is not an object")
        return cls.from_dict(raw)

    @classmethod
    def read_if_exists(cls, path: Path) -> "Lease | None":
        """Return the lease, ``None`` if the file is missing, or raise on corrupt."""
        if not path.exists():
            return None
        return cls.read(path)

    # --- pure helpers -----------------------------------------------------

    def process_handle(self) -> ProcessHandle:
        """The typed handle for a ``process``-runner lease."""
        return ProcessHandle.from_dict(self.handle)

    def ownership(self) -> WorkloadIdentity | None:
        """The session that names this lease's workload, or ``None`` if none can.

        In order (ADR-0016 §2, §6, §12):

        * a handle carrying ``sid`` fields names the session directly;
        * a schema-2 lease whose supervisor has **registered** names session S
          = the supervisor's pid, even before the handle is written — §6 step 2
          relies on this when the ``after_launch`` write fails;
        * a legacy ``{pid, pid_start_time}`` handle maps to the old leader's
          own session: 1.0.x spawned it with ``start_new_session=True``;
        * otherwise ``None``: an unregistered ``starting`` lease, which by I1
          has launched nothing.
        """
        h = self.handle or {}
        if "sid" in h:
            return WorkloadIdentity(
                sid=int(h["sid"]),
                owner_start=float(h["sid_owner_start_time"]),
                owner=str(h["sid_owner"]),
            )
        if not self.is_legacy:
            if self.supervisor is not None and self.supervisor.registered:
                return WorkloadIdentity(
                    self.supervisor.pid, self.supervisor.start_time, SID_OWNER_SUPERVISOR
                )
            return None
        if "pid" in h:
            return WorkloadIdentity(
                sid=int(h["pid"]),
                owner_start=float(h["pid_start_time"]),
                owner=SID_OWNER_WORKLOAD,
            )
        return None  # a legacy lease with no handle pid names nothing — never guess one

    # --- claims (ADR-0017) -------------------------------------------------

    def claim_map(self) -> dict[str, Claim]:
        """Every recorded claim, lapsed or not. A pre-upgrade lease (no
        ``claims`` field) reads as one claim held by its ``session`` (§5)."""
        if self.claims is not None:
            return dict(self.claims)
        return {self.session or UNKNOWN_SESSION: Claim(expires=self.expires)}

    def live_claims(self, now: datetime) -> dict[str, Claim]:
        """The claims that have not lapsed at ``now``."""
        return {k: c for k, c in self.claim_map().items() if now < c.expires}

    def with_claims(self, claims: dict[str, Claim], now: datetime) -> "Lease":
        """A copy holding ``claims`` pruned of lapsed ones, with ``expires``
        derived: the latest live claim's expiry. With none live the lease is
        expired now, which is what "nobody is entitled to it" means (§1)."""
        live = {k: c for k, c in claims.items() if now < c.expires}
        expires = max((c.expires for c in live.values()), default=min(self.expires, now))
        return replace(self, claims=live, expires=expires)

    def claimed(
        self, session: str, *, expires: datetime, now: datetime, via: str | None = None
    ) -> "Lease":
        """Add or renew ``session``'s claim, and only that one (§3)."""
        claims = self.claim_map()
        prev = claims.get(session)
        live_prev = prev is not None and now < prev.expires
        claims[session] = Claim(
            expires=expires,
            since=prev.since if live_prev and prev.since is not None else now,
            via=via if via is not None else (prev.via if prev is not None else None),
        )
        return self.with_claims(claims, now)

    def released(self, session: str, now: datetime) -> "Lease":
        """Drop ``session``'s claim, and only that one (§4)."""
        claims = self.claim_map()
        claims.pop(session, None)
        return self.with_claims(claims, now)

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires

    def renewed(self, new_expires: datetime) -> "Lease":
        """A copy with a pushed-out expiry (renewal = rewrite ``expires``, §6.2)."""
        return replace(self, expires=new_expires)

    def with_watchdog(self, watchdog_pid: int, start_time: float | None = None) -> "Lease":
        return replace(self, watchdog_pid=watchdog_pid, watchdog_pid_start_time=start_time)


def list_lease_files(leases_dir: Path) -> list[Path]:
    """Every ``<project>.json`` lease file on the machine (spec §4.3/§4.4)."""
    if not leases_dir.is_dir():
        return []
    return sorted(leases_dir.glob("*.json"))
