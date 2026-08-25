# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Drake

"""Error codes and the structured error type (spec §4, §9).

Every tool returns ``{"ok": false, "error": "<code>", "message": "<text>"}``
on failure. A :class:`DevctlError` carries that code plus optional structured
detail fields, and knows how to render itself into that envelope.
"""

from __future__ import annotations

from typing import Any

# --- Error codes (spec §9 failure-mode table) -----------------------------
# String constants, not an Enum: they cross the JSON boundary as-is and the
# CLI/MCP shells compare against them without importing an enum member.

PORT_SQUATTED = "PORT_SQUATTED"          # F7  assigned port held by a non-broker process
START_TIMEOUT = "START_TIMEOUT"          # F8  server did not become ready in time
REGISTRY_INVALID = "REGISTRY_INVALID"    # F10 registry missing / bad JSON / schema violation
DOCKER_UNAVAILABLE = "DOCKER_UNAVAILABLE"  # F9  compose runner, Docker not running (deferred runner)
BLOCK_EXHAUSTED = "BLOCK_EXHAUSTED"      # every port in the project's block is taken (ADR-0004)
UNKNOWN_PROJECT = "UNKNOWN_PROJECT"      # project key not in the registry
UNKNOWN_PROFILE = "UNKNOWN_PROFILE"      # profile not in the project's registry entry
PROFILE_MISMATCH = "PROFILE_MISMATCH"    # a DIFFERENT profile is already leased to this cwd
LEASE_INVALID = "LEASE_INVALID"          # lease file present but unparseable
INVALID_CWD = "INVALID_CWD"              # --cwd was passed but empty — an unexpanded shell variable
INTERNAL = "INTERNAL"                    # catch-all for an unexpected failure

# --- enrollment (ADR-0002) and command pinning (ADR-0003) -----------------
# `rent init` reads a devctl.toml authored in a repo devctl does not own, so
# its failures are distinct codes: a user debugging their own project's config
# must not be sent to the machine-level registry, and vice versa.

PROJECT_CONFIG_INVALID = "PROJECT_CONFIG_INVALID"  # devctl.toml missing / bad / schema violation
NAME_TAKEN = "NAME_TAKEN"                # project name already claimed by a different repo
NO_FREE_BLOCK = "NO_FREE_BLOCK"          # no unclaimed port block left in the claim range
NOT_APPROVED = "NOT_APPROVED"            # user declined the command at init/sync
CMD_CHANGED = "CMD_CHANGED"              # devctl.toml no longer matches the approved pin
UNKNOWN_RUNTIME = "UNKNOWN_RUNTIME"      # --runtime named an agent runtime with no binding (ADR-0011)
CANNOT_ADOPT = "CANNOT_ADOPT"            # --adopt found no single usable registry entry (ADR-0012)


class DevctlError(Exception):
    """A failure with a stable ``code`` for the JSON error envelope.

    ``details`` are extra structured fields merged into the envelope (e.g. the
    owning-process info on ``PORT_SQUATTED``, or the captured log tail on
    ``START_TIMEOUT``) so the caller can act without re-deriving them.
    """

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_envelope(self) -> dict[str, Any]:
        """Render into the ``{"ok": false, ...}`` shape all tools return."""
        env: dict[str, Any] = {"ok": False, "error": self.code, "message": self.message}
        env.update(self.details)
        return env

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"DevctlError(code={self.code!r}, message={self.message!r}, details={self.details!r})"
