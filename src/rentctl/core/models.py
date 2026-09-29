# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Drake

"""Shared value types crossing module boundaries.

Kept dependency-free (stdlib only) so both the pure reconciler and the
impure runner/service layers can import them without cycles.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class Readiness(str, Enum):
    """What the start-up probe actually established (spec §4.1 step 4, F8).

    This was a ``bool``, and the two ways of *not* being ``True`` call for
    opposite responses from the operator: a server that never started should be
    stopped and its log read, while a server that started and bound an address
    the probe did not look at should be left alone. Folding them together is
    what made rentctl kill correctly-started servers — the failure payload
    carried the log line proving success.

    ``UNKNOWN`` is the case the old shape could not express at all: the probe
    could not run, which is not evidence of absence
    (``declare-what-a-check-assumes``). It is deliberately distinct from
    ``NOT_LISTENING``, which means the probe ran and found nothing.
    """

    ANSWERED = "answered"            # a TCP connect to loopback succeeded
    LISTENING = "listening"          # our process group listens, but not on loopback
    NOT_LISTENING = "not_listening"  # probe ran: nothing of ours is listening
    UNKNOWN = "unknown"              # the probe could not answer
    NOT_PROBED = "not_probed"        # no start happened (renewed an existing lease)

    @property
    def is_up(self) -> bool:
        """Did the server demonstrably come up? Only ``NOT_LISTENING`` says no.

        ``UNKNOWN`` counts as up on purpose. The alternative is killing a
        process we cannot prove failed, and the lease we write instead keeps it
        tracked — so it is swept on expiry or session end rather than orphaned.
        """
        return self is not Readiness.NOT_LISTENING


SID_OWNER_SUPERVISOR = "supervisor"  # 1.1: session S is a supervisor's (ADR-0016 §1)
SID_OWNER_WORKLOAD = "workload"      # 1.0.x: the spawned shell was itself the session leader


@dataclass(frozen=True)
class WorkloadIdentity:
    """What names a workload: its session, not a pid list (ADR-0016 §2).

    ``owner_start`` is the ``create_time`` of the process that created session
    ``sid``. When the owner is a supervisor it is excluded from membership —
    it is the one doing the stopping, and it verifies the outcome.

    This is also what ``Lease.ownership()`` returns (``leases.OwnershipIdentity``
    is an alias of this class). Two parallel lanes each wrote one; a lease's
    identity and the identity ``stop_workload`` consumes are the same fact, so
    they are one type, and the lease-side field names survive as read-only
    properties so the ADR §12 vocabulary still reads naturally at call sites.
    """

    sid: int
    owner_start: float
    owner: str = SID_OWNER_WORKLOAD
    # Linux only, and only in the live supervisor's own view of itself: its pid
    # when it holds the child-subreaper attribute, so its descendants are
    # members whatever their SID (ADR-0016 §5, plan step 9). Never persisted —
    # the subtree ends with the supervisor, so no reader of a lease may use it.
    tree_root: int | None = None

    @property
    def exclude(self) -> frozenset[int]:
        return frozenset({self.sid}) if self.owner == SID_OWNER_SUPERVISOR else frozenset()

    @property
    def sid_owner_start_time(self) -> float:
        return self.owner_start

    @property
    def sid_owner(self) -> str:
        return self.owner

    @property
    def excludes_owner(self) -> bool:
        """A supervisor is session S's owner but never a workload member."""
        return self.owner == SID_OWNER_SUPERVISOR


@dataclass(frozen=True)
class ProcessHandle:
    """What the process runner needs to find and safely kill a server.

    ``pid``/``pid_start_time`` name the **leader** — the spawned shell — and are
    kept for the ``pid`` field of the up/ls envelopes. ``pid_start_time`` is
    ``psutil.Process(pid).create_time()`` captured at spawn: the PID-recycling
    guard (spec §5.2).

    The ``sid*`` fields name the **workload** (ADR-0016 §2, §12), which is what
    ``alive`` and ``stop`` act on. A 1.0.x handle carries none of them; it maps
    to ``sid = pid`` because 1.0.x spawned the shell with
    ``start_new_session=True``, making it its own session leader (§14). No
    process has to move for the mapping to hold.
    """

    pid: int
    pid_start_time: float
    sid: int | None = None
    sid_owner_start_time: float | None = None
    sid_owner: str = SID_OWNER_WORKLOAD

    def identity(self) -> WorkloadIdentity:
        return WorkloadIdentity(
            sid=self.pid if self.sid is None else self.sid,
            owner_start=(
                self.pid_start_time if self.sid_owner_start_time is None else self.sid_owner_start_time
            ),
            owner=self.sid_owner,
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"pid": self.pid, "pid_start_time": self.pid_start_time}
        if self.sid is not None:
            # Only when recorded: a legacy handle round-trips byte-for-byte, so a
            # renewed 1.0.x lease stays in the format its own watchdog reads.
            # 1.0.x readers ignore these keys, so a new handle is safe for them.
            out["sid"] = self.sid
            out["sid_owner_start_time"] = self.sid_owner_start_time
            out["sid_owner"] = self.sid_owner
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ProcessHandle":
        return cls(
            pid=int(d["pid"]),
            pid_start_time=float(d["pid_start_time"]),
            sid=None if d.get("sid") is None else int(d["sid"]),
            sid_owner_start_time=(
                None if d.get("sid_owner_start_time") is None else float(d["sid_owner_start_time"])
            ),
            sid_owner=str(d.get("sid_owner") or SID_OWNER_WORKLOAD),
        )


@dataclass(frozen=True)
class Survivor:
    """A member still alive after a stop: the ``survivors`` rows (ADR-0016 §4, §12)."""

    pid: int
    start_time: float | None
    name: str
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {"pid": self.pid, "start_time": self.start_time, "name": self.name, "status": self.status}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Survivor":
        # ``start_time`` may be null: a survivor whose start time could not be
        # read is still reported (procutil keeps it as a member) — it is exactly
        # the row an operator needs to see.
        start = d.get("start_time")
        return cls(
            pid=int(d["pid"]),
            start_time=None if start is None else float(start),
            name=str(d["name"]),
            status=str(d["status"]),
        )


CLEANUP_VERIFIED = "verified"
CLEANUP_INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class StopOutcome:
    """What a stop achieved — returned, so nobody has to infer it (ADR-0016 §4).

    1.0.x ``stop()`` returned ``None`` and returned as soon as the *leader* was
    gone, so "the shell died" was indistinguishable from "the workload is gone".
    ``cleanup`` is ``verified`` only when a final scan found the session empty.

    ``signalled`` records whether any signal was actually delivered, which is
    what the ``killed`` evidence means. ``identity_ambiguous`` marks a refusal:
    session S's owner could not be verified, so nothing was signalled at all.
    """

    cleanup: str
    survivors: tuple[Survivor, ...] = ()
    escalated: bool = False
    signalled: bool = False
    identity_ambiguous: bool = False
    detail: str = ""

    @property
    def verified(self) -> bool:
        return self.cleanup == CLEANUP_VERIFIED

    def survivor_dicts(self) -> list[dict[str, Any]]:
        return [s.to_dict() for s in self.survivors]


@dataclass(frozen=True)
class ProcInfo:
    """Identity of a process holding a port — for ``PORT_SQUATTED`` (F7) and
    squatter reporting in ``env_ls``."""

    pid: int
    name: str
    cmdline: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"pid": self.pid, "name": self.name, "cmdline": list(self.cmdline)}
