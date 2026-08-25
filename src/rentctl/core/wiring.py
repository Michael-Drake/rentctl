# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Drake

"""Enrollment wiring — MCP registration, session hooks, policy rule (ADR-0002 §1, §5, §7; ADR-0011).

**This module is the single source the hooks are authored in** (ADR-0002 §5).
Both delivery paths render from here: `rent init` writes them directly, and the
Claude Code plugin manifest is generated from the same fragments. Two paths that
are renders of one source are identical by construction; a hand-copy kept in step
by discipline is the exact shape behind every settings-drift incident this system
has hit, and it must not be recreated inside the tool that fixes it.

Enrollment installs **three** things per runtime, and the third is new
(ADR-0011):

1. the MCP server, so the agent has the tools;
2. the session hooks, so cleanup happens whether or not the agent cooperates;
3. a **policy rule in the project's own context file**, so an agent that cannot
   see the tools still knows not to start a server by hand.

(3) exists because (1) is silent when it is missing. An agent with no rentctl
tools does not conclude "servers are leased here and I should ask" — it
concludes nothing, and starts the server itself. The rule is the only part of
enrollment that survives a runtime rentctl has never heard of.

Merging, not replacing, is the whole job. These files almost always already
exist with other content, and a project may have its own SessionEnd hook.
Enrollment that clobbers either breaks the project it was meant to enroll.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .runtimes import CLAUDE_CODE, HOOK_TIMEOUT, RuntimeBinding

# The MCP server map key and the server's advertised identity (ADR-0009, WI-0017).
# The *distribution* name, not the command: this is what the MCP registry entry is
# published as (§1) and what namespaces the tools a model sees — `mcp__rentctl__env_up`.
# `rent` is what a human types; the two surfaces have different audiences (§2).
SERVER_NAME = "rentctl"

# What the server map key was before ADR-0009. Two enrolled projects have it
# committed in `.mcp.json`, and enrollment has to **rename** those rather than add
# a second entry — see :func:`merge_mcp_config`. This is the server-key twin of
# the ownership problem :data:`OWNED_COMMANDS` solves for hooks.
LEGACY_SERVER_NAME = "devctl"

# The console script the hooks invoke. Both names ship until the enrolled projects
# migrate and the gate passes (ADR-0009 §4), so a hook written either way runs —
# and :func:`install_hooks_detailed` repairs an old spelling to this one, because
# a stale hook is owned (:data:`OWNED_COMMANDS`) rather than foreign.
COMMAND = "rent"

# `--reason session-end` is what makes these teardowns countable as layer-2
# evidence (ADR-0006; spec §11.1 G4). Without it the teardown is still recorded
# but the reason is marked `inferred`, because a human typing `rent down --all`
# produces an identical call — and inferred events do not prove the hook fired.
# Enrollment works either way; only the pilot evidence weakens, which is precisely
# the kind of silent loss that is hard to notice.
SESSION_START_COMMAND = f"{COMMAND} sweep"

# The policy rule (ADR-0011 §3). Delimited so it can be updated in place and
# recognised on re-enrollment without a second copy anywhere.
#
# The delimiters keep the **old** spelling deliberately. They are not a label, they
# are the anchor an already-written rule is found by: rename them and every project
# carrying the old block becomes unrecognisable, so the next `init` appends a second
# rule instead of updating the first. The marker is matched, never read — a user
# sees the body, and the body says `rent`.
RULE_BEGIN = "<!-- devctl:begin -->"
RULE_END = "<!-- devctl:end -->"

RULE_BODY = """\
## Dev servers here are leased — start them through rentctl

This project's dev servers are managed by **rentctl**. To start one, use the
`env_up` MCP tool if you have it, or run `rent up <project>`.

**Do not start the dev server yourself** — not `npm run dev`, not `vite`, not
`python -m http.server`, not a background shell job. A server started by hand
outlives the session that started it; that is the entire failure rentctl exists
to prevent. rentctl leases every server so it dies at session end, at lease
expiry, or on request, whichever comes first.

**If rentctl is not available to you, stop and say so.** Do not start the server
manually as a fallback — that is the case this rule is written for."""


def session_end_command(binding: RuntimeBinding = CLAUDE_CODE) -> str:
    """The SessionEnd teardown, spelled for one runtime.

    The `--cwd` argument is what scopes teardown to *this* project, and each
    runtime names that directory differently. A runtime with no such variable
    gets the argument omitted rather than a broken expansion: rentctl then
    resolves the caller directory itself, and an empty `--cwd ""` would have
    been indistinguishable from a deliberate one.
    """
    if binding.project_dir_env is None:
        return f"{COMMAND} down --all --reason session-end"
    return f'{COMMAND} down --all --cwd "${binding.project_dir_env}" --reason session-end'


def hooks_fragment(binding: RuntimeBinding = CLAUDE_CODE) -> dict[str, Any]:
    """The session hooks, as they appear under a settings file's hooks key.

    SessionEnd is layer 2 — the reason an MCP server alone is half the product:
    it can start environments and cannot guarantee they die.
    """

    def entry(name: str, command: str) -> dict[str, Any]:
        hook: dict[str, Any] = {"type": "command", "command": command, "timeout": HOOK_TIMEOUT}
        if binding.hook_entries_named:
            hook = {"name": name, **hook}
        return hook

    return {
        "SessionEnd": [
            {"matcher": "*", "hooks": [entry(f"{COMMAND}-down", session_end_command(binding))]}
        ],
        "SessionStart": [
            {"matcher": "startup", "hooks": [entry(f"{COMMAND}-sweep", SESSION_START_COMMAND)]}
        ],
    }


def mcp_fragment() -> dict[str, Any]:
    """Our entry for an MCP config's ``mcpServers`` object.

    One stdio instance per session; it holds no authoritative state (spec §3.1).
    Requires ``rent-mcp`` on PATH.
    """
    return {"command": f"{COMMAND}-mcp", "args": []}


def plugin_manifest() -> dict[str, Any]:
    """The Claude Code plugin manifest, rendered from this module (ADR-0002 §5).

    The plugin is the only distribution channel that installs the **hooks** as
    well as the MCP server, and an MCP server alone is half the product: it can
    start environments and cannot guarantee they die. So a stranger who installs
    from PyPI alone still has to wire cleanup by hand — which restates the
    problem ADR-0002 exists to remove.

    **Rendered, never authored.** The hooks and the server entry come from
    :func:`hooks_fragment` and :func:`mcp_fragment`, the same functions `init`
    writes from, so the plugin and the CLI cannot disagree. A hand-kept second
    copy is the exact drift this module's docstring forbids, and the checked-in
    artifact is guarded by a test that re-renders and compares.

    Both are inlined rather than pointed at sibling files. The schema allows
    either; inlining keeps the whole plugin one generated artifact, so there is
    no second file that can be regenerated out of step with the first.

    ``version`` is deliberately absent. Only ``name`` is required, and rentctl's
    version is an open decision (WI-0023) — emitting a number here would mint a
    version claim from a build script rather than a release.
    """
    return {
        "name": SERVER_NAME,
        "description": (
            "Leased dev environments for AI coding sessions — a dev server can never "
            "outlive the work that needed it."
        ),
        "author": {"name": "Michael Drake"},
        # `Michael-Drake` IS the canonical casing — verified against the GitHub
        # API, which resolves the name case-insensitively but reports
        # `"login": "Michael-Drake"`. A pre-publish review asserted the opposite
        # and it was briefly "fixed" to lowercase before the probe was run.
        # Keep in step with `[project.urls]` in pyproject.toml.
        "repository": "https://github.com/Michael-Drake/rentctl",
        "license": "Apache-2.0",
        "keywords": ["dev-server", "lease", "cleanup", "mcp", "ports"],
        "hooks": {"hooks": hooks_fragment(CLAUDE_CODE)},
        "mcpServers": {SERVER_NAME: mcp_fragment()},
    }


def render_plugin_manifest() -> str:
    """The manifest as the bytes that belong in ``.claude-plugin/plugin.json``.

    ``ensure_ascii=False`` for the same reason :func:`_write_json_atomic` uses it,
    and it was got wrong here first: the default mangles the em-dash in the
    description into a ``\\uXXXX`` escape, which is valid JSON and unreadable in
    the field a user reads to decide whether to install this.
    """
    return json.dumps(plugin_manifest(), indent=2, ensure_ascii=False) + "\n"


def render_hooks_text(binding: RuntimeBinding = CLAUDE_CODE) -> str:
    """The hook fragment as text, for ``--print-hooks`` (ADR-0002 §7).

    Emits exactly what :func:`install_hooks` would have written, so a nonstandard
    consumer routing it by hand gets the same bytes rather than an approximation.
    """
    # `ensure_ascii=False` for the same reason as :func:`_write_json_atomic`, and
    # pre-emptively: these bytes are destined for a settings file rentctl does not
    # own. Nothing in a hook fragment is non-ASCII today, so this changes no
    # output — but its two siblings both had to be fixed after the fact, and the
    # asymmetry of leaving one alone is what let the bug come back the same day.
    return json.dumps({"hooks": hooks_fragment(binding)}, indent=2, ensure_ascii=False)


# --- merging --------------------------------------------------------------

def _read_json_object(path: Path) -> dict[str, Any]:
    """Read a JSON object, or ``{}`` when absent.

    A file that exists but does not parse **raises**. Overwriting a config we
    cannot read is how enrollment eats someone else's settings; refusing is the
    only safe answer, and the caller surfaces it.
    """
    if not path.exists():
        return {}
    text = path.read_text()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"{path} exists but is not valid JSON ({e}) — refusing to overwrite") from e
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object — refusing to overwrite")
    return data


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    """Rewrite a config file, changing only what enrollment meant to change.

    ``ensure_ascii=False`` is the whole point of this docstring. These are files
    rentctl **does not own** — a project's own settings, usually generated and
    usually checked in. The default ``ensure_ascii=True`` re-encodes every
    non-ASCII character it round-trips, so an em-dash in a comment the project
    wrote comes back as ``\\u2014``: semantically identical JSON, and a diff
    across every prose line in the file. Observed on a real enrolled project,
    where a one-line hook repair produced a five-line comment diff nobody asked for.

    A file we touch to change one hook must come back byte-identical everywhere
    else, or the project cannot see what we did — and in a generated file, ours
    is the diff their next regeneration silently reverts.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def merge_mcp_config(path: Path, binding: RuntimeBinding = CLAUDE_CODE) -> bool:
    """Add our server to the runtime's MCP server map, preserving everything else.

    **A legacy ``devctl`` key is renamed, not joined.** Every other key in the map
    is somebody else's server and is preserved untouched, but that one is a former
    name for *this* server: leaving it beside the new key would start two stdio
    instances advertising the same four tool names in one session, so a model
    would see `env_up` twice with no way to tell which lease board it addressed.
    This is the one case where enrollment removes a key it did not just write,
    and it is safe precisely because the key is ours.

    This is the server-map twin of what :data:`OWNED_COMMANDS` does for hooks —
    the same rename, the same both-directions failure, a different file.

    Idempotent. Returns True if the file changed.
    """
    data = _read_json_object(path)
    servers = data.get(binding.mcp_key)
    if not isinstance(servers, dict):
        servers = {}
    legacy_present = LEGACY_SERVER_NAME in servers
    if servers.get(SERVER_NAME) == mcp_fragment() and not legacy_present:
        return False
    servers.pop(LEGACY_SERVER_NAME, None)
    servers[SERVER_NAME] = mcp_fragment()
    data[binding.mcp_key] = servers
    _write_json_atomic(path, data)
    return True


def rename_approved_mcp_server(
    path: Path, binding: RuntimeBinding = CLAUDE_CODE
) -> bool:
    """Carry an existing approval of our server across the ADR-0009 rename.

    :func:`merge_mcp_config` renames the server key *inside* the MCP file. The
    runtime also records which of those servers are **approved**, in a different
    file, referring to the server **by name** — so renaming one and not the other
    leaves a project approving a server that no longer exists while the one that
    does exist is unlisted. Both pilot projects were in exactly that state
    (WI-0039): `.mcp.json` said ``rentctl``, the approval list said ``devctl``.

    **Renames only; never adds.** The list is an approval — "specific MCP servers
    from `.mcp.json` files to approve" — so writing ourselves into it where the
    user never put us would be rentctl granting itself a permission on the user's
    behalf. Renaming preserves a decision already made; adding manufactures one.
    A project that has not approved the server keeps that state and is asked by
    the runtime's own gate, which is the correct place for the question.

    A runtime with no approval concept (``mcp_enable_file`` is ``None``) is a
    no-op, not an error.

    Idempotent. Returns True if the file changed.
    """
    if binding.mcp_enable_key is None or not path.exists():
        return False
    data = _read_json_object(path)
    approved = data.get(binding.mcp_enable_key)
    if not isinstance(approved, list) or LEGACY_SERVER_NAME not in approved:
        return False
    # Positional rename, and de-duplicated: a list already carrying both names
    # must not come back with SERVER_NAME twice.
    renamed: list[Any] = []
    for name in approved:
        name = SERVER_NAME if name == LEGACY_SERVER_NAME else name
        if name not in renamed:
            renamed.append(name)
    if renamed == approved:
        return False
    data[binding.mcp_enable_key] = renamed
    _write_json_atomic(path, data)
    return True


# Every console name rentctl has ever shipped under (ADR-0009 §2/§4). Hook
# ownership is decided by these and nothing else.
#
# The rename is exactly why this is a set rather than one prefix. Every enrolled
# project has `devctl …` baked into a committed hook, and ADR-0009 §4 drops those
# aliases once the projects migrate and the gate passes. A matcher that knew only
# the current name would fail in both directions at that moment: it would leave
# the dead `devctl` entry in place *and* append a second `rent` entry beside it,
# so the one command meant to fix enrollment could not fix the rename it is
# enrolling for.
OWNED_COMMANDS = frozenset({
    "devctl", "devctl-watchdog", "devctl-mcp",
    "rent", "rent-watchdog", "rent-mcp",
})


# A settings file that announces itself as machine-generated. Claude Code's
# some generators write a `//generated` key whose value says so in prose, and
# the convention of a `//`-prefixed comment key is common to hand-rolled JSON
# config generally. Matching the KEY rather than its prose keeps this from
# depending on wording that is not ours.
_GENERATED_KEYS = ("//generated", "//GENERATED", "_generated")


def generated_marker(path: Path) -> str | None:
    """The name of the marker declaring this file machine-generated, or ``None``.

    Writing into a generated file is not merely untidy — the write **reverts**.
    Some setups render each project's `.claude/settings.json` from a shared
    floor plus that member's own `settings_extras`, so a hook merged here works
    until the next fleet push and then vanishes, "a failure that surfaces long
    after its cause."

    That is not hypothetical. rentctl added `--reason session-end` to an enrolled
    project's generated settings; a refresh of that generated file stripped it
    three days later; and for those three days every teardown in that project
    recorded as `inferred` and counted toward no evidence. The hook kept working,
    so nothing surfaced it — only the evidence quietly stopped.
    """
    try:
        data = _read_json_object(path)
    except ValueError:
        return None  # unparseable is a different failure, handled by the writer
    return next((k for k in _GENERATED_KEYS if k in data), None)


@dataclass(frozen=True)
class HookResult:
    """What :func:`install_hooks_detailed` did, per event.

    ``repaired`` is kept apart from ``installed`` because the two mean different
    things to a user: one added wiring that was absent, the other **overwrote a
    line already in their settings file**. Folding them into one boolean is how a
    silent rewrite happens.

    ``provisional`` carries the reason this write is expected not to survive —
    set when the target announces itself as generated. It is deliberately not a
    refusal: the write is still the best available repair *right now*, and
    the hook in question did need fixing. What must not happen is reporting it
    as done when it will be reverted, so the caveat travels with the result
    rather than being absorbed.
    """

    installed: tuple[str, ...] = ()
    repaired: tuple[str, ...] = ()
    provisional: str | None = None

    @property
    def changed(self) -> bool:
        return bool(self.installed or self.repaired)


def install_hooks_detailed(
    path: Path, binding: RuntimeBinding = CLAUDE_CODE
) -> HookResult:
    """Install the session hooks, **repairing** a rentctl entry that has drifted.

    Three cases, and the third is the one that was missing:

    * **absent** — rentctl owns no entry for this event: append, leaving any
      hooks the project authored itself untouched.
    * **current** — rentctl's entry already matches what we would write: no-op.
    * **stale** — rentctl owns an entry that differs from the current render:
      **replace that entry in place.**

    Before this, any command beginning ``devctl `` counted as "already wired,"
    so a drifted hook survived every re-run while `init` reported success. A real
    project's ``SessionEnd`` was missing ``--reason session-end`` for exactly
    that reason: rentctl kept certifying it as fine. Repairing absent wiring while
    silently blessing stale wiring is the fail-open shape of
    :doc:`ADR-0008 <../../adr/0008-port-probe-cannot-tell-is-not-nobody-there>` —
    a check whose "all clear" and "did not look" are the same output.

    Only entries rentctl **owns** (:data:`OWNED_COMMANDS`) are ever rewritten. A
    hook the project wrote is never touched, whatever it does.

    When the target announces itself as machine-generated
    (:func:`generated_marker`), the write still happens but the result carries
    ``provisional`` — see :class:`HookResult`.
    """
    marker = generated_marker(path)
    data = _read_json_object(path)
    hooks = data.get(binding.hooks_key)
    if not isinstance(hooks, dict):
        hooks = {}

    installed: list[str] = []
    repaired: list[str] = []

    for event, groups in hooks_fragment(binding).items():
        desired = groups[0]
        existing = hooks.get(event)
        if not isinstance(existing, list):
            existing = []
        else:
            existing = list(existing)

        owned_at = next((i for i, g in enumerate(existing) if _group_is_ours(g)), None)
        if owned_at is None:
            existing.append(desired)
            installed.append(event)
        elif existing[owned_at] != desired:
            existing[owned_at] = desired
            repaired.append(event)
        else:
            continue
        hooks[event] = existing

    provisional = None
    if marker and (installed or repaired):
        provisional = (
            f"{path} declares itself machine-generated ({marker}); this write will "
            "be reverted the next time it is regenerated. Hooks belong in a file "
            "nothing regenerates — re-run without pointing enrollment at this one, "
            "or add the fragment to whatever input feeds this file "
            f"(`{COMMAND} init --print-hooks`)."
        )

    result = HookResult(tuple(installed), tuple(repaired), provisional)
    if not result.changed:
        return result
    data[binding.hooks_key] = hooks
    _write_json_atomic(path, data)
    return result


def install_hooks(path: Path, binding: RuntimeBinding = CLAUDE_CODE) -> bool:
    """Bool-returning wrapper over :func:`install_hooks_detailed`."""
    return install_hooks_detailed(path, binding).changed


def remove_hooks(path: Path, binding: RuntimeBinding = CLAUDE_CODE) -> tuple[str, ...]:
    """Drop rentctl-owned hook groups from ``path``. Returns the events cleaned.

    The migration half of moving the hook target (ADR-0014). Leaving the old copy
    behind does **not** shadow it: the runtime *merges* hook entries across
    settings levels, so a project enrolled before the move would fire both. Two
    teardowns are not a correctness problem — the second finds no lease and
    records nothing, the same way a losing watchdog does — but they race, and if
    the stale spelling wins the event is attributed to the wrong reason. Removing
    it is how the move actually completes.

    Only groups rentctl **owns** (:data:`OWNED_COMMANDS`) are removed; a hook the
    project wrote is never touched, and an event left with no groups has its key
    dropped rather than left as an empty list.

    Returns ``()`` when there was nothing of ours to remove, which is the normal
    case for a project enrolled after the move and for one never enrolled at all.
    A file that does not exist is not an error — there is nothing to clean.
    """
    if not path.exists():
        return ()
    data = _read_json_object(path)
    hooks = data.get(binding.hooks_key)
    if not isinstance(hooks, dict):
        return ()

    cleaned: list[str] = []
    for event in list(hooks):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        kept = [g for g in groups if not _group_is_ours(g)]
        if len(kept) == len(groups):
            continue
        cleaned.append(event)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]

    if not cleaned:
        return ()
    if hooks:
        data[binding.hooks_key] = hooks
    else:
        # An empty `hooks` map is not the same as no map. Leaving `"hooks": {}`
        # behind in someone's settings is a diff that says rentctl still has an
        # opinion about this file when it no longer does.
        data.pop(binding.hooks_key, None)
    _write_json_atomic(path, data)
    return tuple(cleaned)


def _group_is_ours(group: Any) -> bool:
    """Does this hook group belong to us?

    Ownership is by the **command's first token** against
    :data:`OWNED_COMMANDS` — not a prefix test. ``startswith("devctl ")`` would
    miss every ``rent`` spelling after the rename, and a bare prefix would also
    capture a project's own ``./devctl-wrapper.sh``, which we have no business
    overwriting.
    """
    if not isinstance(group, dict):
        return False
    for hook in group.get("hooks", []):
        if not isinstance(hook, dict):
            continue
        command = str(hook.get("command", "")).strip()
        first = command.split(maxsplit=1)
        if first and first[0] in OWNED_COMMANDS:
            return True
    return False


# --- the policy rule (ADR-0011 §3) ----------------------------------------

_RULE_RE = re.compile(
    re.escape(RULE_BEGIN) + r".*?" + re.escape(RULE_END), re.DOTALL
)


def render_rule() -> str:
    """The delimited policy block, exactly as written into a context file."""
    return f"{RULE_BEGIN}\n{RULE_BODY}\n{RULE_END}"


def install_rule(path: Path) -> bool:
    """Write the leased-servers policy into a project's agent-context file.

    Appends when absent, **replaces in place** when the delimited block is
    already there — so a project that reorganised its context file keeps its
    ordering, and an updated rule does not accumulate copies.

    Creates the file when missing: a project with no ``CLAUDE.md`` is exactly a
    project whose agent has nowhere to learn the policy from, which is the case
    that produced the bug.

    Returns True if the file changed.
    """
    block = render_rule()
    existing = path.read_text() if path.exists() else ""

    if _RULE_RE.search(existing):
        updated = _RULE_RE.sub(lambda _: block, existing, count=1)
    elif existing.strip():
        updated = existing.rstrip("\n") + "\n\n" + block + "\n"
    else:
        updated = block + "\n"

    if updated == existing:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(updated)
    os.replace(tmp, path)
    return True


# --- plugin detection (ADR-0002 §5) ---------------------------------------

def plugin_installed(claude_home: Path | None = None) -> bool:
    """Whether our Claude Code plugin is installed for this user, under either name.

    **Provisional.** The plugin does not exist yet (ADR-0002 downstream item 1),
    so this checks the conventional install location and nothing more. When the
    plugin ships with a real manifest, this becomes a manifest read — the call
    sites do not change, which is why the check is a function rather than an
    inline path test.

    Defaults to absent, which is the safe direction: `init` writes the hooks
    itself, and a duplicate is prevented by :func:`install_hooks` idempotence
    rather than by this guess being right.
    """
    home = Path(claude_home) if claude_home is not None else Path.home() / ".claude"
    plugins = home / "plugins"
    return (plugins / SERVER_NAME).is_dir() or (plugins / LEGACY_SERVER_NAME).is_dir()
