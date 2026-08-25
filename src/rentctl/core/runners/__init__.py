# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Drake

"""Runners — the one thing that varies per environment kind (spec §6).

Everything above this interface (leases, ports, tools, watchdog, hooks) is
runner-agnostic and frozen. v1 ships the ``process`` runner; ``compose`` is
design-complete but deferred (spec §6.3) — adding it later is one new class,
no changes above the interface.
"""

from __future__ import annotations

from .base import Runner, get_runner
from .process import ProcessRunner

__all__ = ["Runner", "ProcessRunner", "get_runner"]
