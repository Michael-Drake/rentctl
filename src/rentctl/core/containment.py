# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Drake

"""The one directory-containment check (WI-0068).

Two places decide which directory an approved command runs in: enrollment
(``devctl.toml``'s ``cwd`` against the repo being enrolled) and worktree
re-rooting (the enrolled subdirectory carried over to a caller's lane,
ADR-0010). Both need the same answer to the same question — *after following
every symlink, is this still inside that root?* — and they used to ask it
separately. Re-rooting forgot to ask at all, so a lane that replaced
``frontend/`` with a symlink got its server spawned outside the repository.

One function, so the two cannot drift apart again. Callers own the error code
their user sees; this owns the rule.

Residual limit: this is a check-then-use. Between the check and the spawn a
process with write access to the worktree can swap a directory for a symlink
(TOCTOU). Returning the *resolved* path narrows that — the spawn is handed the
directory that was checked, not the link — but a component of that resolved path
can still be replaced afterwards. Closing it fully needs the runner to spawn
relative to an already-open directory fd, which the frozen runner interface
(spec §6) does not offer. Anyone who can do that swap can already edit the code
the approved command runs, so it is not a privilege boundary — the check exists
to stop the *accidental* or configuration-borne escape.
"""

from __future__ import annotations

from pathlib import Path

from .errors import CWD_ESCAPES_ROOT, DevctlError


class CwdEscapesRoot(DevctlError):
    """``path`` resolved to ``target``, which is not inside ``root``."""

    def __init__(self, message: str, *, root: Path, target: Path | None) -> None:
        super().__init__(
            CWD_ESCAPES_ROOT,
            message,
            root=str(root),
            target=None if target is None else str(target),
        )
        self.root = root
        self.target = target


def resolve_within(root: str | Path, path: str | Path) -> Path:
    """Resolve ``path`` against ``root``; return it only if it stays inside.

    Both ends are resolved: a root reached through a symlink (``/tmp`` →
    ``/private/tmp`` on macOS) must not make its own contents look foreign, and
    the candidate is resolved so a symlinked component is judged by where it
    *lands*. The comparison is by path components (``is_relative_to``), never a
    string prefix — ``/x/lane2`` is not inside ``/x/lane``.

    An absolute ``path`` is judged as-is (``root / "/etc"`` is ``/etc``). A path
    that does not exist yet is fine: existence is the caller's question, and
    asking it first would let a dangling escape pass as merely "missing".
    """
    root_r = Path(root).resolve()
    try:
        target = (root_r / path).resolve()
    except (OSError, RuntimeError) as exc:
        # A symlink loop (RuntimeError before 3.13, OSError after). Nothing
        # proves where it lands, so it is not contained.
        raise CwdEscapesRoot(
            f"{str(path)!r} under {root_r} cannot be resolved ({exc})",
            root=root_r,
            target=None,
        ) from None
    if not target.is_relative_to(root_r):
        raise CwdEscapesRoot(
            f"{str(path)!r} resolves to {target}, outside {root_r}",
            root=root_r,
            target=target,
        )
    return target
