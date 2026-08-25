# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Drake

"""Per-project file locks (spec §5.3).

One ``flock`` per project, held across any read-modify-write of that project's
lease and around the registry port check during ``up``. Single machine, coarse
locks, no distributed anything. ``LOCK_EX`` is blocking, so two concurrent
``up`` calls for the same project serialize: the second waits, then sees the
lease the first wrote and returns ``already_running`` (F3).
"""

from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def project_lock(lock_path: Path) -> Iterator[None]:
    """Hold an exclusive lock on ``lock_path`` for the duration of the block.

    The lock file is created if absent and never removed (removing it would race
    with another holder). On process death the OS releases the flock (F11).
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()
