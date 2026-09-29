# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Drake

"""The four operations, shared by the MCP server and the CLI (spec §4).

Every method returns a plain JSON-able dict: ``{"ok": true, ...}`` on success or
the ``{"ok": false, "error": ...}`` envelope on a :class:`DevctlError`. The MCP
and CLI shells are thin wrappers that just serialize what these return.

**Since 1.1 a per-lease supervisor owns every environment (ADR-0016).** This
module no longer starts, stops or watches a workload itself. It writes lease
transitions under the project lock L, spawns and wakes supervisors, and then
*waits without holding any lock* for the outcome the supervisor writes:

* ``env_up`` writes a ``starting`` lease with a fresh generation *before*
  anything is spawned, spawns the supervisor and records it, releases L, and
  waits for ``running`` or ``startup_failed`` (§6). A workload never exists
  without a lease naming its session.
* ``env_down`` writes a generation-matched stop request under L, wakes the
  supervisor, releases L, and waits up to its budget (§8). The answer is one of
  three shapes, each exactly what the lease says: ``stopped: true``,
  ``cleanup_incomplete`` with survivors, or ``pending``. The SessionEnd hook
  sends every request and returns without waiting (R0): the supervisor finishes
  cleanup whether or not anyone stays to watch.
* ``env_ls``/``env_sweep`` apply the §10 table through
  ``supervision.recover_lease``: a dead supervisor's workload is marked
  ``unsupervised`` and kept serving, recovered by a recovery claim on expiry or
  ``down``, and records nobody will act on are cleaned.

A lease with no supervisor — a 1.0.x (legacy) lease, or one whose supervisor
died — is stopped in *recovery mode*: in-process when the caller's budget can
cover a full grace period, otherwise by a detached recovery supervisor so a
1.5 s hook never abandons a stop halfway.

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
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import psutil

from . import events as ev
from . import procutil, supervision
from .enroll import check_pin
from .errors import (
    BLOCK_EXHAUSTED,
    CLEANUP_INCOMPLETE,
    CMD_CHANGED,
    INVALID_CWD,
    PROFILE_MISMATCH,
    START_TIMEOUT,
    STATE_WRITE_FAILED,
    STOP_IN_PROGRESS,
    SUPERVISOR_START_FAILED,
    UNSUPPORTED_ENVIRONMENT,
    DevctlError,
)
from .events import EventLog
from .leases import CleanupRecord, Lease, ProcessRef, SupervisorRef, list_lease_files
from .lifecycle import (
    REGISTRATION_TIMEOUT,
    REMOVED_ON_REACH,
    TERMINAL,
    Actor,
    ActorKind,
    Event,
    EventKind,
    LifecycleAction,
    Observation,
    StartWait,
    State,
    StopWait,
    decide,
    generation_of,
    holds_port,
    may_remove,
    new_starting_lease,
    start_wait_outcome,
    state_of,
    stop_wait_outcome,
    transition,
)
from .locking import project_lock
from .models import ProcInfo, Readiness
from .paths import DevctlPaths, lease_key, project_from_key
from .ports import draw_port
from .procutil import Membership, ProcessTable
from .registry import BLOCK_SIZE, Registry, RegistryEntry
from .runners import Runner, get_runner
from .supervision import (
    RECOVERED_CLEANED,
    RECOVERED_STARTUP_FAILED,
    RECOVERED_STOPPED,
    RecoveryResult,
)
from .workload import KILL_GRACE_S, TERM_GRACE_S
from .worktree import resolve_spawn_cwd

DEFAULT_LEASE_MINUTES = 120
MAX_LEASE_MINUTES = 480
_LOG_TAIL_LINES = 40

# --- waiting (ADR-0016 §6, §8, R0, R6) -------------------------------------------
# An explicit `down` (CLI or MCP) waits this long for the verified outcome: term
# grace 10 s + kill grace 2 s + slack. Past it the answer is `pending`, which is
# `ok: true` because cleanup completes regardless.
DOWN_WAIT_S = 15.0
# The SessionEnd hook's budget is 1.5 s for the whole hook on a plugin install
# (R0), so a declared session-end sends every request and returns.
SESSION_END_WAIT_S = 0.0
# How long an `up` that finds a lease `stopping` waits before STOP_IN_PROGRESS.
UP_STOPPING_WAIT_S = 15.0
# How often a lock-free waiter re-reads the lease (§8).
POLL_S = 0.1
# How often a waiter re-checks that the process it is waiting on is alive.
LIVENESS_CHECK_S = 1.0
# Slack on top of a full grace period for an in-process recovery stop to count
# as fitting inside a caller's budget.
_INLINE_RECOVERY_SLACK_S = 1.0
# Guard on the `up` loop: each round either returns, waits, or reconciles.
_UP_ROUNDS = 4

# The legacy 1.0.x watchdog's cmdline markers (ADR-0016 §3's legacy-watchdog row).
_WATCHDOG_MARKERS = ("rentctl.watchdog", "devctl-watchdog", "rent-watchdog")

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


# Both loopbacks, because `localhost` is not one address. Node >= 17 resolves it
# to ::1 first on macOS, so a Vite server on defaults listens on IPv6 loopback
# only — and a probe that dialled 127.0.0.1 alone reported it "not on loopback,
# http://localhost will not reach it" (false) and `healthy: false` (WI-0072).
# IPv4 goes first: it is what most servers bind, so the common path stays one
# connect. The per-family timeout is half the old single one, so the worst case
# is still bounded by the same second.
_LOOPBACKS = ("127.0.0.1", "::1")
_LOOPBACK_CONNECT_TIMEOUT_S = 0.5


def _port_answering(port: int) -> bool:
    """Whether anything accepts a TCP connect on ``port`` at either loopback.

    A host without IPv6 loopback fails the ``::1`` dial with an ``OSError``
    like any refused connect, which is the right answer: nothing answers there.
    The supervisor's readiness probe imports this, so there is one dialler.
    """
    for host in _LOOPBACKS:
        try:
            with socket.create_connection((host, port), timeout=_LOOPBACK_CONNECT_TIMEOUT_S):
                return True
        except OSError:
            continue
    return False


def _log_tail(path: str, n: int = _LOG_TAIL_LINES) -> list[str]:
    try:
        return Path(path).read_text(errors="replace").splitlines()[-n:]
    except OSError:  # pragma: no cover - defensive
        return []


class OsSupervision:
    """The machine-touching half of supervision, as one seam (ADR-0016 §6, §8, R0).

    Everything the service does to a *process* — spawning a supervisor, asking
    whether one is alive, waking it, scanning a session — goes through here, so
    a test can substitute a fake that drives the real lifecycle over a fake
    process table without a single real process. One object rather than five
    callables on purpose: a half-injected fake (fake spawner, real liveness
    check) would scan the real machine for a fake pid's session.

    The ``supervision`` functions are looked up at call time, not bound at
    import, so a test guard that wraps ``spawn_supervisor`` sees every spawn.
    """

    table: ProcessTable | None = None  # None: the real process table

    def spawn(self, key: str, generation: str, *, paths: DevctlPaths) -> SupervisorRef:
        return supervision.spawn_supervisor(key, generation, paths=paths)

    def spawn_recovery(
        self, key: str, generation: str, *, paths: DevctlPaths, reason: str, reason_source: str, op: str
    ) -> SupervisorRef:
        return supervision.spawn_supervisor(
            key, generation, paths=paths, recover=True, reason=reason,
            reason_source=reason_source, op=op,
        )

    def alive(self, ref: SupervisorRef | ProcessRef, key: str) -> bool:
        return supervision.supervisor_alive(ref, key)

    def wake(self, ref: SupervisorRef | ProcessRef, key: str) -> bool:
        return supervision.wake_supervisor(ref, key)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def monotonic(self) -> float:
        return time.monotonic()


class _Reported(DevctlError):
    """A failure whose ``up_failed`` event the supervisor already wrote.

    The waiting ``up`` reads it from the lease (the agreement rule, §13) and
    must not record it a second time.
    """


@dataclass
class _Step:
    """What one locked round of ``env_up`` decided."""

    kind: str                           # result | started | wait_start | wait_stop | reconcile
    result: dict[str, Any] | None = None
    lease: Lease | None = None
    generation: str | None = None
    squatter_unverified: str | None = None


@dataclass(frozen=True)
class _Squatter:
    """A listener on a registered block port with no lease behind it, as the
    lock-free snapshot saw it. ``start_time``/``sid`` are ``None`` when the
    process could not be read; such a row is reported but never reclaimed."""

    project: str
    port: int
    pid: int
    name: str
    start_time: float | None
    sid: int | None

    def row(self) -> dict[str, Any]:
        """The board row — the same shape 1.0.x reported."""
        return {
            "project": self.project,
            "port": self.port,
            "pid": self.pid,
            "name": self.name,
            "status": "squatter",
        }


# Strict-mode reclaim (ADR-0016 §11). The event kind lives here, not in
# ``events``, until step 10 folds it into the §13 vocabulary; it is never a
# ``down`` and never counts as a teardown.
_SQUATTER_RECLAIM = "squatter_reclaim"
# ``squatter_reclaim.outcome`` for a row refused before any signal. A row that
# reached ``verified_signal`` carries its ``SignalResult`` value instead.
_RECLAIM_LEASED = "leased"                          # a non-terminal lease holds the port
_RECLAIM_LEASE_SESSION = "lease_session"            # the listener is in a lease's session
_RECLAIM_LEASE_UNREADABLE = "lease_unreadable"      # a project lease could not be read
_RECLAIM_UNVERIFIABLE = "unverifiable"              # no start time / session at snapshot
_RECLAIM_PROBE_UNAVAILABLE = "probe_unavailable"    # the re-probe under L could not run
_RECLAIM_GONE = "gone"                              # nothing listens there any more
_RECLAIM_LISTENER_CHANGED = "listener_changed"      # a different pid listens now


@dataclass
class _StopTarget:
    """One lease a ``down`` is acting on, from request to answer."""

    path: Path
    key: str
    project: str
    cwd: str | None = None
    port: int | None = None
    generation: str | None = None
    was_running: bool = False
    executor: str | None = None               # "supervisor" | "recovery" | None
    executor_ref: SupervisorRef | ProcessRef | None = None
    needs_recovery: bool = False
    legacy: Lease | None = None               # the pre-request legacy lease, for its watchdog
    # Set when the lease was already `cleanup_incomplete`: the request asks for
    # a retry (§8), and only an attempt past this count answers it.
    retry_after_attempt: int | None = None
    row: dict[str, Any] | None = None         # set once the answer is known
    extra: dict[str, Any] = field(default_factory=dict)


class Service:
    """Stateless orchestration of the four tools over the on-disk state.

    All impure collaborators (clock, supervision, session id, port probes) are
    injectable so the operations can be driven fast and deterministically in
    tests.
    """

    def __init__(
        self,
        paths: DevctlPaths | None = None,
        *,
        now_fn: Callable[[], datetime] = _now_local,
        runner_factory: Callable[[str], Runner] = get_runner,
        readiness_timeout: float = 30.0,
        session_id_fn: Callable[[], str] | None = None,
        event_log: EventLog | None = None,
        port_owner_fn: Callable[[int], ProcInfo | None] | None = None,
        port_answering_fn: Callable[[int], bool] | None = None,
        supervision: OsSupervision | None = None,
        term_grace_s: float = TERM_GRACE_S,
        kill_grace_s: float = KILL_GRACE_S,
    ) -> None:
        self.paths = paths or DevctlPaths.default()
        self.paths.ensure_dirs()
        self._now = now_fn
        # Kept to validate the registry's `runner` name: only `process` exists,
        # and `compose` must still fail loudly rather than start something else.
        # Workloads themselves are launched by the supervisor, not a runner.
        self.runner_factory = runner_factory
        # Travels to the supervisor in the lease's plan, because readiness is now
        # probed there (§6 step 3), not here.
        self.readiness_timeout = readiness_timeout
        self.session_id_fn = session_id_fn or self._caller_session
        self.events = event_log or EventLog(self.paths.events_file, now_fn=now_fn)
        self.supervision = supervision or OsSupervision()
        # The grace periods of every stop this service causes: they ride in the
        # plan to the supervisor, and bound an in-process recovery stop.
        self.term_grace_s = term_grace_s
        self.kill_grace_s = kill_grace_s
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
        # opens a real socket to report `healthy`. Seaming only the owner probe
        # left `env_ls` still asking the machine — and it reported a fake
        # environment as healthy because the pilot's real server happened to be
        # answering on that port.
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
    # the supervision seam, as the service uses it
    # ==================================================================

    def _observe(self, lease: Lease, key: str) -> Observation:
        return supervision.observe(
            lease, key, table=self.supervision.table, alive_fn=self.supervision.alive
        )

    def _recover(
        self,
        key: str,
        generation: str,
        *,
        actor: str,
        reason: str | None = None,
        reason_source: str = ev.DECLARED,
        op: str | None = None,
    ) -> RecoveryResult:
        """Run §10 for one lease, in this process (``supervision.recover_lease``).

        It takes L for its own bounded sections and stops *without* it, so no
        caller may hold L here (Rule 1: never nested).
        """
        return supervision.recover_lease(
            key, generation, paths=self.paths, reason=reason, reason_source=reason_source,
            op=op, actor_label=actor, events=self.events, now_fn=self._now,
            term_grace_s=self.term_grace_s, kill_grace_s=self.kill_grace_s,
            table=self.supervision.table, alive_fn=self.supervision.alive,
        )

    def _inline_recovery_fits(self, remaining_s: float) -> bool:
        """Can an in-process recovery stop finish inside ``remaining_s``?

        Only then is it run here. Otherwise a detached recovery supervisor runs
        it, so a caller that leaves early never abandons a stop halfway (R0).
        """
        return remaining_s >= self.term_grace_s + self.kill_grace_s + _INLINE_RECOVERY_SLACK_S

    @staticmethod
    def _me() -> ProcessRef:
        return supervision.self_ref()

    def _lock(self, key: str):
        return project_lock(self.paths.lock_file(project_from_key(key)))

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
            self.runner_factory(entry.runner)  # only `process` runs; compose fails loud
            self._require_process_inspection()
            # Identity is (project, cwd): each worktree of a project gets its own
            # lease, so a second lane starts its own server instead of being
            # handed this one's (ADR-0007).
            target_cwd = os.path.realpath(cwd or self._caller_cwd())
            key = lease_key(project, target_cwd)
            return self._up(entry, prof, profile, minutes, target_cwd, key)
        except _Reported as e:
            return e.to_envelope()
        except DevctlError as e:
            # A refused start is evidence too: it separates "this project never
            # used rentctl" from "it tried and the port was squatted" (spec §9).
            self.events.record_up_failed(
                project, profile=profile, error=e.code, port=e.details.get("port"),
                generation=e.details.get("generation"), phase=e.details.get("phase"),
            )
            return e.to_envelope()

    def _require_process_inspection(self) -> None:
        """Refuse to start anything where the process table cannot be read.

        A workload launched where its session cannot be enumerated could never
        be verified stopped: every later scan would be unknown. So the check runs
        before any lease is written or any process spawned, and says where rentctl
        has to run instead — it never asks for broader privileges.
        """
        denied = procutil.inspection_denied(self.supervision.table)
        if denied is not None:
            raise DevctlError(
                UNSUPPORTED_ENVIRONMENT,
                "rentctl cannot read the process table here, so it could not verify "
                f"that anything it starts is later stopped ({denied}). Nothing was "
                "started. Run rentctl where process inspection works: the MCP server "
                "launched by the agent host, or a terminal CLI — not a CLI invoked "
                "from inside an agent's sandbox.",
                reason=denied,
            )

    def _up(self, entry, prof, profile, minutes, target_cwd, key) -> dict[str, Any]:
        """The §6 protocol. Every locked round is bounded; every wait is lock-free."""
        for _ in range(_UP_ROUNDS):
            with self._lock(key):
                step = self._up_locked(entry, prof, profile, minutes, target_cwd, key)
            # --- L released: nothing below holds a lock (§9 Rule 2) ---------
            if step.kind == "result":
                assert step.result is not None
                return step.result
            if step.kind == "started":
                assert step.lease is not None
                return self._await_own_start(key, step.lease, step.squatter_unverified)
            assert step.generation is not None
            if step.kind == "wait_start":
                # §14: `up` on a `starting` lease waits for it; the next round
                # then renews what it became, or starts fresh if it went away.
                self._await_other_start(key, step.generation)
            elif step.kind == "wait_stop":
                # §14: `up` on a `stopping` lease waits up to 15 s, then starts
                # fresh with a new generation, or reports STOP_IN_PROGRESS.
                target = _StopTarget(self.paths.lease_file(key), key, entry.project,
                                     generation=step.generation)
                deadline = self.supervision.monotonic() + UP_STOPPING_WAIT_S
                self._await_stops([target], deadline, ev.EXPLICIT, ev.DECLARED, actor=ev.ACTOR_UP)
                if target.row is not None and target.row.get("state") == State.CLEANUP_INCOMPLETE.value:
                    raise self._cleanup_incomplete_error(target.row)
                if target.row is None or target.row.get("pending"):
                    raise DevctlError(
                        STOP_IN_PROGRESS,
                        f"{entry.project!r} in this directory is still stopping after "
                        f"{UP_STOPPING_WAIT_S:.0f}s; try again when `rent ls` no longer shows it",
                        state=State.STOPPING.value,
                    )
            else:  # reconcile: §10 for this lease, which may stop it without L
                self._recover(key, step.generation, actor=ev.ACTOR_UP)
        raise DevctlError(
            STOP_IN_PROGRESS,
            f"{entry.project!r} in this directory is being recovered by another process; "
            "try again when `rent ls` shows it settled",
        )

    def _up_locked(self, entry, prof, profile, minutes, target_cwd, key) -> _Step:
        """One round under L: act on what is there, or write the starting lease."""
        lease_path = self.paths.lease_file(key)
        existing = Lease.read_if_exists(lease_path)
        if existing is not None:
            step = self._up_on_existing(existing, key, profile, minutes)
            if step is not None:
                return step

        # Draw a port from the block. Held under the project lock, so two
        # concurrent env_up calls cannot draw the same one (ADR-0004 §1). The
        # `lsof` it may run is §9's one bounded exception to Rule 2.
        port, squatter_unverified = self._draw_port(entry, prof.preferred_offset, key)

        # Where the process actually runs (ADR-0010). The registry holds one cwd
        # per profile — the main checkout — so without this every lane serves the
        # main checkout's files while holding a perfectly correct per-lane lease
        # and port. Re-rooting is refused unless git proves the caller is a
        # worktree of the same repo; the fallback is the registry's own
        # approved directory.
        spawn = resolve_spawn_cwd(prof.cwd, target_cwd)
        run_cwd = spawn.cwd if spawn.rerooted else prof.cwd

        now = self._now()
        log_path = self.paths.log_file(entry.project, now.strftime("%Y-%m-%dT%H%M%S"))
        generation = uuid.uuid4().hex
        # The plan is everything the supervisor runs. It never re-reads the
        # registry, so what was approved and checked here is what executes.
        plan = {
            "cmd": prof.cmd,
            "cwd": run_cwd,
            "port_env": prof.port_env,
            "log": str(log_path),
            "readiness_timeout_s": float(self.readiness_timeout),
            "term_grace_s": float(self.term_grace_s),
            "kill_grace_s": float(self.kill_grace_s),
        }
        lease = new_starting_lease(
            generation=generation, project=entry.project, profile=profile, runner=entry.runner,
            port=port, session=self.session_id_fn(), cwd=target_cwd, spawn_cwd=spawn.cwd,
            log=str(log_path), plan=plan, now=now, expires=now + timedelta(minutes=minutes),
        )
        # §6 step 1: the record comes first. Nothing is launched until it exists.
        try:
            lease.write(lease_path)
        except OSError as e:
            raise DevctlError(
                STATE_WRITE_FAILED, f"could not write the starting lease: {e}",
                port=port, generation=generation, phase=ev.PHASE_STATE_WRITE,
            ) from e
        # §6 step 2, still under L: spawn, then record who was spawned. We are
        # its parent here, so its pid cannot be recycled before we name it.
        try:
            ref = self.supervision.spawn(key, generation, paths=self.paths)
        except DevctlError as e:
            lease_path.unlink(missing_ok=True)  # ours: written a moment ago under this L
            raise DevctlError(
                SUPERVISOR_START_FAILED, e.message, port=port, generation=generation,
                phase=ev.PHASE_SPAWN,
            ) from e
        spawned = transition(
            lease, Event(EventKind.SPAWNED, self._now(), supervisor=ref),
            Actor(ActorKind.CLI, generation, self._me()),
        )
        if isinstance(spawned, Lease):
            try:
                spawned.write(lease_path)
                lease = spawned
            except OSError:
                # The supervisor registers itself either way (§6); an unrecorded
                # spawn only costs the reconciler its liveness hint.
                pass
        return _Step("started", lease=lease, squatter_unverified=squatter_unverified)

    def _up_on_existing(self, existing: Lease, key: str, profile: str, minutes: int) -> _Step | None:
        """Decide what an existing lease means for this ``up``. ``None``: it was
        removed under L and a fresh start may proceed."""
        state = state_of(existing)
        gen = generation_of(existing)
        path = self.paths.lease_file(key)
        if state in TERMINAL:
            # stopped/exited/abandoned left behind by a crash between event and
            # unlink, or a startup_failed nobody consumed: it holds no port and
            # names nothing running, so a new generation replaces it.
            path.unlink(missing_ok=True)
            return None
        if state is State.CLEANUP_INCOMPLETE:
            # Replacing this record would lose the only record naming the
            # survivors (§14).
            raise self._cleanup_incomplete_error(self._incomplete_row(existing))

        obs = self._observe(existing, key)
        if existing.is_legacy:
            if obs.membership is Membership.MEMBERS:
                self._check_profile(existing, profile)
                return self._renew(existing, minutes, key)
            if obs.membership is Membership.AMBIGUOUS:
                raise self._ambiguous_error(existing)
            return _Step("reconcile", generation=gen)
        if state is State.RUNNING:
            if not obs.supervisor_alive:
                return _Step("reconcile", generation=gen)
            self._check_profile(existing, profile)
            return self._renew(existing, minutes, key)
        if state is State.UNSUPERVISED:
            d = decide(existing, obs, self._now())
            if d.action is LifecycleAction.KEEP_AMBIGUOUS:
                raise self._ambiguous_error(existing)
            if d.action is not LifecycleAction.KEEP:
                return _Step("reconcile", generation=gen)
            # Still serving, within its lease, and nobody supervises it. §7 has
            # no renew row for it: it runs out its lease and is then recovered.
            self._check_profile(existing, profile)
            return _Step("result", result=self._up_result(
                existing, already_running=True, record=True, detail=(
                    "running unsupervised: its supervisor died, so this lease was not "
                    "renewed. It is stopped at its expiry by the next ls/sweep/up/down, "
                    "or now with `rent down`."
                ),
            ))
        d = decide(existing, obs, self._now())
        if state is State.STARTING:
            if d.action is not LifecycleAction.KEEP:
                return _Step("reconcile", generation=gen)
            self._check_profile(existing, profile)
            return _Step("wait_start", generation=gen)
        # stopping
        if d.action is LifecycleAction.KEEP:
            return _Step("wait_stop", generation=gen)
        return _Step("reconcile", generation=gen)

    def _renew(self, existing: Lease, minutes: int, key: str) -> _Step:
        """Renew a live lease under L (§7 ``running`` + RENEW; §14 legacy in place)."""
        renewed = transition(
            existing,
            Event(EventKind.RENEW, self._now(), expires=self._now() + timedelta(minutes=minutes)),
            Actor(ActorKind.CLI, generation_of(existing), self._me()),
        )
        if not isinstance(renewed, Lease):
            # A stop is recorded for this generation: the stop was decided first,
            # so wait for it and start fresh rather than resurrect it (#10).
            return _Step("wait_stop", generation=generation_of(existing))
        renewed.write(self.paths.lease_file(key))
        return _Step("result", result=self._up_result(renewed, already_running=True, record=True))

    @staticmethod
    def _check_profile(existing: Lease, profile: str) -> None:
        # A lease is keyed on (project, cwd) and carries no profile (ADR-0007),
        # so one directory holds exactly one environment. Asking for a
        # *different* profile here is a request the identity model cannot
        # satisfy — and it used to be answered with `already_running: true`
        # over the wrong server, so an agent that asked for the api profile got
        # the plain dev one and debugged the wrong process. Refuse and say so.
        if existing.profile != profile:
            raise DevctlError(
                PROFILE_MISMATCH,
                f"{existing.project!r} already has profile {existing.profile!r} running in "
                f"this directory on port {existing.port}; {profile!r} was requested. "
                f"One environment per directory (ADR-0007) — stop the running one "
                f"first, or start this profile from a different worktree.",
                running_profile=existing.profile,
                requested_profile=profile,
                port=existing.port,
            )

    def _start_budget(self) -> float:
        # §6: readiness + registration + slack. The supervisor's own readiness
        # deadline starts after it registers, so this outlasts it.
        return self.readiness_timeout + REGISTRATION_TIMEOUT.total_seconds() + 5.0

    def _await_own_start(self, key: str, lease: Lease, squatter_unverified: str | None) -> dict[str, Any]:
        """Wait, lock-free, for the outcome our supervisor writes (§6 step 3).

        The waiter reads; it never infers. ``running`` is success — the
        supervisor already wrote the ``up`` event, so none is written here.
        """
        gen = generation_of(lease)
        outcome, cur = self._await_start(key, gen, self._start_budget())
        if outcome is StartWait.RUNNING:
            assert cur is not None
            return self._up_result(
                cur, already_running=False, squatter_unverified=squatter_unverified,
                readiness=self._readiness_of(cur), record=False,
            )
        if outcome is StartWait.FAILED:
            assert cur is not None
            self._consume(key, gen)
            raise self._start_failure(cur)
        if outcome is StartWait.CLEANUP_INCOMPLETE:
            assert cur is not None
            raise self._start_failure(cur)
        if outcome is StartWait.GONE:
            raise DevctlError(
                START_TIMEOUT,
                f"{lease.project!r}'s start was cancelled: its lease was removed or replaced "
                "before it reached running",
                port=lease.port, generation=gen, phase=ev.PHASE_READINESS,
            )
        # Still starting past the budget. A live supervisor is told on disk to
        # give up and finishes the teardown itself (§6); `state` says where it is.
        state = self._abandon_start(key, gen)
        raise DevctlError(
            START_TIMEOUT,
            f"{lease.project!r} did not reach running within {self._start_budget():.0f}s; "
            "its supervisor was asked to stop it",
            port=lease.port, generation=gen, phase=ev.PHASE_READINESS, state=state,
            log_tail=_log_tail(lease.log),
        )

    def _await_other_start(self, key: str, generation: str) -> None:
        """Wait for another caller's start. Its failure is reported as ours."""
        outcome, cur = self._await_start(key, generation, self._start_budget())
        if outcome in (StartWait.FAILED, StartWait.CLEANUP_INCOMPLETE):
            assert cur is not None
            raise self._start_failure(cur)
        if outcome is StartWait.PENDING:
            raise DevctlError(
                START_TIMEOUT,
                "another start of this environment is still in progress",
                generation=generation, state=State.STARTING.value,
            )
        # RUNNING or GONE: the next round renews it, or starts fresh.

    def _await_start(self, key: str, generation: str, budget: float) -> tuple[StartWait, Lease | None]:
        path = self.paths.lease_file(key)
        clock = self.supervision
        deadline = clock.monotonic() + budget
        next_check = clock.monotonic() + LIVENESS_CHECK_S
        while True:
            cur = self._read_lease_quiet(path)
            outcome = start_wait_outcome(cur, generation)
            if outcome is not StartWait.PENDING:
                return outcome, cur
            now = clock.monotonic()
            if now >= next_check:
                next_check = now + LIVENESS_CHECK_S
                if cur is not None and not self._supervisor_live(cur, key):
                    # Dead before or after registering: §10 decides (abandon,
                    # or recover the session and write startup_failed).
                    self._recover(key, generation, actor=ev.ACTOR_UP)
                    continue
            if now >= deadline:
                return StartWait.PENDING, cur
            clock.sleep(min(POLL_S, max(0.0, deadline - now)))

    def _supervisor_live(self, lease: Lease, key: str) -> bool:
        """Is anyone still going to act on this ``starting`` lease?"""
        sup = lease.supervisor
        if sup is None:
            # Never recorded (the CLI died between the two writes): only the
            # registration timeout can settle it.
            return self._now() - (lease.state_since or lease.created) < REGISTRATION_TIMEOUT
        return self.supervision.alive(sup, key)

    def _abandon_start(self, key: str, generation: str) -> str | None:
        """Write ``stop{reason: startup-abandoned}`` and wake. Returns the state."""
        me = self._me()
        with self._lock(key):
            cur = self._read_lease_quiet(self.paths.lease_file(key))
            if cur is None or generation_of(cur) != generation:
                return None
            req = transition(
                cur,
                Event(EventKind.STOP_REQUEST, self._now(), reason=ev.STARTUP_ABANDONED,
                      reason_source=ev.DECLARED, op=ev.UP),
                Actor(ActorKind.CLI, generation, me),
            )
            if isinstance(req, Lease) and req is not cur:
                req.write(self.paths.lease_file(key))
                supervision.record_stop_requested(self.events, req, me)
                cur = req
        if cur.supervisor is not None:
            self.supervision.wake(cur.supervisor, key)
        return state_of(cur).value

    def _consume(self, key: str, generation: str) -> None:
        """Delete our own ``startup_failed`` once its error has been read (§7)."""
        with self._lock(key):
            path = self.paths.lease_file(key)
            cur = self._read_lease_quiet(path)
            if cur is None or state_of(cur) is not State.STARTUP_FAILED:
                return
            if may_remove(cur, Actor(ActorKind.CLI, generation), self._now()):
                path.unlink(missing_ok=True)

    @staticmethod
    def _readiness_of(lease: Lease) -> Readiness:
        try:
            return Readiness(lease.readiness or Readiness.ANSWERED.value)
        except ValueError:
            return Readiness.UNKNOWN

    def _start_failure(self, lease: Lease) -> _Reported:
        """The failure the supervisor recorded, as the waiting ``up``'s answer."""
        error = dict(lease.error or {})
        code = error.get("code") or START_TIMEOUT
        details: dict[str, Any] = {
            "port": lease.port,
            "generation": lease.generation,
            "phase": error.get("phase"),
            "log_tail": error.get("log_tail") or _log_tail(lease.log),
        }
        if state_of(lease) is State.CLEANUP_INCOMPLETE:
            # The startup teardown left survivors. The lease is kept to name
            # them, and the envelope names them too.
            c = lease.cleanup or CleanupRecord()
            details.update(
                cleanup="incomplete", state=State.CLEANUP_INCOMPLETE.value,
                survivors=[s.to_dict() for s in c.survivors],
            )
        message = error.get("message") or f"{lease.project!r} failed to start"
        return _Reported(code, message, **details)

    def _draw_port(
        self, entry: RegistryEntry, preferred_offset: int, own_key: str
    ) -> tuple[int, str | None]:
        """Pick a free port from the project's block, or fail loud (ADR-0004).

        "Free" is derived, never stored: a port is taken if another lease of
        this project still occupies it — anything non-terminal with a live
        supervisor or a non-empty session (§7: terminal records never hold a
        port) — or if anything is listening on it. A dead lease's port is
        reclaimable immediately — the listener check is what stops us handing
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
            if other is not None and self._occupies(other, path.stem):
                who = (other.handle or {}).get("pid") or (
                    other.supervisor.pid if other.supervisor else "starting"
                )
                taken[other.port] = f"lease {path.stem} (pid {who})"
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

    def _occupies(self, lease: Lease, key: str) -> bool:
        """Does this lease still hold its port for the draw?"""
        if not holds_port(lease):
            return False
        obs = self._observe(lease, key)
        if obs.supervisor_alive or obs.membership is not Membership.EMPTY:
            return True
        if lease.is_legacy:
            return False
        # A start that has not registered yet launched nothing, but will: its
        # port is spoken for until the §10 table says it was abandoned.
        return decide(lease, obs, self._now()).action is not LifecycleAction.CLEAN

    # ==================================================================
    # env_down
    # ==================================================================

    def env_down(
        self,
        project: str | None = None,
        cwd: str | None = None,
        reason: str | None = None,
        all_instances: bool = False,
        wait_s: float | None = None,
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

        ``wait_s`` bounds how long to wait for the verified outcome (§8):
        15 s by default, ``0`` for a declared session-end (R0), where every
        request is sent and the call returns ``pending`` at once.
        """
        try:
            self._check_cwd(cwd)
            declared = reason is not None
            if project is not None:
                # Naming a project *is* the declaration: there is no other way to
                # read a targeted teardown than as the polite path (layer 1).
                why, source = reason or ev.EXPLICIT, ev.DECLARED
                if all_instances:
                    paths = self.paths.project_lease_files(project)
                else:
                    target = os.path.realpath(cwd or self._caller_cwd())
                    paths = [self.paths.lease_file_for(project, target)]
            else:
                # No project → down everything leased to the caller's cwd (§4.2).
                source = ev.DECLARED if declared else ev.INFERRED
                why = reason or ev.SESSION_END
                target = os.path.realpath(cwd or self._caller_cwd())
                paths = [
                    p for p in list_lease_files(self.paths.leases_dir)
                    if (lease := self._read_lease_quiet(p)) is not None and lease.cwd == target
                ]
            budget = self._down_budget(why, source, wait_s)
            rows = self._stop_all(paths, why, source, budget, project_hint=project)
            if project is not None and not all_instances:
                return {"ok": True, **rows[0]}
            if project is not None:
                return {"ok": True, "project": project, "downed": rows}
            return {"ok": True, "cwd": target, "downed": rows}
        except DevctlError as e:
            return e.to_envelope()

    @staticmethod
    def _down_budget(why: str, source: str, wait_s: float | None) -> float:
        if wait_s is not None:
            return max(0.0, float(wait_s))
        if why == ev.SESSION_END and source == ev.DECLARED:
            return SESSION_END_WAIT_S
        return DOWN_WAIT_S

    def _stop_all(
        self, paths: list[Path], why: str, source: str, budget: float, *, project_hint: str | None
    ) -> list[dict[str, Any]]:
        """Fan out every request, then wait once for all of them (§8, R0).

        1. Per lease, under its own L (never two at once): write the stop
           request. Bounded; no waiting.
        2. Without L: wake the supervisors; hand leases nobody supervises to a
           recovery stop — here if the budget covers a full grace period, else
           to a detached recovery supervisor.
        3. Without L: poll every lease until each answers or the budget ends.
        """
        clock = self.supervision
        deadline = clock.monotonic() + budget
        targets = [self._request_stop(p, why, source, project_hint) for p in paths]
        for t in targets:
            if t.row is None and t.legacy is not None:
                self._kill_watchdog(t.legacy)
            if t.row is None and t.executor == "supervisor" and t.executor_ref is not None:
                # The wake is optional: the supervisor stats its lease every second.
                clock.wake(t.executor_ref, t.key)
        self._dispatch_recovery(
            [t for t in targets if t.row is None and t.needs_recovery], why, source, deadline
        )
        self._await_stops(targets, deadline, why, source, actor=ev.ACTOR_CLI)
        return [t.row for t in targets if t.row is not None]

    def _request_stop(self, path: Path, why: str, source: str, project_hint: str | None) -> _StopTarget:
        """Under L: write the generation-matched stop request (§8)."""
        key = path.stem
        me = self._me()
        with self._lock(key):
            lease = Lease.read_if_exists(path)
            if lease is None:
                # Idempotent (§4.2), and deliberately unlogged: the SessionEnd hook
                # fires in every session, most of which leased nothing.
                t = _StopTarget(path, key, project_hint or project_from_key(key))
                t.row = {"project": t.project, "was_running": False, "stopped": True}
                return t
            t = _StopTarget(path, key, lease.project, lease.cwd, lease.port, generation_of(lease))
            state = state_of(lease)
            if state in TERMINAL:
                # Nothing runs under a terminal record; the actor that reached it
                # already recorded the teardown. Finish the removal it left.
                if state in REMOVED_ON_REACH or may_remove(
                    lease, Actor(ActorKind.CLI, t.generation), self._now()
                ):
                    path.unlink(missing_ok=True)
                t.row = self._row(t, stopped=True)
                return t
            obs = self._observe(lease, key)
            t.was_running = obs.supervisor_alive or obs.membership is Membership.MEMBERS
            if not obs.supervisor_alive and obs.membership is Membership.AMBIGUOUS:
                # PID S is held by a stranger: nothing under it can be proved
                # ours. No request, no signal; the lease is kept and surfaced.
                self.events.record_cleanup_incomplete(
                    lease.project, op=ev.DOWN, reason=why, reason_source=source, survivors=[],
                    identity_ambiguous=True, escalated=False, port=lease.port,
                    generation=lease.generation,
                    detail=f"pid {lease.ownership().sid} is now held by a different process",
                )
                t.row = self._row(t, **self._ambiguous_fields(lease))
                return t
            req = transition(
                lease,
                Event(EventKind.STOP_REQUEST, self._now(), reason=why, reason_source=source, op=ev.DOWN),
                Actor(ActorKind.CLI, t.generation, me),
            )
            cur = lease
            if isinstance(req, Lease) and req is not lease:
                req.write(path)
                supervision.record_stop_requested(self.events, req, me)
                cur = req
            # `req is lease` is the acknowledgement: already stopping, the
            # first reason stands, nothing written and nothing recorded.
            if lease.is_legacy:
                t.legacy = lease
            if state is State.CLEANUP_INCOMPLETE:
                t.retry_after_attempt = (lease.cleanup or CleanupRecord()).attempts
            if obs.supervisor_alive and cur.supervisor is not None:
                t.executor, t.executor_ref = "supervisor", cur.supervisor
            elif state is State.STARTING and cur.supervisor is not None and not cur.supervisor.registered:
                # Spawned, not yet registered: it will refuse to register over the
                # recorded stop and exit, launching nothing (I1). The waiter's
                # liveness check then lets §10 abandon the record.
                t.executor, t.executor_ref = "supervisor", cur.supervisor
            else:
                t.needs_recovery = True
        return t

    def _dispatch_recovery(
        self, targets: list[_StopTarget], why: str, source: str, deadline: float
    ) -> None:
        """Start the recovery stop for leases nobody supervises (§10, R0)."""
        if not targets:
            return
        inline: list[_StopTarget] = []
        for t in targets:
            t.needs_recovery = False
            remaining = deadline - self.supervision.monotonic()
            empty = self._session_empty(t)
            if empty or self._inline_recovery_fits(remaining):
                # An empty session is only a record to clean: no stop, no wait.
                inline.append(t)
                continue
            try:
                ref = self.supervision.spawn_recovery(
                    t.key, t.generation, paths=self.paths, reason=why, reason_source=source, op=ev.DOWN,
                )
            except DevctlError as e:
                t.extra["detail"] = (
                    f"no recovery supervisor could be started ({e.message}); the stop is "
                    "recorded and the next `rent sweep` resumes it"
                )
                continue
            t.executor, t.executor_ref = "recovery", ref
        self._run_inline_recoveries(inline, why, source, deadline)

    def _session_empty(self, t: _StopTarget) -> bool:
        lease = self._read_lease_quiet(t.path)
        if lease is None or generation_of(lease) != t.generation:
            return True
        return self._observe(lease, t.key).membership is Membership.EMPTY

    def _run_inline_recoveries(
        self, targets: list[_StopTarget], why: str, source: str, deadline: float
    ) -> None:
        """Recovery stops in this process: concurrently, one thread per lease (§8).

        Each runs ``recover_lease``, which takes L only for its bounded
        sections. The outcome is read back from the lease by the waiter, never
        taken from the return value (the agreement rule, §13). Daemon threads,
        joined up to the deadline: a caller that must leave is not held hostage.
        """
        if not targets:
            return

        def run(t: _StopTarget) -> None:
            self._recover(t.key, t.generation, actor=ev.ACTOR_CLI, reason=why,
                          reason_source=source, op=ev.DOWN)

        for t in targets:
            t.executor = "inline"
        if len(targets) == 1:
            run(targets[0])
            return
        threads = [threading.Thread(target=run, args=(t,), daemon=True) for t in targets]
        for th in threads:
            th.start()
        for th in threads:
            th.join(max(0.0, deadline - self.supervision.monotonic()))

    def _await_stops(
        self, targets: list[_StopTarget], deadline: float, why: str, source: str, *, actor: str
    ) -> None:
        """Poll, lock-free, until every target answers or the budget ends (§8).

        Each answer is what the lease says: gone or another generation is
        ``stopped``; ``cleanup_incomplete`` names its survivors; anything else
        past the deadline is ``pending``. A supervisor found dead mid-wait hands
        its lease to a recovery stop.
        """
        clock = self.supervision
        next_check = clock.monotonic() + LIVENESS_CHECK_S
        while True:
            open_ = [t for t in targets if t.row is None]
            for t in open_:
                cur = self._read_lease_quiet(t.path)
                outcome = stop_wait_outcome(cur, t.generation) if t.generation else StopWait.STOPPED
                if outcome is StopWait.STOPPED:
                    t.row = self._row(t, stopped=True)
                elif outcome is StopWait.CLEANUP_INCOMPLETE:
                    assert cur is not None
                    attempts = (cur.cleanup or CleanupRecord()).attempts
                    if t.retry_after_attempt is None or attempts > t.retry_after_attempt:
                        t.row = self._row(t, **self._incomplete_row(cur))
            open_ = [t for t in targets if t.row is None]
            if not open_:
                return
            now = clock.monotonic()
            if now >= deadline:
                for t in open_:
                    t.row = self._pending_row(t)
                return
            if now >= next_check:
                next_check = now + LIVENESS_CHECK_S
                orphaned = [t for t in open_ if not self._executor_live(t)]
                self._dispatch_recovery(orphaned, why, source, deadline)
            clock.sleep(min(POLL_S, max(0.0, deadline - now)))

    def _executor_live(self, t: _StopTarget) -> bool:
        """Is anyone still there to finish this stop?

        The lease's supervisor, a recovery supervisor this call spawned, or
        whoever holds a live recovery claim. If none is, the waiter hands the
        lease to a recovery stop rather than waiting out its budget on nobody.
        """
        cur = self._read_lease_quiet(t.path)
        if cur is None or generation_of(cur) != t.generation:
            return True
        if cur.supervisor is not None and self.supervision.alive(cur.supervisor, t.key):
            return True
        if t.executor == "recovery" and t.executor_ref is not None:
            if self.supervision.alive(t.executor_ref, t.key):
                return True
        return cur.recovery is not None and self._observe(cur, t.key).claim_holder_alive

    def _row(self, t: _StopTarget, **fields: Any) -> dict[str, Any]:
        return {
            "project": t.project,
            "cwd": t.cwd,
            "port": t.port,
            "was_running": t.was_running,
            **fields,
        }

    def _pending_row(self, t: _StopTarget) -> dict[str, Any]:
        cur = self._read_lease_quiet(t.path)
        state = State.STOPPING
        if cur is not None and (cur.stop is None or state_of(cur) is State.CLEANUP_INCOMPLETE):
            state = state_of(cur)
        # With a stop recorded the lease is `stopping` in every sense but the
        # one write its owner has not made yet; `running` here would read as
        # "the request was ignored".
        if "detail" in t.extra:
            detail = t.extra["detail"]
        elif t.executor == "supervisor" and t.executor_ref is not None:
            detail = f"supervisor {t.executor_ref.pid} is completing cleanup; rent ls shows the outcome"
        elif t.executor == "recovery" and t.executor_ref is not None:
            detail = (
                f"recovery supervisor {t.executor_ref.pid} is completing cleanup; "
                "rent ls shows the outcome"
            )
        else:
            detail = "cleanup is under way; rent ls shows the outcome"
        return self._row(t, stopped=None, pending=True, state=state.value, detail=detail)

    @staticmethod
    def _incomplete_row(lease: Lease) -> dict[str, Any]:
        """The result fields for a lease the stop could not verify empty (§8)."""
        c = lease.cleanup or CleanupRecord()
        if c.identity_ambiguous:
            return Service._ambiguous_fields(lease)
        pids = ", ".join(str(s.pid) for s in c.survivors) or "unknown"
        return {
            "stopped": False,
            "state": State.CLEANUP_INCOMPLETE.value,
            "survivors": [s.to_dict() for s in c.survivors],
            "detail": (
                f"the stop left survivors (pid {pids}); the lease is kept so they stay "
                "attributable. Investigate before reusing the port."
            ),
        }

    @staticmethod
    def _ambiguous_fields(lease: Lease) -> dict[str, Any]:
        return {
            "stopped": False,
            "state": state_of(lease).value,
            "survivors": [],
            "identity_ambiguous": True,
            "detail": (
                "the workload's session can no longer be proved ours (its pid is held by a "
                "different process), so the lease is kept and nothing in it is touched. "
                "Investigate before reusing the port."
            ),
        }

    @staticmethod
    def _cleanup_incomplete_error(row: dict[str, Any]) -> DevctlError:
        extra = {k: row[k] for k in ("state", "survivors", "identity_ambiguous") if k in row}
        return DevctlError(
            CLEANUP_INCOMPLETE,
            "this directory's environment could not be verified stopped: "
            + str(row.get("detail", "")),
            **extra,
        )

    def _ambiguous_error(self, lease: Lease) -> DevctlError:
        return self._cleanup_incomplete_error(self._ambiguous_fields(lease))

    # ==================================================================
    # env_ls / env_sweep — shared reconcile engine
    # ==================================================================

    def env_ls(self) -> dict[str, Any]:
        try:
            _swept, kept = self._reconcile_all(op=ev.ACTOR_LS)
            registry = self._safe_registry()
            environments = [self._ls_entry(l) for l in kept]
            squatters, sq_unverified = self._squatters(registry, kept)
            environments.extend(sq.row() for sq in squatters)
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
            swept, kept = self._reconcile_all(op=ev.ACTOR_SWEEP)
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
                    # advisory: report only (O4) — nothing is locked, re-probed or signalled
                    result["squatters"] = [sq.row() for sq in squatters]
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
        """Apply the §10 table to every lease. Returns (swept, kept).

        Each lease goes through ``supervision.recover_lease``: a record nobody
        will act on is cleaned, a dead supervisor's live workload is marked
        ``unsupervised`` and kept serving, an expired or stop-requested one is
        stopped under a recovery claim, an ambiguous identity is kept and never
        signalled, and a live supervisor is left alone (or, hung past its
        expiry, asked on disk). The stop runs without L; L is held only for the
        bounded read-modify-writes on either side of it.

        Every row is recorded by whoever performed it, with ``op`` as the actor
        (cleanup layer 4 for sweep/ls, spec §8) — nothing here is inferred.
        """
        swept: list[dict[str, Any]] = []
        kept: list[Lease] = []
        for path in list_lease_files(self.paths.leases_dir):
            before = self._read_lease_quiet(path)
            if before is None:
                continue  # corrupt/vanished — leave it; squatter check surfaces the server
            gen = generation_of(before)
            result = self._recover(path.stem, gen, actor=op)
            entry = {"project": before.project, "cwd": before.cwd, "port": before.port}
            action = self._swept_action(before, result)
            if action is not None:
                swept.append({**entry, "action": action, "reason": result.detail})
            after = self._read_lease_quiet(path)
            if after is not None and holds_port(after):
                kept.append(after)
        return swept, kept

    @staticmethod
    def _swept_action(before: Lease, result: RecoveryResult) -> str | None:
        if result.outcome == RECOVERED_CLEANED:
            return "clean"
        if result.outcome == RECOVERED_STARTUP_FAILED:
            return "recover"
        if result.outcome == RECOVERED_STOPPED:
            # A fresh expiry stop is layer-4 `expire`; resuming a teardown someone
            # else asked for (explicit, session-end) is a `stop`.
            requested = before.stop.reason if before.stop is not None else ev.SWEEP_EXPIRED
            return "expire" if requested in (ev.SWEEP_EXPIRED, ev.EXPIRY) else "stop"
        return None

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

    def _kill_watchdog(self, lease: Lease) -> None:
        """SIGTERM a legacy lease's 1.0.x watchdog — only if provably ours (§3).

        New code never spawns a watchdog; this is for a 1.0.x one still
        babysitting a legacy lease. It is signalled only when the lease carries
        1.0.2's ``watchdog_pid_start_time``, that start time matches, **and**
        the process's cmdline names a watchdog. Anything short of all three is
        not signalled.

        The signal is a courtesy, never the mechanism: the stop request that
        precedes this call rewrote the lease as schema 2, whose poisoned
        ``watchdog_pid`` makes a 1.0.x watchdog read it as corrupt and exit
        without touching anything (§14). Declining costs one idle tick;
        signalling the wrong pid costs somebody else's process (WI-0069).
        """
        pid = lease.watchdog_pid
        if pid is None:
            return
        started = lease.watchdog_pid_start_time
        if started is None:
            # Written by 1.0.1 (or the watchdog died before it could be observed):
            # nothing to check the pid against, so it is not signalled at all.
            self._record_watchdog_skip(lease, pid, "unverifiable")
            return
        observed = procutil.observe_start_time(pid)
        if observed is None:
            return  # already exited on its own — the ordinary case, nothing refused
        if not procutil.start_time_matches(started, observed):
            self._record_watchdog_skip(lease, pid, "pid-recycled")
            return
        if not self._is_watchdog(pid):
            self._record_watchdog_skip(lease, pid, "not-a-watchdog")
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass  # exited between the check and the signal; the poison covers it

    @staticmethod
    def _is_watchdog(pid: int) -> bool:
        try:
            cmdline = psutil.Process(pid).cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return False
        return any(marker in arg for arg in cmdline for marker in _WATCHDOG_MARKERS)

    def _record_watchdog_skip(self, lease: Lease, pid: int, reason: str) -> None:
        self.events.record(
            ev.WATCHDOG_SIGNAL_SKIPPED, lease.project, pid=pid, reason=reason, port=lease.port
        )

    def _read_lease_quiet(self, path: Path) -> Lease | None:
        try:
            return Lease.read_if_exists(path)
        except DevctlError:
            return None  # corrupt lease — skip (F5); the server shows up as a squatter

    def _ls_entry(self, lease: Lease) -> dict[str, Any]:
        """One board row. Beyond 1.0.x's fields: ``state``, ``supervisor``,
        ``survivors`` and ``pending`` — what the lease says, so a row can never
        claim a running server the lifecycle does not (§7, §12)."""
        key = lease_key(lease.project, lease.cwd)
        state = state_of(lease)
        sup = lease.supervisor
        out: dict[str, Any] = {
            "project": lease.project,
            "profile": lease.profile,
            "cwd": lease.cwd,          # which lane this instance belongs to (ADR-0007)
            "port": lease.port,
            "pid": (lease.handle or {}).get("pid"),
            "runner": lease.runner,
            "session": lease.session,
            "started": lease.created.isoformat(),
            "lease_expires": lease.expires.isoformat(),
            "state": state.value,
            "supervisor": None if sup is None else {
                "pid": sup.pid,
                "registered": sup.registered,
                "alive": self.supervision.alive(sup, key),
            },
            "healthy": state in (State.RUNNING, State.UNSUPERVISED) and self._answering(lease.port),
        }
        if lease.is_legacy:
            out["legacy"] = True
        if lease.stop is not None and state not in TERMINAL:
            out["pending"] = True
            out["stop_reason"] = lease.stop.reason
        if lease.cleanup is not None and (lease.cleanup.survivors or state is State.CLEANUP_INCOMPLETE):
            out["survivors"] = [s.to_dict() for s in lease.cleanup.survivors]
            if lease.cleanup.identity_ambiguous:
                out["identity_ambiguous"] = True
        if lease.spawn_cwd is not None and lease.spawn_cwd != lease.cwd:
            # Only when it differs: on the board, N identical rows would bury
            # the one lane whose server is serving somewhere else (ADR-0010).
            out["serving"] = lease.spawn_cwd
        return out

    def _table(self) -> ProcessTable:
        """The process table behind the supervision seam (the real one by default)."""
        return self.supervision.table or procutil.OsProcessTable()

    def _squatters(
        self, registry: Registry | None, kept: list[Lease]
    ) -> tuple[list[_Squatter], str | None]:
        """Listeners inside a claimed block with no lease behind them (ADR-0004 §6).

        Ownership is by block range, not by enumerating declared offsets — a
        squatter on ``block + 7`` of a single-profile project used to be
        invisible here. A listener whose session is a kept lease's session is
        that lease's workload wherever it bound (ADR-0016 §11 precondition 2),
        so it is owned, never a squatter.

        This is a **lock-free snapshot**: good enough to report, never enough to
        act on. Each row carries the listener's ``(pid, start_time, sid)`` as
        observed here (§11 precondition 1), and :meth:`_kill_squatters`
        re-derives all of it under the project lock before it signals anything.

        Returns ``(rows, unverified_reason)``. Per ADR-0008 §3 an unrunnable probe
        must not render as a clean board, and the reason is returned **beside**
        the list rather than as a row in it: a list whose consumer may signal its
        contents must only ever hold real, attributed processes.
        """
        if registry is None:
            return [], None
        kept_ports = {l.port for l in kept}
        owned_sids = {ident.sid for l in kept if (ident := l.ownership()) is not None}
        table = self._table()
        out: list[_Squatter] = []
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
            if owner is None:
                continue
            row = table.row(owner.pid)
            sid = None if row is None else row.sid
            if sid is not None and sid in owned_sids:
                continue
            out.append(
                _Squatter(
                    project=project, port=port, pid=owner.pid, name=owner.name,
                    start_time=None if row is None else row.start_time, sid=sid,
                )
            )
        return out, None

    def _live_lease_sids(self) -> set[int]:
        """The session of every lease on disk that names one, across all projects.

        Deliberately wider than "non-terminal": a listener in *any* recorded
        lease's session is at least arguably ours, and the cost of a wrong
        exclusion here is only that a squatter survives one more sweep. Read
        without the other projects' locks (Rule 1: one L at a time) — a read of
        an atomically-replaced file needs none.
        """
        sids: set[int] = set()
        for path in list_lease_files(self.paths.leases_dir):
            lease = self._read_lease_quiet(path)
            if lease is not None and (ident := lease.ownership()) is not None:
                sids.add(ident.sid)
        return sids

    def _kill_squatters(self, squatters: list[_Squatter]) -> list[dict[str, Any]]:
        """Strict mode's reclaim: SIGTERM a verified squatter, gently (ADR-0016 §11).

        Strict enforcement is off by default and is earned at the pilot-exit
        gate. The snapshot from :meth:`_squatters` was taken without any lock,
        so between it and now a ``starting`` lease may have claimed the port
        and its workload begun listening mid-readiness (§6 writes the lease
        before the first process exists), or the listener may have exited and
        its pid been reused. Each row is therefore re-decided **under that
        port's project lock**, one project lock at a time (Rule 1), and only a
        row that survives every check is signalled:

        1. no non-terminal lease of the project holds the port (and no lease
           file of the project is unreadable — an unreadable lease might be the
           owner, so the port is not reclaimed on a guess);
        2. the port is re-probed now, and the listener is the same pid with the
           same start time and session as the snapshot;
        3. its session is not any lease's session;
        4. then one :func:`procutil.verified_signal` SIGTERM with that identity —
           never SIGKILL, never escalated: a squatter is not ours to force.

        The re-probe is the one bounded external call made under L (``lsof``,
        5 s timeout), the same exception §9 grants the port draw. Nothing here
        sleeps or waits on another rentctl process while L is held.

        Every row, signalled or refused, is recorded as a ``squatter_reclaim``
        event so a refusal is evidence rather than silence.
        """
        results: list[dict[str, Any]] = []
        for sq in squatters:
            with project_lock(self.paths.lock_file(sq.project)):
                outcome = self._reclaim_one(sq)
            killed = outcome == procutil.SignalResult.SIGNALLED.value
            self.events.record(
                _SQUATTER_RECLAIM, sq.project, port=sq.port, pid=sq.pid, name=sq.name,
                start_time=sq.start_time, sid=sq.sid, signal="SIGTERM" if killed else None,
                killed=killed, outcome=outcome,
            )
            results.append({**sq.row(), "killed": killed, "outcome": outcome})
        return results

    def _reclaim_one(self, sq: _Squatter) -> str:
        """Decide and act on one squatter. The caller holds ``sq.project``'s L."""
        leases_unreadable = False
        for path in self.paths.project_lease_files(sq.project):
            try:
                lease = Lease.read_if_exists(path)
            except DevctlError:
                leases_unreadable = True
                continue
            if lease is not None and lease.port == sq.port and holds_port(lease):
                return _RECLAIM_LEASED
        if leases_unreadable:
            return _RECLAIM_LEASE_UNREADABLE
        if sq.start_time is None or sq.sid is None:
            return _RECLAIM_UNVERIFIABLE
        try:
            owner = self._port_owner(sq.port)
        except procutil.ProbeUnavailable:
            return _RECLAIM_PROBE_UNAVAILABLE
        if owner is None:
            return _RECLAIM_GONE
        if owner.pid != sq.pid:
            return _RECLAIM_LISTENER_CHANGED
        table = self._table()
        row = table.row(sq.pid)
        if row is None or row.zombie:
            return _RECLAIM_GONE
        if row.sid != sq.sid or not procutil.start_time_matches(sq.start_time, row.start_time):
            return procutil.SignalResult.IDENTITY_MISMATCH.value
        if row.sid in self._live_lease_sids():
            return _RECLAIM_LEASE_SESSION
        return procutil.verified_signal(
            sq.pid, sq.start_time, sq.sid, signal.SIGTERM, table=self.supervision.table
        ).value

    def _up_result(
        self,
        lease: Lease,
        *,
        already_running: bool,
        squatter_unverified: str | None = None,
        readiness: Readiness = Readiness.NOT_PROBED,
        record: bool,
        detail: str | None = None,
    ) -> dict[str, Any]:
        """Render the ``env_up`` envelope — and, for a renewal, record the event.

        A fresh start's ``up`` event is the supervisor's: it writes it when it
        reaches ``running`` (§13's agreement rule), so ``record`` is false there.
        A renewal is this caller's own transition, so it records it.

        ``squatter_unverified`` carries ADR-0008 §2's blind spot: when set, the
        port was drawn without a working listener check, and the envelope says so
        rather than presenting the draw as fully verified. The renew path leaves
        it ``None`` because it draws no port at all.
        """
        pid = (lease.handle or {}).get("pid")
        sup_pid = None if lease.supervisor is None else lease.supervisor.pid
        if record:
            self.events.record_up(
                lease.project,
                profile=lease.profile,
                port=lease.port,
                pid=pid,
                session=lease.session,
                cwd=lease.cwd,
                spawn_cwd=(lease.spawn_cwd if lease.spawn_cwd != lease.cwd else None),
                lease_expires=lease.expires.isoformat(),
                already_running=already_running,
                generation=lease.generation,
                supervisor_pid=sup_pid,
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
            "pid": pid,
            "lease_expires": lease.expires.isoformat(),
            "already_running": already_running,
            "state": state_of(lease).value,
            "supervisor_pid": sup_pid,
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
            # True only because the probe dials BOTH loopbacks: LISTENING means
            # neither 127.0.0.1 nor ::1 answered, so no resolution of
            # `localhost` reaches it. With an IPv4-only probe this lied about
            # every ::1-only server (WI-0072).
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
        if detail is not None:
            out["detail"] = detail
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
