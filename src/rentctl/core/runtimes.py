# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Drake

"""Agent runtimes devctl can enroll a project for (ADR-0011).

devctl's promise is that a dev server cannot outlive the work that needed it.
That promise is delivered through an agent runtime — an MCP server it can call
and session hooks that fire when it exits. Until now exactly one runtime was
wired, and it was wired *inline*: ``.mcp.json`` and ``.claude/settings.json``,
named directly in the enrollment code.

An agent on any other runtime therefore saw no devctl at all. That is not a
gap in coverage, it is a **fail-open in the safety mechanism**: on 2026-07-30 a
Gemini session in a worktree lane looked for rentctl, found nothing, and started a
dev server by hand. A human happened to be watching. Absence was silent, and
silence read as "no policy applies here."

This module makes the runtime a **binding** — data, not code — so the contract
("register the broker, install the cleanup hooks, state the policy") is written
once and each runtime says only where its files live and what its variables are
called. Adding a runtime is a table entry.

Every field below was verified against an installed runtime rather than
inferred; the ``verified`` field records when. A binding written from memory is
how you ship a fix that does not fix.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

HOOK_TIMEOUT = 30


@dataclass(frozen=True)
class RuntimeBinding:
    """Where one agent runtime keeps the three things enrollment must touch."""

    id: str
    label: str
    # Relative to the project root. MCP servers and hooks may share a file
    # (Gemini) or be separate (Claude Code) — both are merged, never replaced.
    mcp_file: tuple[str, ...]
    mcp_key: str
    # Where this runtime records which project MCP servers are APPROVED, when it
    # has such a concept (``None`` when it does not). Separate from ``mcp_file``
    # because it is a different file referring to the server BY NAME — which is
    # how the ADR-0009 rename came to be half-applied: the server key in
    # ``mcp_file`` was renamed and this list still named the old one (WI-0039).
    mcp_enable_file: tuple[str, ...] | None
    mcp_enable_key: str | None
    hooks_file: tuple[str, ...]
    hooks_key: str
    # A file this runtime's hooks used to be written to, and which enrollment
    # must now sweep clean of devctl-owned entries. ``None`` when the target has
    # never moved. Without it a migrated project carries the hook in two places:
    # the runtime merges hook entries across settings levels, so the stale copy
    # keeps firing rather than being shadowed.
    legacy_hooks_file: tuple[str, ...] | None
    # The repo-level instruction file the agent reads as context.
    context_file: str
    # The runtime's own "where is the project" variable, used inside the hook
    # command. ``None`` means the hook must rely on its working directory.
    project_dir_env: str | None
    # Hook entries carry a human-readable name in this runtime's schema.
    hook_entries_named: bool
    # What proves the runtime is used in a project, and what proves it is
    # installed on the machine.
    project_markers: tuple[tuple[str, ...], ...]
    executable: str | None
    verified: str

    def mcp_path(self, root: Path) -> Path:
        return root.joinpath(*self.mcp_file)

    def mcp_enable_path(self, root: Path) -> Path | None:
        if self.mcp_enable_file is None:
            return None
        return root.joinpath(*self.mcp_enable_file)

    def hooks_path(self, root: Path) -> Path:
        return root.joinpath(*self.hooks_file)

    def legacy_hooks_path(self, root: Path) -> Path | None:
        if self.legacy_hooks_file is None:
            return None
        return root.joinpath(*self.legacy_hooks_file)

    def context_path(self, root: Path) -> Path:
        return root / self.context_file

    def configured_in(self, root: Path) -> bool:
        """Is this runtime already used in this project?"""
        return any(root.joinpath(*marker).exists() for marker in self.project_markers)

    def installed(self) -> bool:
        """Is this runtime present on the machine at all?"""
        return self.executable is not None and shutil.which(self.executable) is not None


# Hooks go to `.claude/settings.local.json`, NOT `.claude/settings.json` — see
# ADR-0014. `settings.json` is a file other tooling legitimately *generates*: a
# project may render it from a shared floor plus its own inputs, so a hook merged
# there works until the next regeneration and then vanishes. That is not
# hypothetical — it cost one enrolled project three days of uncountable teardowns,
# and the failure is silent by construction, because the hook keeps working right
# up until it is gone.
#
# devctl's answer used to be a warning: write anyway, report `provisional`, and
# tell a human to re-enter the hooks in whatever file feeds their generator. That
# makes enrollment a two-party job for exactly the projects most likely to have
# automated their setup, and it is not something a stranger installing from PyPI
# can be asked to do.
#
# `settings.local.json` is the runtime's own per-project, non-generated location,
# and hooks written there are additive rather than overriding. Verified against
# code.claude.com/docs/en/hooks: "Hook entries merge across settings levels rather
# than replacing each other: user, project, and local settings add their own hooks
# without removing managed ones", and "identical handlers are deduplicated
# automatically" — so a project that also carries an identical hook in
# `settings.json` fires it once, not twice.
#
# It is gitignored, which is the right scope rather than a concession: the registry
# this hook depends on is `~/.config/devctl/registry.json`, already machine-local,
# and v1 is single-machine by design (spec §2). A committed hook on a machine with
# no registry entry fails closed on UNKNOWN_PROJECT, so it never bought portability
# in the first place.
CLAUDE_CODE = RuntimeBinding(
    id="claude-code",
    label="Claude Code",
    mcp_file=(".mcp.json",),
    mcp_key="mcpServers",
    # "List of specific MCP servers from `.mcp.json` files to approve"
    # (code.claude.com/docs/en/settings). An APPROVAL list, which is why
    # enrollment only ever renames an entry already in it — see
    # :func:`~rentctl.core.wiring.rename_approved_mcp_server`.
    mcp_enable_file=(".claude", "settings.local.json"),
    mcp_enable_key="enabledMcpjsonServers",
    hooks_file=(".claude", "settings.local.json"),
    hooks_key="hooks",
    legacy_hooks_file=(".claude", "settings.json"),
    context_file="CLAUDE.md",
    project_dir_env="CLAUDE_PROJECT_DIR",
    hook_entries_named=False,
    project_markers=((".mcp.json",), (".claude",), ("CLAUDE.md",)),
    executable="claude",
    verified="2026-08-01 — hook merge/dedup across settings levels, per the runtime's own docs",
)

# Verified 2026-07-30 against gemini-cli 0.35.2 by running `gemini mcp add -s
# project` in a scratch directory and reading the file it produced, plus the
# hook schema in the CLI's own bundled docs (docs/hooks/writing-hooks.md,
# docs/reference/configuration.md). Notable: MCP servers and hooks share
# `.gemini/settings.json`, the SessionStart/SessionEnd event names are the same
# as Claude Code's, `matcher: "*"` is the documented wildcard, hook entries
# additionally carry `name`, and GEMINI_PROJECT_DIR is the analogue of
# CLAUDE_PROJECT_DIR.
GEMINI_CLI = RuntimeBinding(
    id="gemini-cli",
    label="Gemini CLI",
    mcp_file=(".gemini", "settings.json"),
    mcp_key="mcpServers",
    # No approval-list concept found in gemini-cli 0.35.2's bundled docs. Absent
    # rather than guessed, per this table's rule.
    mcp_enable_file=None,
    mcp_enable_key=None,
    hooks_file=(".gemini", "settings.json"),
    hooks_key="hooks",
    # Unmoved, and deliberately not guessed. Gemini CLI may well have a local
    # settings analogue, but this table's rule is that a field is verified against
    # an installed runtime before it ships — and the Claude Code move was made on
    # a documented merge/dedup guarantee, not on the filename looking plausible.
    # Assuming the symmetry here is the same error in the other direction.
    legacy_hooks_file=None,
    context_file="GEMINI.md",
    project_dir_env="GEMINI_PROJECT_DIR",
    hook_entries_named=True,
    project_markers=((".gemini",), ("GEMINI.md",)),
    executable="gemini",
    verified="2026-07-30 — gemini-cli 0.35.2, observed + bundled docs",
)

BINDINGS: tuple[RuntimeBinding, ...] = (CLAUDE_CODE, GEMINI_CLI)
BY_ID = {b.id: b for b in BINDINGS}


def select(root: Path, only: tuple[str, ...] | None = None) -> list[RuntimeBinding]:
    """Which runtimes to enroll this project for.

    A runtime qualifies if it is **already used in the project** or **installed
    on the machine**. The second half is the important one and is deliberately
    generous: the Gemini failure happened precisely because the project had no
    ``.gemini/`` yet, so a project-markers-only rule would have skipped the one
    runtime that needed enrolling and reproduced the bug it exists to fix.

    Writing an unused binding costs a small, additive, merged file. Not writing
    a needed one costs an unleased server nobody can see.
    """
    if only:
        return [BY_ID[r] for r in only if r in BY_ID]
    chosen = [b for b in BINDINGS if b.configured_in(root) or b.installed()]
    # Never enroll a project for nothing: Claude Code is the reference runtime
    # and its files are what `rent init` has always written.
    return chosen or [CLAUDE_CODE]


def unknown_ids(only: tuple[str, ...]) -> list[str]:
    """Requested runtime ids that have no binding — surfaced, never ignored."""
    return [r for r in only if r not in BY_ID]


# Why a runtime ended up in the enrollment set. Reported per runtime, because
# "the project uses this" and "this happens to be installed on the machine" are
# very different claims and were previously indistinguishable in the output.
REQUESTED = "requested"    # named explicitly via --runtime
CONFIGURED = "configured"  # the project already carries this runtime's files
DETECTED = "detected"      # inferred from what is installed on this machine
DEFAULTED = "defaulted"    # nothing qualified; fell back to the reference runtime


def selection_reason(binding: RuntimeBinding, root: Path, only: tuple[str, ...] | None) -> str:
    """Why :func:`select` chose ``binding`` — the assumption, stated.

    `declare-what-a-check-assumes`: enrollment reported `hooks_installed` for
    Gemini on a project whose own config names a *different* Gemini runtime, and
    nothing in the output said the choice came from an executable on the machine
    rather than from the project. An unqualified success for a guess reads as a
    verified fact, which is the shape that makes a wrong guess survive.
    """
    if only:
        return REQUESTED
    if binding.configured_in(root):
        return CONFIGURED
    if binding.installed():
        return DETECTED
    return DEFAULTED
