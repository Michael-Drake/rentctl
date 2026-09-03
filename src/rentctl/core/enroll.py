# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Drake

"""`rent init` — self-service enrollment (ADR-0002) with command pinning (ADR-0003).

This is the whole enrollment story in one command: read the project's own
``devctl.toml``, show the operator exactly what will execute, claim a port block
atomically, write rentctl's own registry, and install the wiring. No second party,
no delivery ritual, no external coordinator.

Two disciplines are load-bearing here and are easy to lose in a refactor:

* **Allocation and record are one write.** The block is drawn and persisted
  inside a single lock-held read-modify-write (ADR-0002 §4). An allocator whose
  write-back is a separate step drifts — that is the failure this design exists
  to remove, observed twice in the week before it was written.
* **Approval precedes any mutation.** Declining at the prompt must leave the
  machine exactly as it was, including no registry file and no half-written
  wiring. The order of operations below is the guarantee.

Approval is injected as a callable rather than prompting here, so `core/` stays
free of stdin and the gate is testable.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import (
    CANNOT_ADOPT,
    CMD_CHANGED,
    NAME_TAKEN,
    NO_FREE_BLOCK,
    NOT_APPROVED,
    UNKNOWN_RUNTIME,
    DevctlError,
)
from .locking import project_lock
from .paths import DevctlPaths
from .project_config import (
    BLOCK_SIZE,
    TOML_NAME,
    ProjectConfig,
    ResolvedProfile,
    profile_fingerprint,
    render_toml,
)
from .registry import Registry, RegistryEntry, RegistryProfile
from .runtimes import BY_ID, CLAUDE_CODE, select, selection_reason, unknown_ids
from .wiring import (
    LEGACY_SERVER_NAME,
    SERVER_NAME,
    HookResult,
    install_hooks_detailed,
    install_rule,
    merge_mcp_config,
    plugin_installed,
    remove_hooks,
    rename_approved_mcp_server,
)

# Where self-claims start. Deliberately below the range used by hand-allocated
# legacy entries (5180 and up) so a fresh claim on a machine that already
# carries migrated entries does not have to step over them one at a time —
# though :func:`_draw_block` skips occupied blocks regardless.
CLAIM_BASE = 5100
CLAIM_CEILING = 5990

Approver = Callable[["EnrollPlan"], bool]

ADOPT_HEADER = """\
Written by `rent init --adopt` from this project's existing registry entry.
The command below is what rentctl was already approved to run; adoption moved
the declaration into the repo, where it belongs, without changing it.
Edit freely — `rent sync` re-approves and re-pins after a change."""


def _parse_toml(text: str) -> Any:
    import tomllib

    return tomllib.loads(text)


def _adopt_config(root: Path, paths: DevctlPaths, adopt: bool | str) -> ProjectConfig:
    """Find this project's existing registry entry and derive a config from it."""
    registry = Registry.load_or_empty(paths.registry_file)

    if isinstance(adopt, str):
        entry = registry.projects.get(adopt)
        if entry is None:
            raise DevctlError(
                CANNOT_ADOPT,
                f"no registry entry named {adopt!r} on this machine "
                f"(have: {', '.join(sorted(registry.projects)) or 'none'})",
            )
        return derive_config(root, entry)

    names = adoption_candidates(root, registry)
    if not names:
        raise DevctlError(
            CANNOT_ADOPT,
            f"nothing to adopt: no registry entry on this machine describes {root}. "
            f"Write a {TOML_NAME} and run `rent init` — see the example in the "
            "error from a bare `init`.",
        )
    if len(names) > 1:
        # Refuse rather than guess. Picking one would enroll this repo under a
        # name it may not own, and the registry is the record that decides which
        # project a port block belongs to.
        raise DevctlError(
            CANNOT_ADOPT,
            f"{len(names)} registry entries describe {root}: {', '.join(names)}. "
            "Re-run with `--adopt <name>` to say which one this repo is.",
        )
    return derive_config(root, registry.projects[names[0]])


def adoption_candidates(root: Path, registry: Registry) -> list[str]:
    """Registry entries that plausibly describe the project at ``root``.

    A project with no ``devctl.toml`` cannot tell us its own name, so the entry
    has to be found by *location*. Two things qualify an entry:

    * ``source_dir`` equals this root — it was self-enrolled from here before
      the file was lost; or
    * ``source_dir`` is unset **and** every profile's ``cwd`` lies inside this
      root — the pre-self-service shape, which is exactly what an external
      registry generator wrote for the first enrolled projects.

    An entry whose profiles point outside ``root`` is never a candidate however
    tempting the name looks: adopting it would author a ``devctl.toml`` claiming
    a directory this repo does not own.
    """
    root = root.resolve()
    out = []
    for name, entry in registry.projects.items():
        if entry.source_dir == str(root):
            out.append(name)
            continue
        if entry.source_dir is not None:
            continue
        cwds = [Path(p.cwd) for p in entry.profiles.values()]
        if cwds and all(_within(c, root) for c in cwds):
            out.append(name)
    return sorted(out)


def _within(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root)
    except (OSError, ValueError):
        return False


def derive_config(root: Path, entry: RegistryEntry) -> ProjectConfig:
    """Build a :class:`ProjectConfig` from an existing registry entry.

    The registry stores absolute, machine-specific ``cwd`` values; ``devctl.toml``
    is a tracked file and must carry repo-relative ones. Re-anchoring is
    therefore the substance of adoption, not a formatting detail — and a profile
    that will not re-anchor is refused rather than approximated.

    Nothing is written here. The caller runs the derived config through the
    normal ADR-0003 approval gate first, so declining still leaves the machine
    untouched.
    """
    root = root.resolve()
    raw_profiles: dict[str, Any] = {}
    for pname, p in entry.profiles.items():
        target = Path(p.cwd).resolve()
        if not _within(target, root):
            raise DevctlError(
                CANNOT_ADOPT,
                f"cannot adopt {entry.project!r}: profile {pname!r} runs in {p.cwd}, "
                f"which is outside {root}. The registry entry describes a different "
                "directory than this repo — fix the entry, or write a "
                f"{TOML_NAME} by hand.",
            )
        rel = target.relative_to(root).as_posix() or "."
        raw_profiles[pname] = {
            "cmd": p.cmd,
            "cwd": rel if rel != "" else ".",
            "port_env": p.port_env,
            "preferred_offset": p.preferred_offset,
        }
    return ProjectConfig.from_dict(
        {
            "project": {"name": entry.project, "runner": entry.runner},
            "profiles": raw_profiles,
        }
    )


@dataclass(frozen=True)
class EnrollPlan:
    """Exactly what `init` is about to do — the object shown for approval.

    Carries **resolved** values, not the template: approval is meaningless if the
    operator is shown ``cwd = "."`` rather than the absolute directory the command
    will actually run in (ADR-0003 §1).
    """

    project: str
    runner: str
    block: int
    source_dir: str
    profiles: dict[str, ResolvedProfile]
    already_enrolled: bool
    adopting: bool = False

    def summary_lines(self) -> list[str]:
        """Human-readable rendering for the CLI prompt."""
        verb = "Re-verify" if self.already_enrolled else "Enroll"
        lines = [
            f"{verb} project '{self.project}' (runner: {self.runner})",
            f"  source:     {self.source_dir}",
            f"  port block: {self.block}-{self.block + BLOCK_SIZE - 1}",
        ]
        if self.adopting:
            # Say this before the command, not after: approving here authors a
            # tracked file in the user's repo, which is a different kind of
            # consent from approving a command rentctl will run.
            lines += [
                f"  adopting:   writes a new {TOML_NAME} into {self.source_dir},",
                "              derived from this project's existing registry entry",
            ]
        lines += [
            "",
            "rentctl will run these commands on your behalf:",
        ]
        for name, p in sorted(self.profiles.items()):
            lines += [
                f"  [{name}]",
                f"    command: {p.cmd}",
                f"    in:      {p.cwd}",
                f"    port via ${p.port_env}",
            ]
        return lines


def enroll(
    repo_root: Path,
    paths: DevctlPaths,
    *,
    approve: Approver,
    unattended: bool = False,
    write_hooks: bool = True,
    claude_home: Path | None = None,
    runtimes: tuple[str, ...] | None = None,
    adopt: bool | str = False,
) -> dict[str, Any]:
    """Enroll the project rooted at ``repo_root``. Idempotent.

    Re-running on an already-enrolled project keeps its block and repairs its
    wiring — including wiring that is **present but stale** (ADR-0012), not only
    wiring that is missing — rather than drawing a second claim.

    ``runtimes`` restricts enrollment to named runtime ids (ADR-0011); by
    default every runtime used in the project or installed on the machine is
    wired. An unknown id **raises** rather than being skipped — a typo that
    silently enrolls nothing is the failure this whole change exists to remove.

    ``adopt`` handles a project that is already in rentctl's registry but has no
    ``devctl.toml`` — the pre-self-service shape (ADR-0012). ``True`` finds the
    entry by location; a string names it outright when more than one matches.
    The derived config goes through the same approval gate as a hand-authored
    one, and the file is written only after approval.
    """
    if runtimes:
        bad = unknown_ids(runtimes)
        if bad:
            raise DevctlError(
                UNKNOWN_RUNTIME,
                f"unknown runtime(s): {', '.join(sorted(bad))} — "
                f"known: {', '.join(sorted(BY_ID))}",
            )
    root = Path(repo_root).resolve()

    adopted_text: str | None = None
    if adopt and not ProjectConfig.exists_in(root):
        config = _adopt_config(root, paths, adopt)
        adopted_text = render_toml(config, header=ADOPT_HEADER)
        # Round-trip before anyone approves it: rendering something `load()`
        # would reject means writing a file that breaks the next command, and
        # the failure would land far from this line.
        if ProjectConfig.from_dict(_parse_toml(adopted_text)) != config:
            raise DevctlError(
                CANNOT_ADOPT,
                f"adoption of {config.name!r} produced a {TOML_NAME} that does not "
                "round-trip — refusing to write it. This is a rentctl bug; please "
                "report the registry entry.",
            )
    else:
        config = ProjectConfig.load(root)

    resolved = config.resolve(root)

    with project_lock(paths.registry_lock):
        registry = Registry.load_or_empty(paths.registry_file)
        existing = registry.projects.get(config.name)

        # A name already held by a *different* checkout is a collision; the same
        # checkout re-running is a repair (ADR-0002 §8).
        if existing is not None and existing.source_dir not in (None, str(root)):
            raise DevctlError(
                NAME_TAKEN,
                f"project name {config.name!r} is already claimed on this machine by "
                f"{existing.source_dir} — rename the project in {TOML_NAME} "
                "(a qualified name like 'org-name' is the usual fix)",
            )

        already = existing is not None
        block = existing.block if already else _draw_block(registry)

        plan = EnrollPlan(
            project=config.name,
            runner=config.runner,
            block=block,
            source_dir=str(root),
            profiles=resolved,
            already_enrolled=already,
            adopting=adopted_text is not None,
        )

        # Nothing has been written yet — this is the last point at which
        # declining leaves the machine untouched.
        if not unattended and not approve(plan):
            raise DevctlError(
                NOT_APPROVED,
                f"enrollment of {config.name!r} declined — nothing was written",
            )

        # Approved — now the adopted file may land. Writing it before this point
        # would leave a declined `--adopt` having authored a file in the repo,
        # breaking the "declining leaves the machine untouched" property that
        # makes the approval gate worth having.
        if adopted_text is not None:
            ProjectConfig.new_path_in(root).write_text(adopted_text)

        entry = _build_entry(config, plan, unattended=unattended)
        registry.projects[config.name] = entry
        registry.write(paths.registry_file)

    # Wiring is outside the lock: it touches the project's own files, not the
    # shared registry, and holding a machine-wide lock across it would serialize
    # unrelated enrollments for no benefit.
    plugin = plugin_installed(claude_home, root)
    bindings = select(root, runtimes)
    # Computed BEFORE the loop, which is load-bearing: `install_rule` writes the
    # very files `configured_in()` looks for (GEMINI.md, .gemini/), so asking
    # inside the loop reports "the project uses this runtime" about a marker
    # enrollment just created a line earlier. A check that answers questions
    # about its own side effects is worse than no check — it manufactures the
    # evidence that makes a guess look verified.
    reasons = {b.id: selection_reason(b, root, runtimes) for b in bindings}
    wired: dict[str, dict[str, Any]] = {}
    for binding in bindings:
        # The policy rule goes in FIRST and unconditionally (ADR-0011 §3). It is
        # the only part of enrollment that still works when the agent cannot see
        # rentctl's tools — which is the case that produced an unleased server.
        # The plugin supplies hooks, never the rule.
        rule_written = install_rule(binding.context_path(root))
        mcp_written = merge_mcp_config(binding.mcp_path(root), binding)
        # The other half of the ADR-0009 server rename (WI-0039). The line above
        # renames the key inside the MCP file; this carries an existing approval
        # of that key across, in the separate file that names it. Renames only —
        # never grants an approval the user did not give.
        enable_path = binding.mcp_enable_path(root)
        approval_renamed = (
            rename_approved_mcp_server(enable_path, binding)
            if enable_path is not None
            else False
        )
        legacy_cleaned: tuple[str, ...] = ()
        if write_hooks and not (plugin and binding is CLAUDE_CODE):
            hooks = install_hooks_detailed(binding.hooks_path(root), binding)
            # Sweep the pre-ADR-0014 location. Runs on EVERY enrollment, not just
            # a detected-migration path: hook entries merge across settings
            # levels, so a copy left in the old file keeps firing beside the new
            # one. Cheap and idempotent — a project that never had hooks there
            # gets `()` and no write.
            legacy = binding.legacy_hooks_path(root)
            if legacy is not None:
                legacy_cleaned = remove_hooks(legacy, binding)
        else:
            hooks = HookResult()
        wired[binding.id] = {
            # WHY this runtime is in the set. `detected` means rentctl inferred it
            # from an executable on this machine, not from anything the project
            # says — a guess, and the output must not read like a verified fact.
            # Observed: a project declared `gemini-antigravity`; rentctl enrolled
            # `gemini-cli` off the `gemini` binary and reported plain success.
            "selected_by": reasons[binding.id],
            "rule_written": rule_written,
            "mcp_written": mcp_written,
            "hooks_written": hooks.changed,
            # Reported separately because they mean different things to a user:
            # one added wiring that was absent, the other overwrote a line
            # already in their settings file (ADR-0012 §3).
            "hooks_installed": list(hooks.installed),
            "hooks_repaired": list(hooks.repaired),
        }
        # Only present when the write is expected to be reverted. Absent on the
        # normal path so it cannot become noise a reader learns to skip.
        if hooks.provisional:
            wired[binding.id]["hooks_provisional"] = hooks.provisional
        # Likewise absent unless something was actually removed. This one is
        # reported because it is a change to a file the project can see and may
        # have committed — a silent deletion from someone's settings is exactly
        # the move rentctl refuses to make elsewhere.
        if legacy_cleaned:
            wired[binding.id]["hooks_legacy_removed"] = {
                "file": str(binding.legacy_hooks_path(root)),
                "events": list(legacy_cleaned),
            }
        # Also absent on the normal path. Present only when an approval actually
        # carried across the rename, because that is a change to a file recording
        # the user's own decisions and should never be inferred from silence.
        if approval_renamed:
            wired[binding.id]["mcp_approval_renamed"] = {
                "file": str(enable_path),
                "from": LEGACY_SERVER_NAME,
                "to": SERVER_NAME,
            }

    return {
        "ok": True,
        "project": config.name,
        "block": block,
        "ports": f"{block}-{block + BLOCK_SIZE - 1}",
        "profiles": sorted(resolved),
        "source_dir": str(root),
        "already_enrolled": already,
        "adopted": adopted_text is not None,
        "plugin": plugin,
        "runtimes": wired,
        # Kept for consumers that predate multi-runtime enrollment; they read
        # the reference runtime's result, which is what they meant all along.
        "mcp_written": wired.get(CLAUDE_CODE.id, {}).get("mcp_written", False),
        "hooks_written": wired.get(CLAUDE_CODE.id, {}).get("hooks_written", False),
        "approved_unattended": unattended,
    }


def sync(repo_root: Path, paths: DevctlPaths, *, approve: Approver) -> dict[str, Any]:
    """Re-approve a changed ``devctl.toml`` and re-pin it (ADR-0003 §4).

    The only path that updates a pinned command. Declining leaves the previous
    pin — and therefore the previously approved command — in force.
    """
    root = Path(repo_root).resolve()
    config = ProjectConfig.load(root)
    resolved = config.resolve(root)

    with project_lock(paths.registry_lock):
        registry = Registry.load_or_empty(paths.registry_file)
        existing = registry.projects.get(config.name)
        if existing is None:
            raise DevctlError(
                NAME_TAKEN,
                f"project {config.name!r} is not enrolled on this machine — run `rent init`",
            )

        plan = EnrollPlan(
            project=config.name,
            runner=config.runner,
            block=existing.block,
            source_dir=str(root),
            profiles=resolved,
            already_enrolled=True,
        )
        if not approve(plan):
            raise DevctlError(
                NOT_APPROVED,
                f"re-approval of {config.name!r} declined — the previous pin stands",
            )

        registry.projects[config.name] = _build_entry(
            config, plan, unattended=existing.approved_unattended
        )
        registry.write(paths.registry_file)

    return {"ok": True, "project": config.name, "block": existing.block, "resynced": True}


def check_pin(entry: RegistryEntry) -> None:
    """Raise ``CMD_CHANGED`` if the project's ``devctl.toml`` no longer matches.

    Two cases deliberately pass rather than fail, both from ADR-0003:

    * **No pin on the entry** — it predates pinning. Unpinned is not mismatched.
    * **No readable ``devctl.toml``** — the registry copy is the authority, and a
      repo that moved, was archived, or is simply not checked out on this machine
      must not read as a changed command. Treating absence as a mismatch would
      break every enrollment the first time a repo moved.

    Only a toml that is present, parseable, and *different* stops the world.
    """
    if entry.source_dir is None:
        return
    root = Path(entry.source_dir)
    if not ProjectConfig.path_in(root).is_file():
        return
    try:
        config = ProjectConfig.load(root)
        resolved = config.resolve(root)
    except DevctlError:
        # An unparseable toml is the project's problem to fix, not grounds to
        # refuse a command that was approved when it was readable.
        return

    for pname, pinned in entry.profiles.items():
        if pinned.cmd_sha256 is None:
            continue
        current = resolved.get(pname)
        if current is None:
            raise DevctlError(
                CMD_CHANGED,
                f"profile {pname!r} was approved for {entry.project!r} but is no longer in "
                f"{TOML_NAME} — run `rent sync` to re-approve",
                project=entry.project,
                profile=pname,
                approved={"cmd": pinned.cmd, "cwd": pinned.cwd, "port_env": pinned.port_env},
                current=None,
            )
        digest = profile_fingerprint(entry.runner, current.cmd, current.cwd, current.port_env)
        if digest != pinned.cmd_sha256:
            raise DevctlError(
                CMD_CHANGED,
                f"{TOML_NAME} for {entry.project!r} no longer matches the approved command "
                f"(profile {pname!r}) — review the change, then `rent sync` to re-approve",
                project=entry.project,
                profile=pname,
                approved={"cmd": pinned.cmd, "cwd": pinned.cwd, "port_env": pinned.port_env},
                current={"cmd": current.cmd, "cwd": current.cwd, "port_env": current.port_env},
            )


# --- internals ------------------------------------------------------------

def _build_entry(
    config: ProjectConfig, plan: EnrollPlan, *, unattended: bool
) -> RegistryEntry:
    profiles = {
        name: RegistryProfile(
            cmd=p.cmd,
            cwd=p.cwd,
            port_env=p.port_env,
            preferred_offset=p.preferred_offset,
            cmd_sha256=profile_fingerprint(config.runner, p.cmd, p.cwd, p.port_env),
        )
        for name, p in plan.profiles.items()
    }
    return RegistryEntry(
        project=config.name,
        block=plan.block,
        runner=config.runner,
        profiles=profiles,
        source_dir=plan.source_dir,
        approved_unattended=unattended,
    )


def _draw_block(registry: Registry) -> int:
    """Lowest free block in the claim range — drawn, never picked (ADR-0002 §4).

    Called under the registry lock, and the result is persisted by the same
    lock-held write, so two concurrent ``init`` runs cannot draw the same block.
    """
    # Overlap, not equality: a migrated entry need not be aligned to BLOCK_SIZE,
    # and an unaligned neighbour (say 5185) would collide with an aligned draw at
    # 5180 while comparing unequal.
    taken = [(e.block, e.block + BLOCK_SIZE - 1) for e in registry.projects.values()]
    for block in range(CLAIM_BASE, CLAIM_CEILING + 1, BLOCK_SIZE):
        hi = block + BLOCK_SIZE - 1
        if not any(lo <= hi and block <= other_hi for lo, other_hi in taken):
            return block
    raise DevctlError(
        NO_FREE_BLOCK,
        f"no free port block between {CLAIM_BASE} and {CLAIM_CEILING} — "
        f"{len(taken)} project(s) enrolled on this machine",
    )
