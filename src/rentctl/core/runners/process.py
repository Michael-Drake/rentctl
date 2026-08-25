# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Drake

"""The ``process`` runner — v1, fully specified (spec §6.1).

Spawns a dev-server command in its **own process group** so teardown catches
npm's child vite/node processes. Every kill is guarded by the PID-start-time
check (§5.2): we re-verify the handle points at the same process before
signalling, and again before escalating to SIGKILL. A recycled PID reads as
dead and is never touched.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

from .. import procutil
from ..models import ProcessHandle
from ..registry import RegistryProfile

_TERM_GRACE_S = 10.0   # SIGTERM → wait → SIGKILL (spec §6.1)
_KILL_GRACE_S = 2.0
_POLL_S = 0.05


class ProcessRunner:
    name = "process"

    def __init__(self, term_grace_s: float = _TERM_GRACE_S, kill_grace_s: float = _KILL_GRACE_S):
        # Grace periods are injectable so tests don't wait real seconds.
        self.term_grace_s = term_grace_s
        self.kill_grace_s = kill_grace_s

    # --- start ------------------------------------------------------------

    def start(self, entry: RegistryProfile, port: int, log_path: Path) -> ProcessHandle:
        """Spawn ``entry.cmd`` with its own session, port injected, logs → file.

        Returns a :class:`ProcessHandle` whose ``pid_start_time`` is captured
        immediately so later kills can prove identity.
        """
        log_path.parent.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, entry.port_env: str(port)}
        # Own session/group (start_new_session=True) → the shell is the group
        # leader and killpg(pgid) reaches every child it spawned.
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
        start_time = procutil.observe_start_time(proc.pid)
        # None only if it died between spawn and observe; a 0.0 sentinel then
        # reads as "not our process" everywhere, so it is never mis-killed.
        return ProcessHandle(pid=proc.pid, pid_start_time=start_time or 0.0)

    # --- stop -------------------------------------------------------------

    def stop(self, handle: ProcessHandle) -> None:
        """Verify-then-kill the whole process group. Idempotent (spec §6.1)."""
        if not procutil.is_alive(handle):
            return  # already dead or PID recycled — never kill what we don't own
        # Shared with the readiness probe, which compares this against the pgid
        # of whatever is listening on the port to tell our server from a squatter.
        pgid = procutil.process_group_of(handle.pid)
        if pgid is None:
            return
        self._signal_group(pgid, signal.SIGTERM)
        if self._wait_gone(handle, self.term_grace_s):
            return
        # Survivors: re-verify identity, then escalate.
        if procutil.is_alive(handle):
            self._signal_group(pgid, signal.SIGKILL)
            self._wait_gone(handle, self.kill_grace_s)

    # --- alive / orphans --------------------------------------------------

    def alive(self, handle: ProcessHandle) -> bool:
        return procutil.is_alive(handle)

    def orphans(self) -> list[ProcessHandle]:
        # The process runner has no reliable machine-wide marker; sweep relies
        # on leases + the port block instead (spec §6.1). Compose will do better.
        return []

    # --- internals --------------------------------------------------------

    @staticmethod
    def _signal_group(pgid: int, sig: int) -> None:
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):  # pragma: no cover - race/perm
            pass

    def _wait_gone(self, handle: ProcessHandle, timeout: float) -> bool:
        """Poll until the handle's process is gone or ``timeout`` elapses."""
        waited = 0.0
        while waited < timeout:
            if not procutil.is_alive(handle):
                return True
            time.sleep(_POLL_S)
            waited += _POLL_S
        return not procutil.is_alive(handle)
