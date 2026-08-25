# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Drake

"""``python3 -m rentctl.doctor`` — the shim-independent liveness check.

This module exists as a *separate entry point* on purpose. `rent doctor` is the
convenient spelling, but it is reached through the console shim, and the failure
this detector was built for (WI-0051) is precisely a broken shim. A self-check
that can only be invoked through the thing it checks cannot report that thing's
absence — so the alarm must have a path that does not depend on the alarm.

Anything scheduling this must treat *both* a non-zero exit and a
"command not found" as failure. A scheduler that skips on the latter reproduces
the original nine-day outage exactly.
"""

from __future__ import annotations

import sys

from .core.doctor import main

if __name__ == "__main__":
    sys.exit(main())
