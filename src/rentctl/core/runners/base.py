# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Drake

"""The frozen runner interface (spec §6).

A runner knows how to start, stop, liveness-check, and enumerate orphans for
one *kind* of environment. The handle it returns is runner-specific (process →
``{pid, pid_start_time}``; compose → a compose project name) and is stored
verbatim in the lease's ``handle`` field, so the interface stays agnostic to
what a handle actually contains.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..errors import REGISTRY_INVALID, DevctlError
from ..models import CLEANUP_INCOMPLETE, CLEANUP_VERIFIED, StopOutcome
from ..registry import RegistryProfile


@runtime_checkable
class Runner(Protocol):
    """Start/stop/alive/orphans for one environment kind. All impure.

    ``stop`` and ``alive`` are **verify-then-act**: a runner must confirm the
    handle still points at the *same* process/container it started before
    killing anything (spec §5.2 — never kill what we don't own).
    """

    name: str

    def start(self, entry: RegistryProfile, port: int, log_path: Path) -> Any:
        """Spawn the environment on ``port``, logging to ``log_path``; return a handle."""
        ...

    def stop(self, handle: Any) -> StopOutcome | None:
        """Idempotent teardown. A dead/recycled handle is a no-op, never an error.

        Returns what it achieved (ADR-0016 §4). ``None`` is what a runner
        written against the 1.0 interface returns; :func:`stop_outcome` turns
        that into an answer by asking ``alive`` afterwards.
        """
        ...

    def alive(self, handle: Any) -> bool:
        """Is the *same* environment behind this handle still running?"""
        ...

    def orphans(self) -> list[Any]:
        """Broker-marked, lease-less handles this runner can find machine-wide."""
        ...


def stop_outcome(runner: Runner, handle: Any) -> StopOutcome:
    """Stop ``handle`` and return what the stop actually achieved.

    Every teardown path goes through here so none of them can read "``stop()``
    returned" as "the workload is gone" — the exact inference that deleted
    leases over live servers in 1.0.x (S1). A runner that reports a
    :class:`StopOutcome` is taken at its word; one that returns ``None`` is
    asked ``alive()`` afterwards, and a live answer is ``incomplete``.
    """
    result = runner.stop(handle)
    if isinstance(result, StopOutcome):
        return result
    if runner.alive(handle):
        return StopOutcome(
            cleanup=CLEANUP_INCOMPLETE,
            signalled=True,
            detail="signalled, but the runner still reports it alive",
        )
    return StopOutcome(cleanup=CLEANUP_VERIFIED, signalled=True)


def get_runner(name: str) -> Runner:
    """Resolve a runner by its registry ``runner`` name.

    ``compose`` is design-frozen but not built in v1 (spec §6.3) — asking for it
    fails clearly rather than silently doing nothing.
    """
    # Imported lazily to avoid a package import cycle at module load.
    from .process import ProcessRunner

    if name == "process":
        return ProcessRunner()
    if name == "compose":
        raise DevctlError(
            REGISTRY_INVALID,
            "the 'compose' runner is designed but not implemented in v1 (spec §6.3)",
        )
    raise DevctlError(REGISTRY_INVALID, f"unknown runner {name!r}")
