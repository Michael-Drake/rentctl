# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Drake

"""rentctl — leased dev environments for AI coding sessions.

An MCP server + CLI that leases dev environments so a dev server can never
outlive the work that needed it. Every environment is leased: it dies at
session end, at lease expiry, or on request — whichever comes first.

This package is three thin shells (``mcp_server``, ``cli``, ``watchdog``)
over one core (``rentctl.core``). See ``README.md`` for the user-facing
contract, including the security model.
"""

__version__ = "1.0.0"
