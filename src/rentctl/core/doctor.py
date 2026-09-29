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
import os
import shlex
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .paths import DevctlPaths
from .registry import Registry
from .runtimes import CLAUDE_CODE, RuntimeBinding
from .wiring import (
    COMMAND,
    INSTALL_COMMAND,
    OWNED_COMMANDS,
    SERVER_NAME,
    PluginState,
    plugin_state,
)

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


# --- session identity (ADR-0017 §2) -----------------------------------------

# A runtime's project-dir variable is how doctor can tell it is running inside an
# agent session, where an unresolved session id is a degradation, not the norm.
_AGENT_SESSION_MARKERS = ("CLAUDE_PROJECT_DIR", "GEMINI_PROJECT_DIR", "DEVCTL_PROJECT_DIR")


def check_session_identity(environ: dict[str, str] | None = None) -> Check:
    """Which source resolved this process's session id, so a silent fall to
    ``unknown`` shows up (ADR-0017 Consequences).

    Env only, as the MCP server and an agent's ``rent up`` resolve it; hook
    stdin applies only to the hook commands. ``unknown`` in a plain terminal is
    expected and OK. Inside an agent session (a runtime's project-dir variable
    is set) it WARNs: that session's claims all share the anonymous bucket.
    """
    from .service import SESSION_ID_ENVS

    env = os.environ if environ is None else environ
    probe = "env: " + ", ".join(SESSION_ID_ENVS)
    for name in SESSION_ID_ENVS:
        if (env.get(name) or "").strip():
            return Check("session-identity", OK, f"resolved from {name}", probe=probe, source=name)
    in_agent = any(env.get(m) for m in _AGENT_SESSION_MARKERS)
    if in_agent:
        return Check(
            "session-identity", WARN,
            "unknown: an agent project-dir variable is set but no session variable is, so "
            "this session's claims fall into the shared `unknown` bucket and its "
            "SessionEnd cannot release them — they lapse at expiry instead",
            probe=probe, source="unknown",
        )
    return Check(
        "session-identity", OK,
        "unknown: no session variable is set (expected outside an agent session)",
        probe=probe, source="unknown",
    )


# --- Codex (ADR-0018 §8) ---------------------------------------------------

# The three silent failure modes the Codex contract found, each of which leaves
# rentctl looking installed while cleanup or the tools are off:
#   1. the plugin is installed but not enabled;
#   2. its hooks are not trusted — Codex runs no hook, plugin hooks included,
#      until the user trusts it in `/hooks`, so SessionEnd cleanup never fires;
#   3. a `[mcp_servers.rentctl]` in config.toml silently shadows the plugin's
#      server of the same name.
# All three are read from `$CODEX_HOME/config.toml` (default `~/.codex`), the
# file `codex plugin add` and `/hooks` write. Verified on codex-cli 0.153.4.

CODEX_CONFIG = "config.toml"
# `[hooks.state."<plugin>@<marketplace>:hooks/hooks.json:<event>:<group>:<hook>"]`;
# rentctl's hooks file has one group with one hook per event.
CODEX_HOOK_EVENTS = ("session_start", "session_end")
CODEX_HOOKS_FILE = "hooks/hooks.json"
CODEX_INSTALL = (
    "codex plugin marketplace add Michael-Drake/rentctl && "
    f"codex plugin add {SERVER_NAME}@{SERVER_NAME}"
)
CODEX_UNTRUSTED = (
    "not trusted: run /hooks in Codex and trust rentctl's hooks — until then "
    "cleanup falls back to lease expiry"
)


def codex_home(environ: Mapping[str, str] | None = None) -> Path:
    """Where Codex keeps its config: ``$CODEX_HOME``, else ``~/.codex``."""
    env = os.environ if environ is None else environ
    value = (env.get("CODEX_HOME") or "").strip()
    return Path(value).expanduser() if value else Path.home() / ".codex"


def codex_present(home: Path, *, which: Callable[[str], str | None] | None = None) -> bool:
    """Whether Codex is on this machine at all: ``codex`` on PATH, or its home."""
    which = which or shutil.which
    return bool(which("codex")) or home.is_dir()


def _codex_plugin_keys(plugins: Any) -> list[str]:
    """``rentctl@<marketplace>`` keys under ``[plugins]`` — any marketplace."""
    if not isinstance(plugins, dict):
        return []
    return sorted(k for k in plugins if isinstance(k, str) and k.split("@", 1)[0] == SERVER_NAME)


def check_codex(home: Path) -> list[Check]:
    """The Codex plugin's state, its hook trust, and MCP shadowing (ADR-0018 §8).

    Reads ``config.toml`` only; never writes it. What this cannot do is
    recompute Codex's ``trusted_hash``, so a present trust entry is reported as
    exactly that — "trust entry present" — not as "the current hooks are
    trusted": if the hook text changed since the user trusted it, Codex asks
    again, and this check cannot see that.
    """
    config = home / CODEX_CONFIG
    probe = f"read {config}"
    if not config.exists():
        return [Check(
            "codex:plugin", OK,
            f"Codex is present but has no {config}, so the rentctl plugin is not "
            f"installed there (to use rentctl from Codex: {CODEX_INSTALL})",
            probe=probe,
        )]
    try:
        data = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return [Check(
            "codex:plugin", UNKNOWN,
            f"couldn't tell: {config} will not read as TOML ({exc}), so the plugin's "
            "state, its hook trust and MCP shadowing are all unknown",
            probe=probe,
        )]

    checks: list[Check] = []
    plugins = data.get("plugins")
    keys = _codex_plugin_keys(plugins)
    shadow = data.get("mcp_servers")
    shadowed = isinstance(shadow, dict) and SERVER_NAME in shadow

    if not keys:
        checks.append(Check(
            "codex:plugin", OK,
            f"the rentctl plugin is not installed in Codex (to use rentctl from Codex: {CODEX_INSTALL})",
            probe=probe,
        ))
        return checks

    enabled = [k for k in keys if isinstance(plugins[k], dict) and plugins[k].get("enabled") is True]
    unclear = [
        k for k in keys
        if not isinstance(plugins[k], dict) or not isinstance(plugins[k].get("enabled"), bool)
    ]
    if enabled:
        checks.append(Check(
            "codex:plugin", OK, f"{', '.join(enabled)} is installed and enabled in Codex",
            probe=probe,
        ))
    elif unclear:
        checks.append(Check(
            "codex:plugin", UNKNOWN,
            f"couldn't tell: [plugins.\"{unclear[0]}\"] in {config} has no boolean "
            "`enabled`, so whether Codex loads it is unknown",
            probe=probe,
        ))
    else:
        checks.append(Check(
            "codex:plugin", WARN,
            f"{', '.join(keys)} is installed in Codex but disabled: its tools and "
            f"cleanup hooks do not load (set enabled = true under [plugins.\"{keys[0]}\"] "
            f"in {config})",
            probe=probe,
        ))

    checks.append(_check_codex_hook_trust(enabled or keys, data, config, probe))

    if shadowed:
        checks.append(Check(
            "codex:mcp-shadow", WARN,
            f"[mcp_servers.{SERVER_NAME}] in {config} silently shadows the plugin's "
            f"MCP server of the same name, so Codex runs that entry instead: delete "
            f"the [mcp_servers.{SERVER_NAME}] table and let the plugin supply the server",
            probe=probe,
        ))
    else:
        checks.append(Check(
            "codex:mcp-shadow", OK,
            f"no [mcp_servers.{SERVER_NAME}] in {config} shadows the plugin's server",
            probe=probe,
        ))
    return checks


def _check_codex_hook_trust(keys: list[str], data: dict[str, Any], config: Path, probe: str) -> Check:
    hooks = data.get("hooks", {})
    state = hooks.get("state", {}) if isinstance(hooks, dict) else None
    if not isinstance(state, dict):
        return Check(
            "codex:hooks", UNKNOWN,
            f"couldn't tell: [hooks.state] in {config} is not a table, so hook trust is unknown",
            probe=probe,
        )
    for key in keys:
        entries = [f"{key}:{CODEX_HOOKS_FILE}:{event}:0:0" for event in CODEX_HOOK_EVENTS]
        missing = [
            e for e in entries
            if not (isinstance(state.get(e), dict)
                    and isinstance(state[e].get("trusted_hash"), str)
                    and state[e]["trusted_hash"].strip())
        ]
        if not missing:
            return Check(
                "codex:hooks", OK,
                f"trust entry present for {key}'s SessionStart and SessionEnd hooks "
                "(the hash itself cannot be checked here; if the hook text changed, "
                "Codex asks again in /hooks)",
                probe=probe,
            )
    events = ", ".join(m.rsplit(":", 3)[1] for m in missing)
    return Check(
        "codex:hooks", WARN,
        f"{keys[-1]}'s hooks are {CODEX_UNTRUSTED} (no trusted_hash for: {events})",
        probe=probe,
    )


# --- the whole examination ------------------------------------------------


# --- does the INSTALLED plugin wire the hooks this rentctl expects? ------------
#
# Found by the 1.2.0 live acceptance run (D2): a plugin whose SessionEnd command
# was broken passed `claude plugin validate --strict`, and `rent doctor` said
# `hooks: ok` because the plugin was enabled and `rent` resolved. Nothing read
# the hook the client would actually run, so cleanup fell silently to expiry.
# This reads the installed copy — the bytes the client loads — and compares it
# with the render this rentctl ships. It cannot see a plugin a session loaded
# with `--plugin-dir`; it speaks for installs.

_HOOK_EVENTS = ("SessionStart", "SessionEnd")


def _hook_commands_in(events: Any) -> dict[str, list[str]] | None:
    if not isinstance(events, dict):
        return None
    out: dict[str, list[str]] = {}
    for event in _HOOK_EVENTS:
        cmds: list[str] = []
        for group in events.get(event) or []:
            for hook in (group.get("hooks") if isinstance(group, dict) else None) or []:
                if isinstance(hook, dict) and isinstance(hook.get("command"), str):
                    cmds.append(hook["command"])
        out[event] = cmds
    return out


def installed_hook_commands(plugin_root: Path, *, inline_ok: bool) -> dict[str, list[str]] | None:
    """The SessionStart/SessionEnd commands a client loads from an installed plugin.

    ``hooks/hooks.json`` (the wrapped shape) is what both clients load.
    ``inline_ok`` also accepts a manifest's bare inline map — Claude Code loads
    that (rentctl 1.0.x/1.1.x shipped it); Codex silently ignores it. ``None``
    when ``hooks/hooks.json`` exists but will not parse.
    """
    found: dict[str, list[str]] = {e: [] for e in _HOOK_EVENTS}
    hooks_file = plugin_root / "hooks" / "hooks.json"
    if hooks_file.is_file():
        try:
            data = json.loads(hooks_file.read_text())
        except (OSError, ValueError):
            return None
        got = _hook_commands_in(data.get("hooks") if isinstance(data, dict) else None)
        if got is None:
            return None
        for e in _HOOK_EVENTS:
            found[e] += got[e]
    manifest = plugin_root / ".claude-plugin" / "plugin.json"
    if inline_ok and manifest.is_file():
        try:
            data = json.loads(manifest.read_text())
        except (OSError, ValueError):
            data = {}
        got = _hook_commands_in(data.get("hooks") if isinstance(data, dict) else None)
        if got is not None:
            for e in _HOOK_EVENTS:
                found[e] += got[e]
    return found


def check_installed_plugin_hooks(name: str, plugin_root: Path, *, inline_ok: bool) -> Check:
    from .wiring import plugin_hooks_fragment

    probe = f"read {plugin_root}/hooks/hooks.json" + (
        " and .claude-plugin/plugin.json#hooks" if inline_ok else ""
    ) + "; compared with this rentctl's render"
    if not plugin_root.is_dir():
        return Check(name, WARN, f"the install register names {plugin_root}, which does not exist — "
                     "reinstall the plugin", probe)
    got = installed_hook_commands(plugin_root, inline_ok=inline_ok)
    if got is None:
        return Check(name, UNKNOWN, f"couldn't tell: {plugin_root}/hooks/hooks.json will not "
                     "parse as a hook file", probe)
    want = _hook_commands_in(plugin_hooks_fragment()) or {}
    if not got["SessionEnd"]:
        return Check(name, FAIL, f"the installed plugin at {plugin_root} gives the client no "
                     "SessionEnd hook it will load — session-end cleanup is OFF and "
                     "environments stop only at lease expiry. Reinstall or update the plugin.",
                     probe)
    if got == want:
        return Check(name, OK, f"the installed plugin at {plugin_root} wires SessionStart and "
                     "SessionEnd exactly as this rentctl renders them", probe)
    return Check(name, WARN, f"the installed plugin at {plugin_root} wires hooks that differ "
                 "from this rentctl's (a plugin and package at different versions, or an "
                 "edited install): session-end cleanup may not run as documented. Update the "
                 "plugin to match `rent --version`.", probe)


def claude_plugin_roots(claude_home: Path | None = None) -> list[Path]:
    """Install paths of our plugin in Claude Code's register, any scope."""
    from .wiring import _claude_home

    home = _claude_home(claude_home)
    try:
        entries = json.loads((home / "plugins" / "installed_plugins.json").read_text())["plugins"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    roots: list[Path] = []
    for key, installs in (entries.items() if isinstance(entries, dict) else []):
        if not isinstance(key, str) or key.split("@", 1)[0] != SERVER_NAME:
            continue
        for install in installs if isinstance(installs, list) else []:
            p = install.get("installPath") if isinstance(install, dict) else None
            if isinstance(p, str) and Path(p) not in roots:
                roots.append(Path(p))
    return roots


def codex_plugin_roots(home: Path) -> list[Path]:
    """Cached install dirs of our plugin in Codex (``plugins/cache/<mkt>/rentctl/<ver>``)."""
    base = home / "plugins" / "cache"
    return sorted(p for p in base.glob(f"*/{SERVER_NAME}/*") if p.is_dir()) if base.is_dir() else []


def diagnose(
    paths: DevctlPaths | None = None,
    *,
    runner: Runner | None = None,
    binding: RuntimeBinding = CLAUDE_CODE,
    claude_home: Path | None = None,
    codex_home_dir: Path | None = None,
) -> Report:
    """Run every check and return the report. Never raises.

    ``claude_home`` is where Claude Code keeps its plugin register and user
    settings (default ``~/.claude``); ``codex_home_dir`` is Codex's
    (default ``$CODEX_HOME`` or ``~/.codex``). Both injectable so tests never
    read the real ones. The Codex checks appear only when Codex is present.
    """
    paths = paths or DevctlPaths.default()
    report = Report()

    report.checks.append(check_shim(COMMAND, runner=runner))
    report.checks.append(check_install_is_durable())
    report.checks.append(check_supervision())
    report.checks.append(check_session_identity())

    for root in claude_plugin_roots(claude_home):
        report.checks.append(check_installed_plugin_hooks("plugin-hooks:claude-code", root, inline_ok=True))

    home = codex_home_dir if codex_home_dir is not None else codex_home()
    if codex_present(home):
        report.checks.extend(check_codex(home))
        for root in codex_plugin_roots(home):
            report.checks.append(check_installed_plugin_hooks("plugin-hooks:codex", root, inline_ok=False))

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
