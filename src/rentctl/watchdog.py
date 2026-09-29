# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Drake

"""Per-environment watchdog — the detached babysitter (spec §6.2).

``env_up`` spawns ``python -m rentctl.watchdog <lease-key>`` detached. The
argument is a **lease key** (``<project>--<cwd-hash>``), not a project name: a
project can have several live instances at once, one per worktree, and a
watchdog born for one of them must not act on another (ADR-0007 §5).

Every tick it re-reads the lease and acts:

* lease gone            → exit (someone downed it)
* workload gone         → remove lease, exit (its session has no members left;
                          a dead leader shell alone is not "gone", ADR-0016 §4)
* ``now >= expires``    → stop the server, remove lease once the stop is
                          verified, exit; survivors keep the lease and retry
* otherwise             → sleep, tick again

Renewal is just rewriting ``expires`` in the lease (``env_up`` on a running
environment); the watchdog notices on its next tick. If the watchdog itself
dies, the next ``sweep``/``up``/``ls`` reconcile is the backstop (layer 4, §8).

This is one of the four independent cleanup layers — deliberately simple, single
project, no registry needed.

**Since 1.1 (ADR-0016) nothing spawns this.** A per-lease supervisor owns every
new environment. A watchdog still running from 1.0.x keeps babysitting its
*legacy* lease with the fixed runner, and exits at once — touching nothing —
when the lease under its key is schema 2 (``exit-lease-supervised``).
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .core import events as ev
from .core.errors import DevctlError
from .core.events import EventLog
from .core.leases import Lease
from .core.locking import project_lock
from .core.logcap import trim_log_if_large
from .core.models import StopOutcome
from .core.paths import DevctlPaths, project_from_key
from .core.runners import Runner, get_runner, stop_outcome
from .core.service import _now_local

# Single-tick outcomes.
GONE = "exit-lease-gone"
DEAD = "exit-process-dead"
EXPIRED = "exit-lease-expired"
CORRUPT = "exit-lease-corrupt"
SUPERVISED = "exit-lease-supervised"  # a schema-2 lease: its supervisor owns it
CONTINUE = "continue"

DEFAULT_INTERVAL_S = 60.0


def watch_once(
    key: str,
    paths: DevctlPaths,
    now: datetime,
    runner_factory: Callable[[str], Runner] = get_runner,
    events: EventLog | None = None,
) -> str:
    """One reconcile tick for ``project``, under its lock. Returns an outcome.

    Both terminal outcomes that touch a lease are recorded as cleanup layer 3
    (spec §8) — this is the only code path that can produce layer-3 evidence,
    and the pilot gate's G4 has no other way to see it fire. ``GONE`` and
    ``CORRUPT`` are not recorded: the watchdog changed nothing, and whoever did
    remove the lease logged it themselves.
    """
    log = events or EventLog(paths.events_file)
    lease_path = paths.lease_file(key)
    with project_lock(paths.lock_file(project_from_key(key))):
        try:
            lease = Lease.read_if_exists(lease_path)
        except DevctlError:
            return CORRUPT  # unparseable lease — let sweep/ls surface the server as a squatter
        if lease is None:
            return GONE
        if not lease.is_legacy:
            # A per-lease supervisor owns this environment (ADR-0016). Its
            # `starting` record has no process handle yet, and every other state
            # is the supervisor's to act on: a watchdog here could only race it.
            # 1.1 never spawns one; this is the exit for one that finds a
            # replacement lease under its key.
            return SUPERVISED
        runner = runner_factory(lease.runner)
        handle = lease.process_handle()
        if not runner.alive(handle):
            lease_path.unlink(missing_ok=True)
            _record(log, lease, ev.PROCESS_GONE, killed=False)
            return DEAD
        if lease.is_expired(now):
            # Take the outcome the stop established rather than asserting it.
            # This is the ONLY path that produces cleanup-layer-3 evidence for
            # the pilot gate, so `killed=True` on an unverified stop does not
            # merely mislead — it manufactures the evidence the gate is scored
            # from. And "the leader died" is not "the workload is gone" (S1).
            outcome = stop_outcome(runner, handle)
            if not outcome.verified:
                # Keep the lease: it is the only record naming a session that
                # still has members holding a port. Return CONTINUE so the next
                # tick retries rather than treating this as finished.
                log.record_cleanup_incomplete(
                    lease.project,
                    op="watchdog",
                    reason=ev.EXPIRY,
                    reason_source=ev.DECLARED,
                    survivors=outcome.survivor_dicts(),
                    identity_ambiguous=outcome.identity_ambiguous,
                    escalated=outcome.escalated,
                    port=lease.port,
                    pid=handle.pid,
                    detail=outcome.detail,
                )
                return CONTINUE
            lease_path.unlink(missing_ok=True)
            _record(log, lease, ev.EXPIRY, killed=True, outcome=outcome)
            return EXPIRED
        # Server is alive and within lease — keep its log bounded (approved follow-up).
        trim_log_if_large(Path(lease.log))
        return CONTINUE


def _record(
    log: EventLog, lease: Lease, reason: str, *, killed: bool, outcome: StopOutcome | None = None
) -> None:
    log.record_down(
        lease.project,
        op="watchdog",
        reason=reason,
        reason_source=ev.DECLARED,
        killed=killed,
        port=lease.port,
        pid=lease.process_handle().pid,
        cleanup=outcome.cleanup if outcome is not None else None,
        escalated=outcome.escalated if outcome is not None else None,
    )


def run(
    key: str,
    paths: DevctlPaths | None = None,
    *,
    interval: float = DEFAULT_INTERVAL_S,
    now_fn: Callable[[], datetime] = _now_local,
    sleep_fn: Callable[[float], None] = time.sleep,
    runner_factory: Callable[[str], Runner] = get_runner,
    max_ticks: int | None = None,
    events: EventLog | None = None,
) -> str:
    """Loop ``watch_once`` until a terminal outcome (or ``max_ticks`` for tests)."""
    paths = paths or DevctlPaths.default()
    log = events or EventLog(paths.events_file)
    ticks = 0
    while True:
        outcome = watch_once(key, paths, now_fn(), runner_factory, log)
        if outcome != CONTINUE:
            return outcome
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            return "max-ticks"
        sleep_fn(interval)


DEPRECATION_NOTICE = (
    "rentctl: rent-watchdog is deprecated since 1.1 — a per-lease supervisor owns every "
    "new environment; this only babysits leases written by 1.0.x and is removed in 2.0"
)


def main(argv: list[str] | None = None) -> int:
    """The ``rent-watchdog`` / ``devctl-watchdog`` entry point (deprecated, ADR-0016 §14).

    The console scripts stay until 2.0 because removing them is an interface
    change. The notice goes to stderr, one line, so a caller parsing the outcome
    on stdout is unaffected.
    """
    print(DEPRECATION_NOTICE, file=sys.stderr)
    # See cli.py: no explicit prog, so --help names the alias actually invoked.
    parser = argparse.ArgumentParser(
        description="per-environment lease watchdog (deprecated since 1.1; legacy 1.0.x leases only)"
    )
    parser.add_argument("key", help="lease key to babysit: <project>--<cwd-hash>")
    parser.add_argument(
        "--interval", type=float, default=DEFAULT_INTERVAL_S, help="tick seconds (default 60)"
    )
    args = parser.parse_args(argv)
    outcome = run(args.key, interval=args.interval)
    print(outcome)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
