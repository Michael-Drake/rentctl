# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Drake

"""Is rentctl alive, and are its capabilities actually real? (WI-0051)

rentctl's console shims died at the ADR-0009 rename and **nobody noticed for
nine days**. Every enrolled repo's SessionStart sweep and SessionEnd teardown was
configured as ``rent …``; `rent` no longer imported; hooks that exit non-zero are
not surfaced by the runtime. So the pilot's layer-2 evidence stream was down,
cleanup silently regressed to layer 3 alone, and the first symptom was a human
trying to lease something on day nine.

This module is the detector that outage should have had
(``scheduled-liveness-smoke-test``, and the ``add-structural-guard-on-recurrence``
trigger).

**Why this cannot be built on the event log.** The obvious signal is layer-2
silence: no ``session-end`` teardown since 2026-08-01 looks damning. It proves
nothing. :mod:`rentctl.core.events` deliberately records *no* event for a `down`
that found no lease, because the SessionEnd hook fires in every enrolled session
including the many that never leased anything. So silence is produced identically
by "the hook is dead" and "nobody leased anything" — and scoring the first from
the second is exactly the proof-from-a-negative that makes the pilot's G3
criterion unpassable. A detector built on it would have been unfalsifiable in the
same way the thing it detects was invisible.

**So every check here executes the capability rather than inspecting a
description of it.** Finding the string ``rent sweep`` in a settings file proves
the text is present, which was *just as true* throughout the nine-day outage. The
only question that distinguishes those worlds is whether the command runs
(``ship-the-detector-with-the-capability``).

**Three statuses, never two.** ``UNKNOWN`` is not folded into ``OK``
(``declare-what-a-check-assumes``): "I could not tell" and "I checked and it is
fine" are different answers, and collapsing them is how a detector certifies the
gap it exists to find.

**The self-reference problem, stated because it is load-bearing.** A self-check
invoked as ``rent doctor`` cannot report that ``rent`` is missing — the broken
entry point swallows its own alarm. That is why this module is runnable as
``python3 -m rentctl.doctor`` with no shim involved, and why anything scheduling
it MUST treat a non-zero exit *and* a "command not found" as failure. A scheduler
that skips on "command not found" reproduces the original outage exactly.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .paths import DevctlPaths
from .registry import Registry
from .runtimes import CLAUDE_CODE, RuntimeBinding
from .wiring import COMMAND, INSTALL_COMMAND, OWNED_COMMANDS, PluginState, plugin_state

# --- statuses -------------------------------------------------------------

OK = "ok"
WARN = "warn"
FAIL = "fail"
UNKNOWN = "unknown"

#: Statuses that make `rent doctor` exit non-zero. WARN does not — a warning is a
#: thing to fix, not a thing that is broken now, and a detector that pages on
#: warnings gets muted, which is the only failure mode worse than not existing.
EXIT_NONZERO = (FAIL, UNKNOWN)

# Where a project's session hooks come from (WI-0070).
SOURCE_PLUGIN = "plugin"
SOURCE_SETTINGS = "settings"
SOURCE_BOTH = "both"
SOURCE_NONE = "none"

#: How long the capability probe may take before we call it UNKNOWN rather than
#: FAIL. A hung probe is not evidence of a broken shim.
PROBE_TIMEOUT = 20


@dataclass(frozen=True)
class Check:
    """One question, its answer, and how the answer was reached.

    ``probe`` records what was actually run (``capture-the-probe``) — a report
    that says "shims ok" without saying what it executed is a verdict without
    evidence, and this whole module exists because a verdict was trusted for nine
    days.
    """

    name: str
    status: str
    detail: str
    probe: str = ""
    #: For a hooks check, where the hooks come from (``SOURCE_*``). Empty for
    #: every other check.
    source: str = ""

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"check": self.name, "status": self.status, "detail": self.detail}
        if self.probe:
            out["probe"] = self.probe
        if self.source:
            out["source"] = self.source
        return out


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status in EXIT_NONZERO]

    @property
    def ok(self) -> bool:
        return not self.failed

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": [c.as_dict() for c in self.checks],
            "summary": {
                status: sum(1 for c in self.checks if c.status == status)
                for status in (OK, WARN, FAIL, UNKNOWN)
            },
        }


# --- the probe seam -------------------------------------------------------

#: Runs a command and returns (returncode, stdout, stderr). Injected so tests can
#: simulate a broken shim without breaking the test runner's own install.
Runner = Callable[[Sequence[str]], "tuple[int, str, str]"]


def _subprocess_runner(argv: Sequence[str]) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return (124, "", f"timed out after {PROBE_TIMEOUT}s")
    except OSError as exc:
        return (127, "", str(exc))
    return (proc.returncode, proc.stdout, proc.stderr)


# --- individual checks ----------------------------------------------------


def check_shim(command: str = COMMAND, *, runner: Runner | None = None) -> Check:
    """Does the installed console script resolve **and work**?

    Resolution alone is not the question. Throughout the nine-day outage the
    shim file existed and was executable; it died on import, because the renamed
    package resolved as a namespace package over stale ``__pycache__``. So the
    probe runs a real read-only command end to end and requires structured
    success out the far side.
    """
    run = runner or _subprocess_runner
    path = shutil.which(command)
    if path is None:
        return Check(
            f"shim:{command}",
            FAIL,
            f"{command!r} does not resolve on PATH — every hook spelled "
            f"{command!r} is failing silently right now",
            probe=f"shutil.which({command!r})",
        )

    probe = f"{command} ls"
    code, out, err = run([path, "ls"])
    if code == 0:
        try:
            parsed = json.loads(out)
        except ValueError:
            return Check(
                f"shim:{command}",
                UNKNOWN,
                f"{path} exited 0 but did not emit JSON — cannot confirm the "
                f"capability is real",
                probe=probe,
            )
        if parsed.get("ok") is True:
            return Check(f"shim:{command}", OK, f"{path} runs and answers", probe=probe)
        return Check(
            f"shim:{command}",
            FAIL,
            f"{path} ran but reported not-ok: {parsed!r}",
            probe=probe,
        )

    reason = (err or out).strip().splitlines()
    tail = reason[-1] if reason else f"exit {code}"
    return Check(
        f"shim:{command}",
        FAIL,
        f"{path} exists but fails to run — {tail}",
        probe=probe,
    )


def check_install_is_durable() -> Check:
    """Is the installed package a built artifact, or an editable pointer?

    An editable install makes every enrolled repo's CLI float on a live source
    tree: the moment that tree is renamed, moved, or mid-refactor, the whole
    fleet's hooks break — which is precisely how the nine-day outage was armed
    (WI-0050). Reported as a WARNING rather than a failure: it is working right
    now, and it is a loaded gun rather than a fired one.
    """
    try:
        import rentctl
    except Exception as exc:  # pragma: no cover - unreachable from a live import
        return Check("install", FAIL, f"the rentctl package does not import: {exc}")

    origin = getattr(rentctl, "__file__", None)
    if not origin:
        return Check("install", UNKNOWN, "the rentctl package reports no __file__")

    resolved = Path(origin).resolve()
    parts = resolved.parts
    if "site-packages" in parts or "dist-packages" in parts:
        return Check(
            "install",
            OK,
            f"installed as a built artifact ({resolved.parent})",
            probe="rentctl.__file__",
        )
    return Check(
        "install",
        WARN,
        f"running from a source tree ({resolved.parent}) — an editable install "
        f"re-arms the failure class that broke the fleet's shims for 9 days",
        probe="rentctl.__file__",
    )


def check_registry(paths: DevctlPaths) -> Check:
    """Is the registry readable? Everything downstream assumes it."""
    path = paths.registry_file
    if not path.exists():
        return Check(
            "registry",
            WARN,
            f"no registry at {path} — nothing is enrolled on this machine",
            probe=str(path),
        )
    try:
        registry = Registry.load(path)
    except Exception as exc:
        return Check("registry", FAIL, f"{path} will not load: {exc}", probe=str(path))
    return Check(
        "registry",
        OK,
        f"{len(registry.projects)} project(s) enrolled",
        probe=str(path),
    )


def _hook_commands(settings: dict[str, Any]) -> list[str]:
    """Every hook command in a settings file that is ours to care about."""
    found: list[str] = []
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return found
    for groups in hooks.values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            for hook in group.get("hooks", []) or []:
                if not isinstance(hook, dict):
                    continue
                command = hook.get("command")
                if not isinstance(command, str) or not command.strip():
                    continue
                try:
                    head = shlex.split(command)[0]
                except ValueError:
                    continue
                if Path(head).name in OWNED_COMMANDS:
                    found.append(command)
    return found


def check_project_hooks(
    project: str,
    source_dir: Path,
    *,
    binding: RuntimeBinding = CLAUDE_CODE,
    claude_home: Path | None = None,
) -> Check:
    """Are this project's hooks wired — by whom — and can they actually run?

    The outage's signature: the settings file said ``rent sweep``, the string was
    present and correct, and the command did not exist. So this resolves the
    command's head on PATH rather than merely finding it in the file — the one
    difference between a wired project and a working one.

    **Two sources, and the check has to see both** (WI-0070). Hooks reach a
    Claude Code session from the project's settings file *or* from the rentctl
    plugin. Reading only the settings file warned "layer 3 alone" on exactly the
    published install path, where the plugin supplies the hooks and ``init``
    deliberately writes none. So the result names its ``source``:

    ============  ======  =====================================================
    source        status  why
    ============  ======  =====================================================
    ``plugin``    OK      plugin enabled here, and ``rent`` resolves
    ``settings``  OK      settings file wires them, all resolvable
    ``both``      WARN    double-wired: every SessionEnd runs teardown twice
    ``none``      WARN    layer 3 alone (unchanged)
    ============  ======  =====================================================

    **Double-wired is a WARN, not a FAIL and not an OK.** Claude Code
    deduplicates an identical handler across settings files but keeps "a
    plugin's … copy of the same handler separate", so both fire, in parallel.
    That is safe: ``env_down`` writes its stop request in ``_request_stop``,
    which re-reads the lease file *under* the per-project lock, and the first
    request wins (ADR-0016 §8). Whichever run loses the race finds the stop
    already requested, or the lease already gone, and writes nothing and
    records nothing; the one ``down`` comes from the actor that verified the
    stop. It is still worth fixing — two
    copies of one wiring, upgraded by two different mechanisms, is the drift
    class this module's history is made of — but nothing is broken now, and
    paging on it is how a detector gets muted.

    A plugin state that cannot be determined is never folded into either
    answer: with no settings hooks it is ``UNKNOWN`` (we cannot say whether
    anything is wired); with settings hooks it is ``WARN`` (they are wired; we
    cannot rule out the double).
    """
    name = f"hooks:{project}"
    if binding is CLAUDE_CODE:
        plugin = plugin_state(claude_home, source_dir)
    else:
        plugin = PluginState(False, f"the rentctl plugin does not apply to {binding.label}")

    settings_path = binding.hooks_path(source_dir)
    commands: list[str] = []
    if not settings_path.exists():
        legacy = binding.legacy_hooks_path(source_dir)
        if legacy is not None and legacy.exists():
            settings_path = legacy
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return Check(name, UNKNOWN, f"{settings_path} will not parse: {exc}",
                         probe=str(settings_path))
        if not isinstance(settings, dict):
            return Check(name, UNKNOWN, f"{settings_path} is not a JSON object",
                         probe=str(settings_path))
        commands = _hook_commands(settings)
    probe = f"shutil.which(head) for each hook in {settings_path}; plugin_state()"

    broken = []
    for command in commands:
        head = shlex.split(command)[0]
        if shutil.which(head) is None and not Path(head).exists():
            broken.append(command)
    if broken:
        return Check(
            name,
            FAIL,
            f"{len(broken)} of {len(commands)} wired hook(s) name a command that "
            f"does not resolve: {broken!r} — these are failing silently every session",
            probe=probe,
            source=SOURCE_BOTH if plugin.active else SOURCE_SETTINGS,
        )

    if commands and plugin.active:
        return Check(
            name,
            WARN,
            f"double-wired: {plugin.detail}, and {settings_path} also wires "
            f"{len(commands)} rentctl hook(s). Claude Code does not deduplicate a "
            f"plugin's hook against a settings-file copy, so each SessionEnd runs "
            f"teardown twice. Teardown is idempotent under the per-project lock — "
            f"the second run finds no lease — so this is redundancy, not breakage; "
            f"but the two copies are upgraded by different mechanisms and can drift",
            probe=probe,
            source=SOURCE_BOTH,
        )
    if commands and plugin.active is None:
        return Check(
            name,
            WARN,
            f"{len(commands)} wired hook(s) in {settings_path}, all resolvable — but "
            f"cannot tell whether the plugin also wires them ({plugin.detail})",
            probe=probe,
            source=SOURCE_SETTINGS,
        )
    if commands:
        return Check(
            name,
            OK,
            f"{len(commands)} wired hook(s) in {settings_path}, all resolvable",
            probe=probe,
            source=SOURCE_SETTINGS,
        )

    if plugin.active:
        if shutil.which(COMMAND) is None:
            return Check(
                name,
                FAIL,
                f"{plugin.detail}, but its hooks call {COMMAND!r}, which does not "
                f"resolve on PATH — session-end cleanup is off until "
                f"`{INSTALL_COMMAND}`",
                probe=probe,
                source=SOURCE_PLUGIN,
            )
        return Check(
            name,
            OK,
            f"hooks supplied by the plugin — {plugin.detail}; {COMMAND!r} resolves",
            probe=probe,
            source=SOURCE_PLUGIN,
        )
    if plugin.active is None:
        return Check(
            name,
            UNKNOWN,
            f"{settings_path} wires no rentctl hooks, and cannot tell whether the "
            f"plugin supplies them: {plugin.detail}",
            probe=probe,
            source=SOURCE_NONE,
        )

    if not settings_path.exists():
        return Check(
            name,
            WARN,
            f"no settings file at {settings_path} and {plugin.detail} — this "
            f"project is enrolled in the registry but has no hooks wired",
            probe=probe,
            source=SOURCE_NONE,
        )
    return Check(
        name,
        WARN,
        f"{settings_path} wires no rentctl hooks and {plugin.detail} — teardown "
        f"for this project depends on layer 3 alone",
        probe=probe,
        source=SOURCE_NONE,
    )


# --- supervision level (ADR-0016 §5, plan step 9) --------------------------

#: Run in a throwaway child so the probe never changes the doctor process: the
#: child-subreaper attribute is per process and dies with the child.
SUPERVISION_PROBE = (
    "import json; from rentctl.core import procutil; "
    "print(json.dumps({'subreaper': procutil.enable_child_subreaper(), "
    "'pidfd': procutil.pidfd_available()}))"
)

SESSION_BOUNDARY = (
    "every process that stays in the environment's session is found and stopped "
    "(orphans of an exited shell, SIGTERM-ignorers, setpgid() escapers, children "
    "forked during shutdown). Boundary: a command that calls setsid() or daemonizes "
    "(double-fork + setsid) leaves the session and escapes supervision; if it holds "
    "the lease's port rentctl reports it and never signals it"
)

SUBREAPER_BOUNDARY = (
    "each supervisor is a child subreaper (prctl PR_SET_CHILD_SUBREAPER), so every "
    "descendant of the command is captured whatever its session: a process that "
    "double-forks and setsid()s is still reparented to the supervisor, not init, and "
    "is stopped with the rest. Boundary: a process that is not the command's "
    "descendant is not captured — work handed to another manager (systemd-run, a "
    "container runtime, at/cron, an already-running server such as tmux) — nor is an "
    "orphan whose parent had moved it into a foreign PID namespace with setns(2), "
    "since it reparents to that namespace's init; the capture also ends if the "
    "supervisor itself is killed"
)


def check_supervision(
    *, platform: str | None = None, runner: Runner | None = None
) -> Check:
    """Which supervision guarantee does this host give, and where does it end?

    The level is what a supervisor started here would establish:
    ``session`` (macOS, or Linux where the subreaper is refused) or
    ``session+subreaper`` (Linux). On Linux the capability is **executed**, not
    inferred from the kernel version: a child interpreter calls the same
    ``enable_child_subreaper`` the supervisor calls and opens a pidfd. Each
    lease also records the level its own supervisor actually got
    (``supervisor.supervision``).

    macOS at ``session`` is the designed guarantee, so OK. Linux that fell back
    to ``session`` is a WARN: it works, with the weaker boundary. A probe that
    could not run is UNKNOWN, never OK.
    """
    platform = platform or sys.platform
    name = "supervision"
    if not platform.startswith("linux"):
        return Check(
            name,
            OK,
            f"supervision: session ({platform}) — {SESSION_BOUNDARY}. Signals to single "
            f"processes are identity-checked just before sending (no pidfd on this platform)",
            probe="sys.platform",
        )
    probe = "python -c <procutil.enable_child_subreaper(); procutil.pidfd_available()>"
    code, out, err = (runner or _subprocess_runner)([sys.executable, "-c", SUPERVISION_PROBE])
    try:
        found = json.loads(out) if code == 0 else None
    except ValueError:
        found = None
    if not isinstance(found, dict):
        tail = (err or out).strip().splitlines()
        return Check(
            name,
            UNKNOWN,
            "could not determine the supervision level: the probe failed "
            f"({tail[-1] if tail else f'exit {code}'}); supervisors still give at least "
            "`session`",
            probe=probe,
        )
    pidfd = (
        "signals to single processes go through a pidfd (race-free)"
        if found.get("pidfd")
        else "pidfds are unavailable here, so signals use the identity-checked kill() path"
    )
    if found.get("subreaper"):
        return Check(name, OK, f"supervision: session+subreaper — {SUBREAPER_BOUNDARY}. {pidfd}",
                     probe=probe)
    return Check(
        name,
        WARN,
        "supervision: session — prctl(PR_SET_CHILD_SUBREAPER) was refused on this Linux "
        f"host, so it has the macOS guarantee: {SESSION_BOUNDARY}. {pidfd}",
        probe=probe,
    )


# --- the whole examination ------------------------------------------------


def diagnose(
    paths: DevctlPaths | None = None,
    *,
    runner: Runner | None = None,
    binding: RuntimeBinding = CLAUDE_CODE,
    claude_home: Path | None = None,
) -> Report:
    """Run every check and return the report. Never raises.

    ``claude_home`` is where Claude Code keeps its plugin register and user
    settings (default ``~/.claude``); injectable so tests never read the real one.
    """
    paths = paths or DevctlPaths.default()
    report = Report()

    report.checks.append(check_shim(COMMAND, runner=runner))
    report.checks.append(check_install_is_durable())
    report.checks.append(check_supervision())

    registry_check = check_registry(paths)
    report.checks.append(registry_check)

    if registry_check.status == OK:
        try:
            registry = Registry.load(paths.registry_file)
        except Exception:
            return report
        for name, entry in sorted(registry.projects.items()):
            if not entry.source_dir:
                report.checks.append(
                    Check(
                        f"hooks:{name}",
                        UNKNOWN,
                        "the registry records no source_dir, so its hooks cannot be located",
                    )
                )
                continue
            report.checks.append(
                check_project_hooks(
                    name, Path(entry.source_dir), binding=binding, claude_home=claude_home
                )
            )
    return report


def main(argv: Sequence[str] | None = None) -> int:
    """``python3 -m rentctl.doctor`` — the shim-independent entry point.

    Deliberately importable and runnable without the console script, because a
    self-check reached *through* the thing it checks cannot report that thing's
    absence.
    """
    report = diagnose()
    print(json.dumps(report.as_dict(), indent=2))
    return report.exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
