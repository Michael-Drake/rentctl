# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Drake

"""devctl core — all logic, pure where possible.

The MCP server and CLI are thin argument-parsers over this package; tests
target it directly. Nothing here holds authoritative state in memory: state
lives in the OS process table (ground truth), lease files, and the registry.
Every operation reconciles lease files against ground truth before acting
(spec §3.1).
"""
