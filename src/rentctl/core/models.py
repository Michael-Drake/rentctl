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


@dataclass(frozen=True)
class ProcessHandle:
    """What the process runner needs to find and safely kill a server.

    ``pid_start_time`` is ``psutil.Process(pid).create_time()`` captured at
    spawn. It is the PID-recycling guard: a PID can be reused by an unrelated
    process, but that process will have a different start time (spec §5.2).
    """

    pid: int
    pid_start_time: float

    def to_dict(self) -> dict[str, Any]:
        return {"pid": self.pid, "pid_start_time": self.pid_start_time}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ProcessHandle":
        return cls(pid=int(d["pid"]), pid_start_time=float(d["pid_start_time"]))


@dataclass(frozen=True)
class ProcInfo:
    """Identity of a process holding a port — for ``PORT_SQUATTED`` (F7) and
    squatter reporting in ``env_ls``."""

    pid: int
    name: str
    cmdline: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"pid": self.pid, "name": self.name, "cmdline": list(self.cmdline)}
