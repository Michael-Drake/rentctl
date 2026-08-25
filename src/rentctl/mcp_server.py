# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Drake

"""The stdio MCP shell — ``env_up``/``env_down``/``env_ls``/``env_sweep`` (spec §3.2).

A thin FastMCP wrapper over :class:`rentctl.core.service.Service`. Claude Code
spawns this per session over stdio, so the instance holds no authoritative state
(§3.1) — every call reconciles the on-disk state against the OS. Each tool
returns the service's JSON envelope verbatim: ``{"ok": true, ...}`` or
``{"ok": false, "error": "<code>", ...}`` (§4, §9).
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .core import wiring
from .core.events import EXPLICIT
from .core.service import DEFAULT_LEASE_MINUTES, Service

# The advertised server identity must match the `mcpServers` key enrollment writes
# (ADR-0009, WI-0017) — a server whose handshake name disagrees with the key it is
# registered under is two names for one thing in the surface a user debugs.
mcp = FastMCP(wiring.SERVER_NAME)

_service: Service | None = None


def _svc() -> Service:
    """Lazily build the service so importing this module has no side effects."""
    global _service
    if _service is None:
        _service = Service()
    return _service


@mcp.tool()
def env_up(
    project: str, lease_minutes: int = DEFAULT_LEASE_MINUTES, profile: str = "default"
) -> dict:
    """Start (or return the already-running) leased dev environment for a project."""
    return _svc().env_up(project, lease_minutes, profile)


@mcp.tool()
def env_down(project: str | None = None) -> dict:
    """Stop a project's environment; omit ``project`` to down all leased to this cwd."""
    # Always cleanup layer 1, in both shapes: hooks cannot call MCP tools, so an
    # MCP teardown is by construction the polite path and never a SessionEnd kill.
    return _svc().env_down(project, reason=EXPLICIT)


@mcp.tool()
def env_ls() -> dict:
    """List every broker-owned environment on the machine (live, reconciled inventory)."""
    return _svc().env_ls()


@mcp.tool()
def env_sweep() -> dict:
    """Reconcile every lease: stop expired/dead, report (or under strict, reclaim) squatters."""
    return _svc().env_sweep()


def main() -> None:  # pragma: no cover - exercised as a subprocess, not in-process
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
