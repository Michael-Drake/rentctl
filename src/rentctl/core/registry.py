# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Drake

"""The registry: static per-project config — runner, command, port block (spec §5.1).

Read-only from rentctl's perspective (authored by doctrine, synced per O3). All
loading fails **closed**: any problem — missing file, bad JSON, schema
violation — raises ``DevctlError(REGISTRY_INVALID)`` so no tool starts or kills
anything on a bad registry (F10).

Port model (spec §5.1 **as amended by ADR-0004**): each project owns a 10-port
block, and that block is still the unit of allocation and of squatter ownership.
The *specific* port is no longer assigned here — it is drawn per lease at
``env_up`` from the free ports in the block, because N worktrees of one project
run concurrently and a static offset gives them all the same port.

What survives is ``preferred_offset``: a profile may say where it would *like*
to sit, and ``env_up`` honours it when that port is free. So a project's primary
checkout keeps its familiar port in the ordinary case, while a second worktree
quietly gets the next one instead of failing. It is a preference in the strict
sense — never guaranteed, never load-bearing, and nothing may be written down
that assumes it held.

Two constraints therefore relax: preferred offsets need not be unique within a
project (the draw resolves the tie), and ``default`` need not be 0. Entries
authored against the old schema keep working — ``offset`` is read as
``preferred_offset``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import REGISTRY_INVALID, UNKNOWN_PROFILE, UNKNOWN_PROJECT, DevctlError

BLOCK_SIZE = 10
MIN_BLOCK = 1024          # stay out of the privileged range
MAX_BLOCK = 65535 - (BLOCK_SIZE - 1)  # block + 9 must stay a valid port
VALID_RUNNERS = ("process", "compose")
VALID_ENFORCEMENT = ("advisory", "strict")
DEFAULT_PROFILE = "default"


@dataclass(frozen=True)
class RegistryProfile:
    """One runnable variant of a project (spec §5.1 ``profiles`` entry).

    ``cmd_sha256`` is the approved-command pin (ADR-0003 §2) — ``None`` on entries
    written before pinning existed, which is not a mismatch, just unpinned.
    """

    cmd: str
    cwd: str
    port_env: str
    preferred_offset: int  # 0..9; where this profile would like to sit, if free
    cmd_sha256: str | None = None


@dataclass(frozen=True)
class RegistryEntry:
    """A project's registry record: its block, runner, and profiles.

    ``source_dir`` is the repo root ``rent init`` was run from — where this
    project's ``devctl.toml`` lives, so the pin can be re-checked (ADR-0003 §4).
    ``None`` on entries that predate self-service enrollment.
    """

    project: str
    block: int
    runner: str
    profiles: dict[str, RegistryProfile]
    source_dir: str | None = None
    approved_unattended: bool = False

    def profile(self, name: str = DEFAULT_PROFILE) -> RegistryProfile:
        try:
            return self.profiles[name]
        except KeyError:
            raise DevctlError(
                UNKNOWN_PROFILE,
                f"project {self.project!r} has no profile {name!r} "
                f"(have: {', '.join(sorted(self.profiles))})",
            ) from None

    def preferred_port(self, profile: str = DEFAULT_PROFILE) -> int:
        """Where this profile would like to sit. Only a starting point for the draw."""
        return self.block + self.profile(profile).preferred_offset

    def block_ports(self) -> range:
        """Every port this project owns — the draw pool, and the squatter range.

        Ownership is by *range* now, not by enumerating declared offsets
        (ADR-0004 §6). A listener on ``block + 7`` of a single-profile project
        used to be invisible; it is now attributable.
        """
        return range(self.block, self.block + BLOCK_SIZE)


@dataclass(frozen=True)
class Registry:
    """The whole registry: a name → :class:`RegistryEntry` map.

    ``enforcement`` (O4) is advisory during the pilot — squatters are reported,
    not killed — and flips to ``strict`` only after the pilot-exit gate.
    """

    projects: dict[str, RegistryEntry]
    enforcement: str = "advisory"

    @classmethod
    def load_or_empty(cls, path: Path) -> "Registry":
        """Load, or return an empty registry when the file does not exist yet.

        **Only `rent init` may use this.** Every runtime path stays fail-closed
        (F10): a missing registry means "nothing is enrolled," which must stop
        `up`/`down`/`sweep` rather than let them proceed against no config. But a
        stranger's first `init` runs on a machine where the file cannot exist yet,
        so enrollment needs the one door that treats absent as empty.

        A file that exists but is corrupt still raises — that is a broken machine,
        not a fresh one, and enrolling on top of it would destroy the claims it
        holds (ADR-0002 §3: the registry is now non-derivable state).
        """
        if not path.exists():
            return cls(projects={}, enforcement="advisory")
        return cls.load(path)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for :meth:`write`. Round-trips through :meth:`from_dict`."""
        projects: dict[str, Any] = {}
        for name, entry in self.projects.items():
            profiles: dict[str, Any] = {}
            for pname, p in entry.profiles.items():
                raw: dict[str, Any] = {
                    "cmd": p.cmd,
                    "cwd": p.cwd,
                    "port_env": p.port_env,
                    "preferred_offset": p.preferred_offset,
                }
                if p.cmd_sha256 is not None:
                    raw["cmd_sha256"] = p.cmd_sha256
                profiles[pname] = raw
            raw_entry: dict[str, Any] = {
                "block": entry.block,
                "runner": entry.runner,
                "profiles": profiles,
            }
            if entry.source_dir is not None:
                raw_entry["source_dir"] = entry.source_dir
                raw_entry["approved_unattended"] = entry.approved_unattended
            projects[name] = raw_entry
        return {
            "//": (
                "rentctl's own record, written only by rentctl commands (ADR-0002 §3). "
                "Do not hand-edit: this is authoritative state, not a regenerable view — "
                "a lost claim cannot be re-derived."
            ),
            "enforcement": self.enforcement,
            "projects": projects,
        }

    def write(self, path: Path) -> None:
        """Atomically write the registry (temp + ``os.replace``), as leases do.

        Allocation and record are one operation (ADR-0002 §4); a torn write here
        is a lost or duplicated port block, so it must not be possible.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        # `ensure_ascii=False` and an explicit encoding: this file carries a
        # `//` note telling a human NOT to hand-edit it, and the default was
        # rendering that warning's own punctuation as `\uXXXX` escapes on disk.
        # A do-not-touch notice is worth nothing if it is unreadable.
        tmp.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path) -> "Registry":
        """Load + validate. Any failure → ``DevctlError(REGISTRY_INVALID)`` (F10)."""
        if not path.exists():
            raise DevctlError(REGISTRY_INVALID, f"registry not found at {path}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            raise DevctlError(REGISTRY_INVALID, f"cannot read registry {path}: {e}") from e
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: Any) -> "Registry":
        if not isinstance(raw, dict):
            raise DevctlError(REGISTRY_INVALID, "registry root must be a JSON object")
        projects_raw = raw.get("projects")
        if not isinstance(projects_raw, dict) or not projects_raw:
            raise DevctlError(REGISTRY_INVALID, "registry must have a non-empty 'projects' object")

        enforcement = raw.get("enforcement", "advisory")
        if enforcement not in VALID_ENFORCEMENT:
            raise DevctlError(
                REGISTRY_INVALID, f"enforcement {enforcement!r} not in {VALID_ENFORCEMENT}"
            )

        projects: dict[str, RegistryEntry] = {}
        seen_blocks: dict[int, str] = {}
        for name, entry_raw in projects_raw.items():
            entry = _parse_entry(name, entry_raw)
            # Cross-project block collision is a registry authoring error. Checked
            # over the whole block now, not just declared offsets: with per-lease
            # draws every port in a block is live, so overlapping blocks would
            # collide in use even when no two profiles declared the same port.
            for port in entry.block_ports():
                block_owner = seen_blocks.get(port)
                if block_owner is not None and block_owner != name:
                    raise DevctlError(
                        REGISTRY_INVALID,
                        f"port {port} claimed by both {block_owner!r} and {name!r}",
                    )
            for port in entry.block_ports():
                seen_blocks[port] = name
            projects[name] = entry
        return cls(projects=projects, enforcement=enforcement)

    def entry(self, project: str) -> RegistryEntry:
        try:
            return self.projects[project]
        except KeyError:
            raise DevctlError(
                UNKNOWN_PROJECT,
                f"no project {project!r} in registry (have: {', '.join(sorted(self.projects))})",
            ) from None

    def port_owner_map(self) -> dict[int, str]:
        """{port: project} over every claimed block — squatter lookup by range.

        A port maps to the project whose block contains it, whether or not any
        profile ever declared that offset (ADR-0004 §6).
        """
        out: dict[int, str] = {}
        for name, entry in self.projects.items():
            for port in entry.block_ports():
                out[port] = name
        return out


def _parse_entry(name: str, raw: Any) -> RegistryEntry:
    if not isinstance(raw, dict):
        raise DevctlError(REGISTRY_INVALID, f"project {name!r} must be an object")

    block = raw.get("block")
    if not isinstance(block, int) or isinstance(block, bool):
        raise DevctlError(REGISTRY_INVALID, f"project {name!r} 'block' must be an integer")
    if not (MIN_BLOCK <= block <= MAX_BLOCK):
        raise DevctlError(
            REGISTRY_INVALID,
            f"project {name!r} block {block} out of range {MIN_BLOCK}..{MAX_BLOCK}",
        )

    runner = raw.get("runner")
    if runner not in VALID_RUNNERS:
        raise DevctlError(
            REGISTRY_INVALID,
            f"project {name!r} runner {runner!r} not in {VALID_RUNNERS}",
        )

    profiles_raw = raw.get("profiles")
    if not isinstance(profiles_raw, dict) or not profiles_raw:
        raise DevctlError(REGISTRY_INVALID, f"project {name!r} needs a non-empty 'profiles' object")
    if DEFAULT_PROFILE not in profiles_raw:
        raise DevctlError(REGISTRY_INVALID, f"project {name!r} must define a {DEFAULT_PROFILE!r} profile")

    # Preferred offsets need not be unique, and `default` need not be 0: both were
    # assignment constraints, and offsets are preferences now (ADR-0004 §4). Two
    # profiles wanting the same slot is resolved by the draw, not rejected here.
    profiles = {
        pname: _parse_profile(name, pname, praw) for pname, praw in profiles_raw.items()
    }

    source_dir = raw.get("source_dir")
    if source_dir is not None and not isinstance(source_dir, str):
        raise DevctlError(REGISTRY_INVALID, f"project {name!r} 'source_dir' must be a string")
    approved_unattended = raw.get("approved_unattended", False)
    if not isinstance(approved_unattended, bool):
        raise DevctlError(
            REGISTRY_INVALID, f"project {name!r} 'approved_unattended' must be a boolean"
        )

    return RegistryEntry(
        project=name,
        block=block,
        runner=runner,
        profiles=profiles,
        source_dir=source_dir,
        approved_unattended=approved_unattended,
    )


def _parse_profile(project: str, pname: str, raw: Any) -> RegistryProfile:
    if not isinstance(raw, dict):
        raise DevctlError(REGISTRY_INVALID, f"{project!r}/{pname!r} profile must be an object")
    for field in ("cmd", "cwd", "port_env"):
        if not isinstance(raw.get(field), str) or not raw[field]:
            raise DevctlError(
                REGISTRY_INVALID, f"{project!r}/{pname!r} profile needs a non-empty string {field!r}"
            )
    # `offset` is the pre-ADR-0004 spelling; entries authored against the old
    # schema keep working, so already-enrolled projects need no edit.
    offset = raw.get("preferred_offset", raw.get("offset", 0))
    if not isinstance(offset, int) or isinstance(offset, bool) or not (0 <= offset < BLOCK_SIZE):
        raise DevctlError(
            REGISTRY_INVALID,
            f"{project!r}/{pname!r} profile 'preferred_offset' must be an integer "
            f"in 0..{BLOCK_SIZE - 1}",
        )
    pin = raw.get("cmd_sha256")
    if pin is not None and (not isinstance(pin, str) or len(pin) != 64):
        raise DevctlError(
            REGISTRY_INVALID, f"{project!r}/{pname!r} 'cmd_sha256' must be a 64-char hex digest"
        )

    return RegistryProfile(
        cmd=raw["cmd"],
        cwd=raw["cwd"],
        port_env=raw["port_env"],
        preferred_offset=offset,
        cmd_sha256=pin,
    )
