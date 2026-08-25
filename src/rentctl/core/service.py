# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Drake

"""The four operations, shared by the MCP server and the CLI (spec §4).

Every method returns a plain JSON-able dict: ``{"ok": true, ...}`` on success or
the ``{"ok": false, "error": ...}`` envelope on a :class:`DevctlError`. The MCP
and CLI shells are thin wrappers that just serialize what these return.

All mutation of a project's lease happens under that project's flock (§5.3), and
every operation reconciles lease claims against the OS before acting (§3.1).

**F10 interpretation.** A bad registry fails ``env_up`` closed (nothing is
started on bad config) and disables squatter detection in ``ls``/``sweep`` (that
alone needs the registry). But lease-driven **cleanup** — ``env_down`` and the
reconcile half of ``ls``/``sweep`` — still runs, because a lease is
self-contained ground truth and stranding a real orphan behind a corrupt
registry would defeat rentctl's whole purpose. Raised with the spec author as a reading of F10.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import events as ev
from . import procutil
from .enroll import check_pin
from .errors import (
    BLOCK_EXHAUSTED,
    CMD_CHANGED,
    INVALID_CWD,
    PROFILE_MISMATCH,
    START_TIMEOUT,
    DevctlError,
)
from .events import EventLog
from .leases import Lease, list_lease_files
from .locking import project_lock
from .models import ProcInfo, Readiness
from .paths import DevctlPaths, lease_key, project_from_key
from .ports import draw_port
from .reconcile import Action, decide
from .registry import BLOCK_SIZE, Registry, RegistryEntry
from .runners import Runner, get_runner
from .worktree import resolve_spawn_cwd

DEFAULT_LEASE_MINUTES = 120
MAX_LEASE_MINUTES = 480
_LOG_TAIL_LINES = 40

# Readiness polling. The loopback connect is cheap enough to run often; the
# owner probe shells out to `lsof` on macOS, so it runs at a tenth the rate —
# fast enough to notice a non-loopback bind within a second, slow enough not to
# spawn 300 subprocesses waiting out a 30 s timeout.
_READY_POLL_S = 0.1
_OWNER_PROBE_INTERVAL_S = 1.0

# Runtime-neutral first, then each known runtime's own name (ADR-0011 §4).
# DEVCTL_* exists so a runtime rentctl has never heard of can still be wired by
# exporting one variable, rather than waiting for a release that knows its name.
PROJECT_DIR_ENVS = ("DEVCTL_PROJECT_DIR", "CLAUDE_PROJECT_DIR", "GEMINI_PROJECT_DIR")

# Each name below is verified against the runtime that sets it (WI-0036). This
# list previously carried `CLAUDE_SESSION_ID`, which NOTHING sets — so lease
# attribution never once resolved under the only runtime the pilot actually uses,
# and all 33 events in the log recorded `unknown`. The list looked plausible and
# was never exercised, which is precisely why a wrong name survives: it fails
# into a value that reads like a legitimate answer.
#
# Provenance, so the next reader does not have to re-derive it:
#   DEVCTL_SESSION_ID       — ours. The documented override, and the escape hatch
#                             for a runtime rentctl has never heard of. Kept FIRST
#                             so it always wins.
#   CLAUDE_CODE_SESSION_ID  — set by Claude Code in the session's process
#                             environment; observed directly, and it is the same
#                             variable an external session harness matches
#                             journals on. NOTE: it is NOT in the published hook
#                             docs, which say session_id arrives as JSON on stdin.
#                             So this is verified-by-observation, not by contract,
#                             and could change without notice. That is acceptable
#                             here only because the failure mode is degrading to
#                             `unattributed` — visible in the summary's
#                             `sessions` block — rather than a wrong attribution.
#   GEMINI_SESSION_ID       — documented by gemini-cli 0.35.2 in its own bundled
#                             docs (docs/hooks/index.md: "The unique ID for the
#                             current session"). Contract, not observation.
SESSION_ID_ENVS = ("DEVCTL_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "GEMINI_SESSION_ID")


def _first_env(names: tuple[str, ...]) -> str | None:
    """First of ``names`` set to a non-empty value, else None."""
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _now_local() -> datetime:
    """Timezone-aware local time (carries the offset into lease stamps)."""
    return datetime.now().astimezone()


def _port_answering(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


def _log_tail(path: str, n: int = _LOG_TAIL_LINES) -> list[str]:
    try:
        return Path(path).read_text(errors="replace").splitlines()[-n:]
    except OSError:  # pragma: no cover - defensive
        return []


class Service:
    """Stateless orchestration of the four tools over the on-disk state.

    All impure collaborators (clock, runner factory, readiness probe, watchdog
    spawner, session id) are injectable so the operations can be driven fast and
    deterministically in tests.
    """

    def __init__(
        self,
        paths: DevctlPaths | None = None,
        *,
        now_fn: Callable[[], datetime] = _now_local,
        runner_factory: Callable[[str], Runner] = get_runner,
        readiness_fn: Callable[[int, float, int | None], Readiness] | None = None,
        readiness_timeout: float = 30.0,
        watchdog_spawn: Callable[[str], int | None] | None = None,
        session_id_fn: Callable[[], str] | None = None,
        event_log: EventLog | None = None,
        port_owner_fn: Callable[[int], ProcInfo | None] | None = None,
        port_answering_fn: Callable[[int], bool] | None = None,
    ) -> None:
        self.paths = paths or DevctlPaths.default()
        self.paths.ensure_dirs()
        self._now = now_fn
        self.runner_factory = runner_factory
        self.readiness_fn = readiness_fn
        self.readiness_timeout = readiness_timeout
        self.watchdog_spawn = watchdog_spawn or self._spawn_watchdog
        self.session_id_fn = session_id_fn or self._caller_session
        self.events = event_log or EventLog(self.paths.events_file, now_fn=now_fn)
        # The last machine-touching dependency to get a seam, and the reason the
        # unit suite was not hermetic: every other one here is injectable, so the
        # port draw silently asked the real machine what was listening. Tests then
        # asserted against whatever happened to hold a port on the developer's Mac
        # — and the fixture registry hands them a real project's own block, 5180,
        # so the suite broke precisely when that project was in use.
        #
        # `None` means "the real probe," resolved through the module at CALL time
        # rather than captured here. That is deliberate: a default argument would
        # freeze the reference at import and silently break every test that fakes
        # the probe with `monkeypatch.setattr(procutil, "port_owner", …)` — the
        # seam would then cost more than it bought.
        self.port_owner_fn = port_owner_fn
        # The health probe is a SECOND machine call on a different path: `env_ls`
        # opens a real socket to report `healthy`, bypassing `readiness_fn` (which
        # polls with a timeout and answers a different question). Seaming only the
        # owner probe left `env_ls` still asking the machine — and it reported a
        # fake runner's environment as healthy because the pilot's real server
        # happened to be answering on that port.
        self.port_answering_fn = port_answering_fn

    def _port_owner(self, port: int) -> ProcInfo | None:
        """Who is listening on ``port``, through the seam.

        Raises :class:`procutil.ProbeUnavailable` when the probe cannot run at
        all (ADR-0008) — callers must keep distinguishing that from ``None``,
        which means "checked, nobody there."
        """
        if self.port_owner_fn is not None:
            return self.port_owner_fn(port)
        return procutil.port_owner(port)

    def _readiness(self, port: int, timeout: float, pgid: int | None) -> Readiness:
        """Did the server come up? Resolved through the seam, like the others.

        ``None`` means "the real probe," resolved at CALL time for the same
        reason as ``port_owner_fn`` above: a default argument would freeze the
        reference at import.
        """
        if self.readiness_fn is not None:
            return self.readiness_fn(port, timeout, pgid)
        return self._default_readiness(port, timeout, pgid)

    def _default_readiness(self, port: int, timeout: float, pgid: int | None) -> Readiness:
        """Poll for readiness, distinguishing *not on loopback* from *not up*.

        The loopback connect stays the fast path — it is what almost every dev
        server satisfies, and it is one syscall. What changed is the fallback:
        a server that binds a specific non-loopback address (a tailnet address,
        a chosen LAN interface) answers there and *never* on loopback, so the
        old probe timed out and env_up killed a process that had started
        perfectly. The log line proving it was up travelled inside the failure.

        So on each pass we also ask who is listening on the port at any address
        — but at a slower cadence, because on macOS that is an ``lsof``
        subprocess and polling it at 100 ms would spawn hundreds of them.
        """
        deadline = time.monotonic() + timeout
        next_owner_check = 0.0
        while time.monotonic() < deadline:
            if self._answering(port):
                return Readiness.ANSWERED
            now = time.monotonic()
            if now >= next_owner_check:
                next_owner_check = now + _OWNER_PROBE_INTERVAL_S
                if self._owner_verdict(port, pgid) is Readiness.LISTENING:
                    return Readiness.LISTENING
            time.sleep(_READY_POLL_S)
        # One final, definitive classification rather than whatever the last
        # periodic check happened to see. This is what separates "stopped and
        # its log is worth reading" from "running somewhere we cannot reach."
        return self._owner_verdict(port, pgid)

    def _owner_verdict(self, port: int, pgid: int | None) -> Readiness:
        """Classify a non-answering port by who, if anyone, is listening on it."""
        try:
            owner = self._port_owner(port)
        except procutil.ProbeUnavailable:
            return Readiness.UNKNOWN
        if owner is None:
            return Readiness.NOT_LISTENING
        if pgid is None:
            # Something is listening but we have no group to compare it against,
            # so we cannot say it is ours. Not proof of failure either.
            return Readiness.UNKNOWN
        owner_pgid = procutil.process_group_of(owner.pid)
        if owner_pgid is None:
            # A listener we can see but cannot attribute is NOT "nobody there" —
            # the same rule the psutil backend applies to an unattributable
            # connection. Killing on this would be killing on ignorance.
            return Readiness.UNKNOWN
        if owner_pgid == pgid:
            return Readiness.LISTENING
        # A foreign process holds the port, so ours did not come up on it. The
        # draw already refuses squatted ports (F7); this is the narrow race
        # where one appeared between the draw and the bind.
        return Readiness.NOT_LISTENING

    def _answering(self, port: int) -> bool:
        """Whether anything answers a TCP connect on ``port``, through the seam."""
        if self.port_answering_fn is not None:
            return self.port_answering_fn(port)
        return _port_answering(port)

    @staticmethod
    def _check_cwd(cwd: str | None) -> None:
        """Reject a ``cwd`` that was **supplied but empty**.

        Absent (``None``) is legal and means "use the caller's directory" — that
        is the documented shape for a runtime with no project-dir variable, which
        :func:`wiring.session_end_command` deliberately emits with no ``--cwd``.

        Empty is a different thing wearing the same clothes. Every hook rentctl
        writes for a runtime that *does* have such a variable interpolates it:

            rent down --all --cwd "$CLAUDE_PROJECT_DIR" --reason session-end

        If that variable is unset at hook time the shell expands it to ``""``, and
        an empty string is falsy — so ``cwd or self._caller_cwd()`` silently tore
        down whatever was leased to the *hook process's* directory instead of the
        project's. Usually the project root, so usually right by accident, and
        invisible when wrong. Worse, the teardown still recorded
        ``reason_source: declared``, manufacturing pilot evidence for a hook whose
        scoping never worked.

        This is the ADR-0008 fold in a third place: "not supplied" and "supplied,
        but the value evaporated" must not share an answer.
        """
        if cwd is not None and not cwd.strip():
            raise DevctlError(
                INVALID_CWD,
                '--cwd was given but empty — a shell variable that did not expand. '
                "Omit --cwd entirely to mean the caller's directory; passing an "
                "empty one is never what was intended.",
            )

    # ==================================================================
    # env_up
    # ==================================================================

    def env_up(
        self,
        project: str,
        lease_minutes: int = DEFAULT_LEASE_MINUTES,
        profile: str = "default",
        cwd: str | None = None,
    ) -> dict[str, Any]:
        try:
            self._check_cwd(cwd)
            minutes = self._clamp_lease(lease_minutes)
            registry = self._load_registry()
            entry = registry.entry(project)
            # The approved-command pin, checked before anything is started
            # (ADR-0003 §4). A repo whose devctl.toml changed since enrollment
            # stops here with CMD_CHANGED rather than executing the new command.
            # Unpinned and legacy entries pass through untouched.
            check_pin(entry)
            prof = entry.profile(profile)
            runner = self.runner_factory(entry.runner)
            # Identity is (project, cwd): each worktree of a project gets its own
            # lease, so a second lane starts its own server instead of being
            # handed this one's (ADR-0007).
            target_cwd = os.path.realpath(cwd or self._caller_cwd())
            key = lease_key(project, target_cwd)
            lease_path = self.paths.lease_file(key)

            with project_lock(self.paths.lock_file(project)):
                existing = Lease.read_if_exists(lease_path)
                if existing is not None and runner.alive(existing.process_handle()):
                    # A lease is keyed on (project, cwd) and carries no profile
                    # (ADR-0007), so one directory holds exactly one environment.
                    # Asking for a *different* profile here is a request the
                    # identity model cannot satisfy — and it used to be answered
                    # with `already_running: true` over the wrong server, so an
                    # agent that asked for the api profile got the plain dev one
                    # and debugged the wrong process. Refuse and say so.
                    if existing.profile != profile:
                        raise DevctlError(
                            PROFILE_MISMATCH,
                            f"{project!r} already has profile {existing.profile!r} running in "
                            f"this directory on port {existing.port}; {profile!r} was requested. "
                            f"One environment per directory (ADR-0007) — stop the running one "
                            f"first, or start this profile from a different worktree.",
                            running_profile=existing.profile,
                            requested_profile=profile,
                            port=existing.port,
                        )
                    # Reconcile: alive → renew and report already_running (§4.1, F3).
                    renewed = existing.renewed(self._now() + timedelta(minutes=minutes))
                    renewed.write(lease_path)
                    return self._up_result(renewed, already_running=True)
                if existing is not None:
                    lease_path.unlink(missing_ok=True)  # dead lease → clean, proceed

                # Draw a port from the block. Held under the project lock, so two
                # concurrent env_up calls cannot draw the same one (ADR-0004 §1).
                port, squatter_unverified = self._draw_port(
                    entry, prof.preferred_offset, key
                )

                # Where the process actually runs (ADR-0010). The registry holds
                # one cwd per profile — the main checkout — so without this every
                # lane serves the main checkout's files while holding a perfectly
                # correct per-lane lease and port. Re-rooting is refused unless
                # git proves the caller is a worktree of the same repo; the
                # fallback is the registry's own approved directory.
                spawn = resolve_spawn_cwd(prof.cwd, target_cwd)
                # The runner interface stays frozen (spec §6): it is handed a
                # profile and spawns in that profile's cwd, exactly as before.
                # Lane resolution belongs here, where target_cwd, the lease key
                # and the port draw already live.
                run_prof = replace(prof, cwd=spawn.cwd) if spawn.rerooted else prof

                now = self._now()
                log_path = self.paths.log_file(project, now.strftime("%Y-%m-%dT%H%M%S"))
                handle = runner.start(run_prof, port, log_path)

                # The process group is what tells our listener from a squatter:
                # `start_new_session=True` makes the spawned shell the group
                # leader, so vite/node children share its pgid.
                readiness = self._readiness(
                    port, self.readiness_timeout, procutil.process_group_of(handle.pid)
                )
                # Readiness establishes that the port is served. This establishes
                # that it is served by US. Without it, a foreign listener
                # answering loopback satisfies the probe while our own command
                # dies on EADDRINUSE, and env_up returns ok:true over a lease
                # naming a pid that is already gone — which every later
                # reconcile then reads as a crashed server of ours. Checked
                # before the readiness verdict because a dead process settles
                # the question whoever is answering.
                if not runner.alive(handle):
                    raise DevctlError(
                        START_TIMEOUT,
                        f"{project!r} exited during startup on port {port} — "
                        f"anything answering there now belongs to another process",
                        port=port,
                        log_tail=_log_tail(str(log_path)),
                    )
                if not readiness.is_up:
                    runner.stop(handle)
                    raise DevctlError(
                        START_TIMEOUT,
                        f"{project!r} did not answer on port {port} within "
                        f"{self.readiness_timeout:.0f}s, and nothing of ours is "
                        f"listening on it",
                        port=port,
                        log_tail=_log_tail(str(log_path)),
                    )

                lease = Lease(
                    project=project,
                    profile=profile,
                    runner=entry.runner,
                    handle=handle.to_dict(),
                    port=port,
                    session=self.session_id_fn(),
                    cwd=target_cwd,
                    created=now,
                    expires=now + timedelta(minutes=minutes),
                    log=str(log_path),
                    watchdog_pid=None,
                    spawn_cwd=spawn.cwd,
                )
                lease.write(lease_path)

                # The watchdog babysits a *lease*, not a project — with N lanes
                # per project, a project name would not say which one (ADR-0007 §5).
                wpid = self.watchdog_spawn(key)
                if wpid is not None:
                    lease = lease.with_watchdog(wpid)
                    lease.write(lease_path)
                return self._up_result(
                    lease,
                    already_running=False,
                    squatter_unverified=squatter_unverified,
                    readiness=readiness,
                )
        except DevctlError as e:
            # A refused start is evidence too: it separates "this project never
            # used rentctl" from "it tried and the port was squatted" (spec §9).
            self.events.record_up_failed(
                project, profile=profile, error=e.code, port=e.details.get("port")
            )
            return e.to_envelope()

    def _draw_port(
        self, entry: RegistryEntry, preferred_offset: int, own_key: str
    ) -> tuple[int, str | None]:
        """Pick a free port from the project's block, or fail loud (ADR-0004).

        "Free" is derived, never stored: a port is taken if a *live* lease of this
        project holds it, or if anything is listening on it. A dead lease's port
        is reclaimable immediately — the listener check is what stops us handing
        out a port some process still holds, whoever owns it.

        Returns ``(port, unverified_reason)``. ``unverified_reason`` is ``None``
        on the normal path and a string when the listener probe could not run
        (ADR-0008 §2): we still draw and still start, because a wrongly-drawn
        port already fails safely at bind time, but the blind spot travels with
        the result instead of being silently absorbed.
        """
        taken: dict[int, str] = {}
        for path in self.paths.project_lease_files(entry.project):
            if path.stem == own_key:
                continue
            other = self._read_lease_quiet(path)
            if other is not None and self._lease_alive(other):
                taken[other.port] = f"lease {path.stem} (pid {other.process_handle().pid})"
        unverified: str | None = None
        for port in entry.block_ports():
            if port in taken:
                continue
            try:
                owner = self._port_owner(port)
            except procutil.ProbeUnavailable as e:
                # Backend-level, not port-level: retrying the remaining ports
                # would fail identically. Stop probing and carry the reason.
                unverified = str(e)
                break
            if owner is not None:
                # Never killed, never drawn — F7's "don't touch what we don't own"
                # becomes "route around it" now that the port isn't fixed.
                taken[port] = f"pid {owner.pid} ({owner.name}), no rentctl lease"

        drawn = draw_port(entry.block, BLOCK_SIZE, preferred_offset, set(taken))
        if drawn is None:
            raise DevctlError(
                BLOCK_EXHAUSTED,
                f"every port in {entry.project!r}'s block "
                f"{entry.block}..{entry.block + BLOCK_SIZE - 1} is taken",
                block=entry.block,
                holders={str(p): who for p, who in sorted(taken.items())},
            )
        return drawn.port, unverified

    # ==================================================================
    # env_down
    # ==================================================================

    def env_down(
        self,
        project: str | None = None,
        cwd: str | None = None,
        reason: str | None = None,
        all_instances: bool = False,
    ) -> dict[str, Any]:
        """Stop this cwd's instance of a project, or everything leased to ``cwd``.

        ``reason`` is the caller declaring which cleanup layer it *is* — the
        SessionEnd hook passes ``session-end``, an LLM/human ``explicit``. When
        it's omitted the reason is inferred from the call shape and the event is
        stamped ``inferred``, because ``rent down --all`` typed by hand is
        indistinguishable from the hook and must not be counted as proof the
        hook fired (spec §11.1 G4).

        Naming a project tears down **this cwd's** instance, not every lane's —
        an LLM finishing with its own dev server must not reach into a sibling
        worktree (ADR-0007 §3). ``all_instances`` is the deliberate opt-in for
        "every lane's copy."
        """
        try:
            self._check_cwd(cwd)
            declared = reason is not None
            if project is not None:
                # Naming a project *is* the declaration: there is no other way to
                # read a targeted teardown than as the polite path (layer 1).
                why, source = reason or ev.EXPLICIT, ev.DECLARED
                with project_lock(self.paths.lock_file(project)):
                    if all_instances:
                        downed = [
                            self._down_lease(p, why, source, op=ev.DOWN)
                            for p in self.paths.project_lease_files(project)
                        ]
                        return {"ok": True, "project": project, "downed": downed}
                    target = os.path.realpath(cwd or self._caller_cwd())
                    path = self.paths.lease_file_for(project, target)
                    return {"ok": True, **self._down_lease(path, why, source, op=ev.DOWN, project=project)}

            # No project → down everything leased to the caller's cwd (§4.2).
            source = ev.DECLARED if declared else ev.INFERRED
            why = reason or ev.SESSION_END
            target = os.path.realpath(cwd or self._caller_cwd())
            downed = []
            for path in list_lease_files(self.paths.leases_dir):
                lease = self._read_lease_quiet(path)
                if lease is None or lease.cwd != target:
                    continue
                with project_lock(self.paths.lock_file(lease.project)):
                    downed.append(self._down_lease(path, why, source, op=ev.DOWN))
            return {"ok": True, "cwd": target, "downed": downed}
        except DevctlError as e:
            return e.to_envelope()

    def _down_lease(
        self,
        path: Path,
        reason: str,
        reason_source: str,
        *,
        op: str,
        project: str | None = None,
    ) -> dict[str, Any]:
        lease = Lease.read_if_exists(path) if path.exists() else None
        if lease is None:
            # Idempotent (§4.2), and deliberately unlogged: the SessionEnd hook
            # fires in every session, most of which leased nothing.
            return {"project": project or project_from_key(path.stem), "was_running": False}
        runner = self.runner_factory(lease.runner)
        handle = lease.process_handle()
        was_running = runner.alive(handle)
        runner.stop(handle)          # verify-then-kill; no-op if dead/recycled
        # Ask again. `stop()` returns None and cannot report what it achieved:
        # it swallows a failed signal, and it *computes* whether the process
        # actually died and then discards the answer. So `killed` used to be the
        # liveness reading taken BEFORE the attempt — a claim about intent, not
        # outcome. A process that survives SIGKILL (uninterruptible sleep on a
        # mounted volume is the realistic case here) produced `killed: true`,
        # false layer-2/3 evidence, and a deleted lease.
        #
        # `alive()` is already part of the frozen runner interface (spec §6), so
        # this needs no change to that contract — only the honesty of asking.
        still_alive = runner.alive(handle)
        killed = was_running and not still_alive

        if still_alive:
            # Do NOT unlink. The lease is the only record naming this process;
            # deleting it while the process lives is precisely what manufactures
            # an unattributable survivor holding a port. Keeping it means the
            # next sweep or watchdog tries again and the board still shows it.
            self.events.record_down(
                lease.project,
                op=op,
                reason=reason,
                reason_source=reason_source,
                killed=False,
                port=lease.port,
                pid=handle.pid,
                stop_failed=True,
            )
            return {
                "project": lease.project,
                "cwd": lease.cwd,
                "port": lease.port,
                "was_running": was_running,
                "stopped": False,
                "detail": (
                    f"pid {handle.pid} was signalled but is still alive; the lease is "
                    "kept so it stays attributable. Investigate before reusing the port."
                ),
            }

        self._kill_watchdog(lease)
        path.unlink(missing_ok=True)
        self.events.record_down(
            lease.project,
            op=op,
            reason=reason,
            reason_source=reason_source,
            killed=killed,           # a dead process was never signalled — not a kill
            port=lease.port,
            pid=handle.pid,
        )
        return {
            "project": lease.project,
            "cwd": lease.cwd,
            "port": lease.port,
            "was_running": was_running,
        }

    # ==================================================================
    # env_ls / env_sweep — shared reconcile engine
    # ==================================================================

    def env_ls(self) -> dict[str, Any]:
        try:
            _swept, kept = self._reconcile_all(op="ls")
            registry = self._safe_registry()
            environments = [self._ls_entry(l) for l in kept]
            squatters, sq_unverified = self._squatters(registry, kept)
            environments.extend(squatters)
            result: dict[str, Any] = {"ok": True, "environments": environments}
            if sq_unverified is not None:
                # ADR-0008 §3: a board with no squatter rows must not be readable
                # as "checked and clean" when the check could not run.
                result["squatter_check"] = "unavailable"
                result["squatter_check_detail"] = sq_unverified
            if registry is None:
                result["registry_error"] = "registry invalid — squatter detection skipped"
            return result
        except DevctlError as e:  # pragma: no cover - reconcile swallows lease errors
            return e.to_envelope()

    def env_sweep(self) -> dict[str, Any]:
        try:
            swept, kept = self._reconcile_all(op="sweep")
            registry = self._safe_registry()
            result: dict[str, Any] = {
                "ok": True,
                "swept": swept,
                "kept": [self._ls_entry(l) for l in kept],
            }
            squatters, sq_unverified = self._squatters(registry, kept)
            if squatters:
                if registry is not None and registry.enforcement == "strict":
                    result["killed_squatters"] = self._kill_squatters(squatters)
                else:
                    result["squatters"] = squatters  # advisory: report only (O4)
            if sq_unverified is not None:
                # ADR-0008 §3. Note this matters MORE under strict enforcement:
                # an unverified sweep that reported clean would be a licence to
                # believe the block is reclaimed when it was never inspected.
                result["squatter_check"] = "unavailable"
                result["squatter_check_detail"] = sq_unverified
            drift = self._pin_drift(registry)
            if drift:
                result["command_drift"] = drift
            if registry is None:
                result["registry_error"] = "registry invalid — squatter detection skipped"
            return result
        except DevctlError as e:  # pragma: no cover - reconcile swallows lease errors
            return e.to_envelope()

    def report_false_kill(
        self, project: str, *, note: str, port: int | None = None
    ) -> dict[str, Any]:
        """Record that a teardown killed something the user wanted (G3, WI-0016).

        Matched to the most recent ``down`` this could plausibly be about, so the
        report carries the disputed event rather than a timestamp someone has to
        correlate by hand later. A report matching nothing is still recorded —
        the user is the authority on having lost a process, and rentctl's failure
        to find the corresponding record is itself worth knowing about.

        Never refuses. A complaint channel that can reject the complaint is not a
        complaint channel; the whole point is that G3 has no other input.
        """
        matched = self._recent_kill(project, port)
        self.events.record_false_kill(project, note=note, port=port, matched=matched)
        return {
            "ok": True,
            "recorded": True,
            "project": project,
            "matched": matched,
            "note": (
                "Recorded. No teardown in the log matches this report — worth "
                "checking whether the process was rentctl's at all."
                if matched is None
                else "Recorded against the teardown shown."
            ),
        }

    def _recent_kill(self, project: str, port: int | None) -> dict[str, Any] | None:
        """The newest recorded teardown for this project, optionally on ``port``."""
        found = self.events.read(project=project)
        for event in reversed(found):
            if event.get("event") != ev.DOWN:
                continue
            if port is not None and event.get("port") != port:
                continue
            return event
        return None

    def _pin_drift(self, registry: Registry | None) -> list[dict[str, Any]]:
        """Projects whose ``devctl.toml`` no longer matches the approved pin.

        ADR-0003's third call site. `env_up` **refuses** on drift, which is right
        — it is about to execute the changed command. Sweep must only **report**
        it: sweep is the cleanup path, and refusing to reconcile because a config
        drifted would leave running servers alive to protect against a command it
        was never going to run. Fail-closed and fail-safe point opposite ways
        here, and cleanup follows fail-safe.

        The value is timing. Drift is otherwise invisible until someone tries to
        start the environment and gets `CMD_CHANGED` at the moment they wanted to
        work; sweep runs at session *start*, so the same fact arrives when there
        is time to deal with it. `sync` is the fix, and the report names it.
        """
        if registry is None:
            return []
        drifted: list[dict[str, Any]] = []
        for name, entry in sorted(registry.projects.items()):
            try:
                check_pin(entry)
            except DevctlError as e:
                if e.code != CMD_CHANGED:
                    raise
                drifted.append(
                    {"project": name, "message": e.message, "fix": f"rent sync --path {entry.source_dir}"}
                )
        return drifted

    def _reconcile_all(self, *, op: str) -> tuple[list[dict[str, Any]], list[Lease]]:
        """Apply the pure plan to every lease under locks. Returns (swept, kept).

        Both reconcile outcomes are recorded as cleanup layer 4 (spec §8) — the
        reconciler knows exactly what it did, so nothing here is inferred.
        """
        now = self._now()
        swept: list[dict[str, Any]] = []
        kept: list[Lease] = []
        for path in list_lease_files(self.paths.leases_dir):
            # Lock by the project half of the key — available without reading the
            # file, so lock-then-read still holds. The parsed lease's `project` is
            # what gets reported (ADR-0007 §6).
            with project_lock(self.paths.lock_file(project_from_key(path.stem))):
                lease = self._read_lease_quiet(path)
                if lease is None:
                    continue  # corrupt/vanished — leave it; squatter check surfaces the server
                verdict = decide(lease, self._lease_alive, now)
                entry = {"project": lease.project, "cwd": lease.cwd, "port": lease.port}
                if verdict.action is Action.KEEP:
                    kept.append(lease)
                elif verdict.action is Action.CLEAN:
                    path.unlink(missing_ok=True)
                    self._record_reconcile(lease, op, ev.SWEEP_DEAD, killed=False)
                    swept.append({**entry, "action": "clean", "reason": verdict.reason})
                else:  # EXPIRE
                    stopped = self._stop_lease(lease, path)
                    # `killed=stopped`, not `killed=True`. Layer-4 evidence must
                    # describe what happened, not what was attempted.
                    self._record_reconcile(
                        lease, op, ev.SWEEP_EXPIRED, killed=stopped, stop_failed=not stopped
                    )
                    if stopped:
                        swept.append({**entry, "action": "expire", "reason": verdict.reason})
                    else:
                        # Not swept — it is still running and still leased. Reporting
                        # it as swept is how a live server becomes invisible.
                        kept.append(lease)
        return swept, kept

    def _record_reconcile(
        self, lease: Lease, op: str, reason: str, *, killed: bool, stop_failed: bool = False
    ) -> None:
        self.events.record_down(
            lease.project,
            op=op,
            reason=reason,
            reason_source=ev.DECLARED,
            killed=killed,
            stop_failed=stop_failed or None,
            port=lease.port,
            pid=lease.process_handle().pid,
        )

    # ==================================================================
    # helpers
    # ==================================================================

    def _load_registry(self) -> Registry:
        return Registry.load(self.paths.registry_file)

    def _safe_registry(self) -> Registry | None:
        try:
            return self._load_registry()
        except DevctlError:
            return None

    def _lease_alive(self, lease: Lease) -> bool:
        return self.runner_factory(lease.runner).alive(lease.process_handle())

    def _stop_lease(self, lease: Lease, path: Path) -> bool:
        """Stop a lease's process. Returns whether it is actually gone.

        The lease is kept when the process survives — see :meth:`_down_lease`
        for why: the lease is the only record naming it, so unlinking a lease
        over a living process is what produces an unattributable survivor.
        """
        runner = self.runner_factory(lease.runner)
        handle = lease.process_handle()
        runner.stop(handle)
        if runner.alive(handle):
            return False
        self._kill_watchdog(lease)
        path.unlink(missing_ok=True)
        return True

    def _kill_watchdog(self, lease: Lease) -> None:
        if lease.watchdog_pid is None:
            return
        try:
            os.kill(lease.watchdog_pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass  # already gone, or not ours — the lease removal makes it self-exit anyway

    def _read_lease_quiet(self, path: Path) -> Lease | None:
        try:
            return Lease.read_if_exists(path)
        except DevctlError:
            return None  # corrupt lease — skip (F5); the server shows up as a squatter

    def _ls_entry(self, lease: Lease) -> dict[str, Any]:
        handle = lease.process_handle()
        alive = self.runner_factory(lease.runner).alive(handle)
        out = {
            "project": lease.project,
            "profile": lease.profile,
            "cwd": lease.cwd,          # which lane this instance belongs to (ADR-0007)
            "port": lease.port,
            "pid": handle.pid,
            "runner": lease.runner,
            "session": lease.session,
            "started": lease.created.isoformat(),
            "lease_expires": lease.expires.isoformat(),
            "healthy": alive and self._answering(lease.port),
        }
        if lease.spawn_cwd is not None and lease.spawn_cwd != lease.cwd:
            # Only when it differs: on the board, N identical rows would bury
            # the one lane whose server is serving somewhere else (ADR-0010).
            out["serving"] = lease.spawn_cwd
        return out

    def _squatters(
        self, registry: Registry | None, kept: list[Lease]
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Listeners inside a claimed block with no lease behind them (ADR-0004 §6).

        Ownership is by block range, not by enumerating declared offsets — a
        squatter on ``block + 7`` of a single-profile project used to be
        invisible here.

        Returns ``(rows, unverified_reason)``. Per ADR-0008 §3 an unrunnable probe
        must not render as a clean board, and the reason is returned **beside**
        the list rather than as a row in it: ``_kill_squatters`` calls
        ``os.kill(row["pid"])`` on every element, so a synthetic
        "couldn't check" row would either crash it or — given a placeholder pid —
        get something killed. A list whose consumer kills its contents may only
        ever hold real, attributed processes.
        """
        if registry is None:
            return [], None
        kept_ports = {l.port for l in kept}
        out: list[dict[str, Any]] = []
        for port, project in sorted(registry.port_owner_map().items()):
            if port in kept_ports:
                continue
            try:
                owner = self._port_owner(port)
            except procutil.ProbeUnavailable as e:
                # Backend-level: the remaining ports would fail identically, and
                # a partial sweep reported as complete is the very fold ADR-0008
                # removes. Surface what could not be checked.
                return out, str(e)
            if owner is not None:
                out.append(
                    {
                        "project": project,
                        "port": port,
                        "pid": owner.pid,
                        "name": owner.name,
                        "status": "squatter",
                    }
                )
        return out, None

    def _kill_squatters(self, squatters: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Strict-enforcement only (earned at the pilot-exit gate). A squatter is a
        # listener on OUR registered block port with no lease — under strict we own
        # the block and reclaim it.
        killed = []
        for sq in squatters:
            pid = sq["pid"]
            try:
                os.kill(pid, signal.SIGTERM)
                killed.append({**sq, "killed": True})
            except (ProcessLookupError, PermissionError):
                killed.append({**sq, "killed": False})
        return killed

    def _up_result(
        self,
        lease: Lease,
        *,
        already_running: bool,
        squatter_unverified: str | None = None,
        readiness: Readiness = Readiness.NOT_PROBED,
    ) -> dict[str, Any]:
        """Render the ``env_up`` envelope — and record the event.

        Both success paths (fresh start, and renew-an-already-running lease)
        converge here, which is why the event is emitted from the formatter
        rather than duplicated at each return.

        ``squatter_unverified`` carries ADR-0008 §2's blind spot: when set, the
        port was drawn without a working listener check, and the envelope says so
        rather than presenting the draw as fully verified. The renew path leaves
        it ``None`` because it draws no port at all.
        """
        self.events.record_up(
            lease.project,
            profile=lease.profile,
            port=lease.port,
            pid=lease.process_handle().pid,
            session=lease.session,
            cwd=lease.cwd,
            spawn_cwd=(lease.spawn_cwd if lease.spawn_cwd != lease.cwd else None),
            lease_expires=lease.expires.isoformat(),
            already_running=already_running,
        )
        out: dict[str, Any] = {
            "ok": True,
            "project": lease.project,
            # Which profile is actually serving. Named on every start for the same
            # reason as `serving` below: the envelope previously carried no profile
            # at all, so a caller had nothing to check its assumption against and a
            # substitution was indistinguishable from the thing it asked for.
            "profile": lease.profile,
            "url": f"http://localhost:{lease.port}",
            "port": lease.port,
            "pid": lease.process_handle().pid,
            "lease_expires": lease.expires.isoformat(),
            "already_running": already_running,
        }
        if lease.spawn_cwd is not None:
            # Which directory is actually being served. Named on every start,
            # not only on re-rooted ones: "you are being served the checkout you
            # are standing in" is exactly as worth saying as the alternative,
            # and a field that appears only in the surprising case is a field
            # nobody learns to read.
            out["serving"] = lease.spawn_cwd
        if squatter_unverified is not None:
            out["squatter_check"] = "unavailable"
            out["squatter_check_detail"] = squatter_unverified
        # Emitted on every start, not only the surprising ones — same argument as
        # `serving` above. A field that appears only when something is odd is a
        # field nobody learns to read, and this one qualifies the `url` directly.
        out["readiness"] = readiness.value
        if readiness is Readiness.LISTENING:
            out["readiness_detail"] = (
                "started and listening, but not on loopback — it has bound a "
                f"specific address, so {out['url']} will not reach it"
            )
        elif readiness is Readiness.UNKNOWN:
            out["readiness_detail"] = (
                "could not confirm it came up: the listener probe could not run. "
                "The lease was written anyway, so the process is tracked and will "
                "be swept rather than orphaned"
            )
        return out

    @staticmethod
    def _clamp_lease(minutes: int) -> int:
        return max(1, min(int(minutes), MAX_LEASE_MINUTES))

    @staticmethod
    def _caller_cwd() -> str:
        """The project directory the caller is working in (ADR-0011 §4).

        rentctl's own variable wins, then each known runtime's, then the process
        working directory. Reading only ``CLAUDE_PROJECT_DIR`` was not a
        cosmetic coupling: on any other runtime it silently fell through to
        ``os.getcwd()``, which is right for a hook and wrong for an MCP server
        whose working directory is wherever the runtime happened to launch it.
        """
        return _first_env(PROJECT_DIR_ENVS) or os.getcwd()

    @staticmethod
    def _caller_session() -> str:
        """The session id to attribute a lease to (ADR-0011 §4).

        Returns :data:`~rentctl.core.events.UNATTRIBUTED` when none of
        :data:`SESSION_ID_ENVS` is set in this process's environment. That is a
        statement about what rentctl could read and nothing more.

        The previous wording here called it "the honest answer for a runtime that
        exposes no session identity." It was not honest, because it was not true:
        Claude Code did expose one and rentctl was reading a name nothing sets. A
        docstring that certifies a conclusion the code cannot support is worse
        than a missing field — every reader of the log was told the runtime had
        been asked and had nothing to say.
        """
        return _first_env(SESSION_ID_ENVS) or ev.UNATTRIBUTED

    def _spawn_watchdog(self, key: str) -> int | None:
        """Spawn ``rentctl.watchdog <lease-key>`` detached (§6.2). Returns its pid."""
        proc = subprocess.Popen(
            [sys.executable, "-m", "rentctl.watchdog", key],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env={**os.environ},
        )
        return proc.pid
