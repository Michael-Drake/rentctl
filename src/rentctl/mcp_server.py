# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Drake

"""The stdio MCP shell — ``env_up``/``env_down``/``env_ls``/``env_sweep`` (spec §3.2).

A thin FastMCP wrapper over :class:`rentctl.core.service.Service`. Claude Code
spawns this per session over stdio, so the instance holds no authoritative state
(§3.1) — every call reconciles the on-disk state against the OS. Each tool
returns the service's JSON envelope verbatim: ``{"ok": true, ...}`` or
``{"ok": false, "error": "<code>", ...}`` (§4, §9).

**Session identity is resolved per call (ADR-0018 §4).** Claude Code puts the
session id in this server's environment (``CLAUDE_CODE_SESSION_ID``); Codex puts
nothing there and sends it with every ``tools/call`` in ``_meta.threadId`` (also
``_meta["x-codex-turn-metadata"].session_id``), equal to the ``session_id`` its
SessionEnd hook receives — the join key that lets the hook release the claims
made here. So each tool reads ``_meta`` first and falls back to the environment.

**Every tool carries MCP annotations** (ADR-0018 §5). ``codex exec`` refuses a
tool with none; honest hints let it call ``env_ls``/``env_up``/``env_down``
without a human and keep ``env_sweep`` — destructive under strict enforcement —
behind an approval.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from . import __version__
from .core import wiring
from .core.events import EXPLICIT
from .core.service import (
    DEFAULT_LEASE_MINUTES,
    DOWN_WAIT_S,
    VIA_MCP,
    Service,
    _valid_session_id,
)

# ADR-0016 R6: an agent that asked for a stop should normally see it complete,
# and the wait is bounded so no turn is held hostage by a stubborn workload.
MCP_DOWN_WAIT_S = DOWN_WAIT_S
MCP_MAX_WAIT_S = 60.0

# The advertised server identity must match the `mcpServers` key enrollment writes
# (ADR-0009, WI-0017) — a server whose handshake name disagrees with the key it is
# registered under is two names for one thing in the surface a user debugs.
mcp = FastMCP(wiring.SERVER_NAME)
# FastMCP takes no version, so the handshake's `serverInfo.version` fell back to the
# mcp SDK's own (1.30.0) — a number that reads as rentctl's and is not. "Which
# rentctl answered?" is an incident question; the handshake should answer it. The
# low-level server is private API, hence the guard: on an SDK without it, the
# handshake just keeps the SDK's number rather than failing to import.
if hasattr(mcp, "_mcp_server"):
    mcp._mcp_server.version = __version__

_service: Service | None = None


def _svc() -> Service:
    """Lazily build the service so importing this module has no side effects."""
    global _service
    if _service is None:
        # The service's own resolver reads the environment (Claude Code, Gemini
        # CLI); a per-call id from `_meta` is passed explicitly (ADR-0018 §4).
        _service = Service(via=VIA_MCP)
    return _service


# Codex's `_meta` keys (verified live on codex-cli 0.153.4, 2026-09-29).
META_THREAD_ID = "threadId"
META_CODEX_TURN = "x-codex-turn-metadata"


def _as_mapping(obj: Any) -> Mapping[str, Any]:
    """A read-only view of ``obj``'s fields, whatever shape the SDK gave it.

    The SDK's ``RequestParams.Meta`` is a pydantic model with ``extra="allow"``,
    so client-specific keys land in ``model_extra``; a plain dict (another SDK
    version, or a nested value) is used as is. Anything else reads as empty.
    """
    if isinstance(obj, Mapping):
        return obj
    extra = getattr(obj, "model_extra", None)
    if isinstance(extra, Mapping):
        return extra
    return {}


def meta_session(ctx: Context | None) -> str | None:
    """The calling session's id from a ``tools/call``'s ``_meta``, or ``None``.

    ``threadId`` first, then ``x-codex-turn-metadata.session_id``. Never raises:
    a call outside a request, with no ``_meta``, or with a malformed one falls
    back to the environment, as it did before Codex existed.
    """
    if ctx is None:
        return None
    try:
        meta = ctx.request_context.meta
    except Exception:  # noqa: BLE001 - no request in flight: no per-call id
        return None
    if meta is None:
        return None
    fields = _as_mapping(meta)
    sid = _valid_session_id(fields.get(META_THREAD_ID))
    if sid is not None:
        return sid
    return _valid_session_id(_as_mapping(fields.get(META_CODEX_TURN)).get("session_id"))


# ADR-0018 §5. Closed-world throughout: every tool acts only on this machine's
# rentctl state. `env_up`/`env_down` are not destructive — they touch only
# rentctl-leased workloads that expiry would stop anyway, and `env_down`
# releases only the caller's claim unless `force`. Both are idempotent: a second
# `up` renews, a second `down` finds nothing to release.
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
LEASE_CHANGE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
# Under strict enforcement a sweep can reclaim a foreign listener's port.
SWEEP = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)


@mcp.tool(annotations=LEASE_CHANGE)
def env_up(
    project: str | None = None,
    lease_minutes: int = DEFAULT_LEASE_MINUTES,
    profile: str = "default",
    ctx: Context | None = None,
) -> dict:
    """Start (or renew) this checkout's leased dev environment and return its URL.

    Omit ``project`` to use the one this checkout declares in its rentctl.toml.
    Use the returned ``url``/``port`` — never a conventional port — and check
    ``readiness``. Calling it again renews your session's lease. Errors come back
    as ``ok: false`` with an ``error`` code: ``NOT_A_PROJECT`` / ``UNKNOWN_PROJECT``
    (not enrolled — a person runs ``rent init``), ``CMD_CHANGED`` (a person must
    re-approve with ``rent sync``), ``START_TIMEOUT`` (read ``log_tail``).
    """
    return _svc().env_up(project, lease_minutes, profile, session=meta_session(ctx))


@mcp.tool(annotations=LEASE_CHANGE)
def env_down(
    project: str | None = None,
    wait_s: float | None = None,
    force: bool = False,
    ctx: Context | None = None,
) -> dict:
    """Release this session's hold on a project's environment; it stops when no other session holds it.

    Other agent sessions in the same checkout may be using the same server. By
    default this releases only YOUR session's claim: if another session still
    holds one, the server keeps running and the result says ``stopped: false``
    with ``held_by`` naming them. If you held the last claim, it stops.
    ``force=true`` stops it for everyone and lists ``overrode_claims`` — use it
    only when the user asked to stop the server outright. Omit ``project`` to
    act on everything leased to this cwd.

    Waits up to ``wait_s`` seconds (default 15, at most 60) for verified
    cleanup. Past that the result is ``pending: true`` — still ``ok``: the
    environment's supervisor finishes the stop, and ``env_ls`` shows the outcome.
    """
    # Always cleanup layer 1, in both shapes: hooks cannot call MCP tools, so an
    # MCP teardown is by construction the polite path and never a SessionEnd kill.
    # Bounded (R6): an agent turn is never held longer than MCP_MAX_WAIT_S.
    # Release by default, `force` to stop for everyone (ADR-0017, ruled 2026-09-28).
    wait = MCP_DOWN_WAIT_S if wait_s is None else min(max(0.0, float(wait_s)), MCP_MAX_WAIT_S)
    return _svc().env_down(
        project, reason=EXPLICIT, wait_s=wait, release=not force, session=meta_session(ctx)
    )


@mcp.tool(annotations=READ_ONLY)
def env_ls() -> dict:
    """List every broker-owned environment on the machine (live, reconciled inventory)."""
    return _svc().env_ls()


@mcp.tool(annotations=SWEEP)
def env_sweep(ctx: Context | None = None) -> dict:
    """Reconcile every lease: stop expired/dead, report (or under strict, reclaim) squatters."""
    return _svc().env_sweep(session=meta_session(ctx))


def main() -> None:  # pragma: no cover - exercised as a subprocess, not in-process
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
