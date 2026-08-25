# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Drake

"""Lease files — annotations claiming ownership and expiry (spec §5.2).

A lease is a *claim*, not ground truth: it can be stale, corrupt, or point at a
dead or recycled PID. The OS process table is what's real; the reconciler
(``reconcile.py``) checks every lease against it before acting. This module is
just the typed serialization + safe read/write of the claim.

Writes are atomic (temp file + ``os.replace``) so a crash mid-write can never
leave a half-written lease that later reads as corrupt.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from .errors import LEASE_INVALID, DevctlError
from .models import ProcessHandle


@dataclass(frozen=True)
class Lease:
    """A claim over one running environment (spec §5.2)."""

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

    # --- serialization ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "profile": self.profile,
            "runner": self.runner,
            "handle": self.handle,
            "port": self.port,
            "watchdog_pid": self.watchdog_pid,
            "session": self.session,
            "cwd": self.cwd,
            "created": self.created.isoformat(),
            "expires": self.expires.isoformat(),
            "log": self.log,
            "spawn_cwd": self.spawn_cwd,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Lease":
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

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires

    def renewed(self, new_expires: datetime) -> "Lease":
        """A copy with a pushed-out expiry (renewal = rewrite ``expires``, §6.2)."""
        return replace(self, expires=new_expires)

    def with_watchdog(self, watchdog_pid: int) -> "Lease":
        return replace(self, watchdog_pid=watchdog_pid)


def list_lease_files(leases_dir: Path) -> list[Path]:
    """Every ``<project>.json`` lease file on the machine (spec §4.3/§4.4)."""
    if not leases_dir.is_dir():
        return []
    return sorted(leases_dir.glob("*.json"))
