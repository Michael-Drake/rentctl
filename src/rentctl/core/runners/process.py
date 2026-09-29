# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Drake

"""The ``process`` runner (spec §6.1, with ADR-0016 §4's semantics).

Spawns a dev-server command in its **own session**, so everything it starts —
npm's vite/node children, and their orphans once the shell exits — is findable
by session id. The handle names that session (ADR-0016 §2).

What changed from 1.0.x is what ``alive`` and ``stop`` *mean*. Both used to
look only at the leader shell: ``stop`` returned the moment the shell died, so
a child that ignored SIGTERM outlived the teardown, and ``alive`` read "npm
exited, vite still holds the port" as dead (S1). Now ``alive`` means "the
workload session has members" and ``stop`` runs the one stop algorithm
(:func:`~rentctl.core.workload.stop_workload`) in recovery mode, returning a
:class:`~rentctl.core.models.StopOutcome` instead of ``None``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .. import procutil
from ..models import SID_OWNER_WORKLOAD, ProcessHandle, StopOutcome
from ..procutil import Membership, ProcessTable
from ..registry import RegistryProfile
from ..workload import KILL_GRACE_S, RECOVERY, RESCAN_S, TERM_GRACE_S, stop_workload


class ProcessRunner:
    name = "process"

    def __init__(
        self,
        term_grace_s: float = TERM_GRACE_S,
        kill_grace_s: float = KILL_GRACE_S,
        *,
        rescan_s: float = RESCAN_S,
        table: ProcessTable | None = None,
    ):
        # Grace periods are injectable so tests don't wait real seconds; the
        # table so the decision logic can run against a fake process table.
        self.term_grace_s = term_grace_s
        self.kill_grace_s = kill_grace_s
        self.rescan_s = rescan_s
        self.table = table

    # --- start ------------------------------------------------------------

    def start(self, entry: RegistryProfile, port: int, log_path: Path) -> ProcessHandle:
        """Spawn ``entry.cmd`` with its own session, port injected, logs → file.

        Returns a :class:`ProcessHandle` whose start time is captured
        immediately so later signals can prove identity.
        """
        log_path.parent.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, entry.port_env: str(port)}
        # Own session (start_new_session=True) → the shell is the session and
        # group leader, so sid == pgid == its pid, and everything it spawns is
        # born into that session.
        with open(log_path, "ab", buffering=0) as log_f:
            proc = subprocess.Popen(
                entry.cmd,
                shell=True,
                cwd=entry.cwd,
                env=env,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        # None only if it died between spawn and observe; a 0.0 sentinel then
        # reads as "owner unverifiable" everywhere, so it is never signalled.
        start_time = procutil.observe_start_time(proc.pid) or 0.0
        return ProcessHandle(
            pid=proc.pid,
            pid_start_time=start_time,
            sid=proc.pid,
            sid_owner_start_time=start_time,
            sid_owner=SID_OWNER_WORKLOAD,
        )

    # --- stop -------------------------------------------------------------

    def stop(self, handle: ProcessHandle) -> StopOutcome:
        """Stop every verified member of the handle's session. Idempotent.

        Recovery mode: this process is not the session's owner, so each member
        gets its own verified signal and no group is signalled (ADR-0016 §3).
        An already-empty session is ``verified`` with nothing sent; an owner
        that cannot be verified is ``incomplete`` with nothing sent.
        """
        return stop_workload(
            handle.identity(),
            RECOVERY,
            term_grace_s=self.term_grace_s,
            kill_grace_s=self.kill_grace_s,
            rescan_s=self.rescan_s,
            table=self.table,
        )

    # --- alive / orphans --------------------------------------------------

    def alive(self, handle: ProcessHandle) -> bool:
        """Does the workload session still have members?

        Not "does the leader live": npm exiting while vite keeps the port is a
        running server. ``AMBIGUOUS`` reads as alive on purpose — "cannot tell"
        is not "nobody there" (ADR-0008) — so the lease is kept and surfaced
        rather than deleted over something that may be ours.
        """
        ident = handle.identity()
        scan = procutil.session_scan(
            ident.sid, ident.owner_start, ident.exclude, table=self.table
        )
        return scan.state is not Membership.EMPTY

    def orphans(self) -> list[ProcessHandle]:
        # The process runner has no reliable machine-wide marker; sweep relies
        # on leases + the port block instead (spec §6.1). Compose will do better.
        return []
