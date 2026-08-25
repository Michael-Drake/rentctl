# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Drake

"""On-disk locations for registry, leases, locks, and logs (spec §5).

All paths hang off two roots — a config home (registry) and a state home
(leases, locks, logs) — following the XDG base-dir spec. Both are overridable
so tests can point the whole system at a tmp dir:

* ``RENTCTL_CONFIG_HOME`` / ``DEVCTL_CONFIG_HOME`` / ``XDG_CONFIG_HOME`` → config root
* ``RENTCTL_STATE_HOME``  / ``DEVCTL_STATE_HOME``  / ``XDG_STATE_HOME``  → state root

The ``RENTCTL_*`` overrides win over the legacy ``DEVCTL_*`` ones, which win over
the generic ``XDG_*`` ones, which win over the ``~/.config`` and
``~/.local/state`` defaults.

**The directory under those roots stays ``devctl``** even though the product is
`rentctl` (project owner's call). A working folder is not a user-facing name — nobody
types it and nothing published references it — so renaming it would buy a
cosmetic match at the cost of migrating every live lease, every log, and the
event log carrying the pilot's G4 evidence. The mismatch is deliberate and
recorded; do not "fix" it without a reason better than symmetry.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

_CWD_HASH_LEN = 12

# Not `rentctl`. See the module docstring — this is a decision, not an oversight.
_DIR_NAME = "devctl"


def project_from_key(key: str) -> str:
    """The project half of a lease key — what its per-project lock is named.

    Split on the *last* ``--`` so a project whose own name contains one still
    resolves; the hash suffix never does.
    """
    return key.rsplit("--", 1)[0]


def lease_key(project: str, cwd: str) -> str:
    """The identity of one leased instance: ``<project>--<hash of cwd>`` (ADR-0007).

    A lease is keyed by the *pair* (project, cwd), because N git worktrees of one
    repository run concurrently and each needs its own environment. Keying on
    project alone hands the second worktree the first one's server and misroutes
    both teardowns.

    ``cwd`` is resolved first, so a symlink and its target are one instance
    rather than two. The project name stays unhashed for a legible state
    directory and a per-project glob; the hash exists only to make an arbitrary
    absolute path into a safe, bounded, deterministic filename.
    """
    resolved = os.path.realpath(cwd)
    digest = hashlib.sha256(resolved.encode()).hexdigest()[:_CWD_HASH_LEN]
    return f"{project}--{digest}"


@dataclass(frozen=True)
class DevctlPaths:
    """Resolved filesystem layout. Construct via :meth:`default` in production."""

    config_home: Path
    state_home: Path

    @classmethod
    def default(cls) -> "DevctlPaths":
        """Resolve from environment: ``RENTCTL_*``, then ``DEVCTL_*``, then ``XDG_*``, then HOME.

        ``DEVCTL_*`` stays honored for the same reason the ``devctl`` console
        script does (ADR-0009 §4) — it is baked into shells and scripts that
        predate the rename, and breaking it mid-pilot breaks the cleanup the
        pilot exists to prove.
        """
        config = (
            os.environ.get("RENTCTL_CONFIG_HOME")
            or os.environ.get("DEVCTL_CONFIG_HOME")
            or os.environ.get("XDG_CONFIG_HOME")
            or str(Path.home() / ".config")
        )
        state = (
            os.environ.get("RENTCTL_STATE_HOME")
            or os.environ.get("DEVCTL_STATE_HOME")
            or os.environ.get("XDG_STATE_HOME")
            or str(Path.home() / ".local" / "state")
        )
        return cls(config_home=Path(config), state_home=Path(state))

    # --- config root ------------------------------------------------------
    @property
    def config_dir(self) -> Path:
        return self.config_home / _DIR_NAME

    @property
    def registry_file(self) -> Path:
        return self.config_dir / "registry.json"

    @property
    def registry_lock(self) -> Path:
        """Machine-wide lock held across a block claim (ADR-0002 §4).

        Distinct from the per-project lease locks: a claim is a read-modify-write
        of the *whole* registry, so two ``init`` runs for different projects still
        have to serialize or they can draw the same free block.
        """
        return self.config_dir / "registry.lock"

    # --- state root -------------------------------------------------------
    @property
    def state_dir(self) -> Path:
        return self.state_home / _DIR_NAME

    @property
    def leases_dir(self) -> Path:
        return self.state_dir / "leases"

    @property
    def logs_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def events_file(self) -> Path:
        """The append-only lease event log (``events.py``). Machine-wide, not per-project."""
        return self.state_dir / "events.jsonl"

    def lease_file(self, key: str) -> Path:
        """The lease file for one instance key (see :func:`lease_key`)."""
        return self.leases_dir / f"{key}.json"

    def lease_file_for(self, project: str, cwd: str) -> Path:
        return self.lease_file(lease_key(project, cwd))

    def project_lease_files(self, project: str) -> list[Path]:
        """Every instance's lease for one project — all lanes of it (ADR-0007)."""
        if not self.leases_dir.is_dir():
            return []
        return sorted(self.leases_dir.glob(f"{project}--*.json"))

    def lock_file(self, project: str) -> Path:
        """Still per-*project*, deliberately: it serializes ADR-0004's port draw
        across every instance of the project, so two concurrent ``env_up`` calls
        cannot draw the same port."""
        return self.leases_dir / f"{project}.lock"

    def log_file(self, project: str, stamp: str) -> Path:
        return self.logs_dir / f"{project}-{stamp}.log"

    def ensure_dirs(self) -> None:
        """Create the state directories (idempotent). Config dir is the user's."""
        self.leases_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
