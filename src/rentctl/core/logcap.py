# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Drake

"""Bounded dev-server logs (approved follow-up to the design spec).

A dev server writes to its log file directly via an append fd held by the
detached child, so rentctl can't cap it at write time without a daemon (which we
deliberately don't have). Instead the watchdog trims the file **in place** on its
tick when it grows past a cap.

In-place matters: we keep the same inode (open ``r+b``, rewrite, ``truncate``)
rather than rename/replace, because the child still holds an append fd to that
inode — replacing it would leave the child writing to an unlinked file that no
one ever reads. The child's ``O_APPEND`` writes always target end-of-file, so at
worst a trim races a write and clips a line or two; that is fine for a log.

Cap is a backstop against unbounded overnight growth, not a realtime limit —
60 s tick granularity is plenty. Trimming to ``keep`` well below ``max`` means we
don't re-trim every tick.
"""

from __future__ import annotations

from pathlib import Path

MAX_LOG_BYTES = 5 * 1024 * 1024   # trim once a log exceeds this
KEEP_BYTES = 1 * 1024 * 1024      # ...down to roughly this much recent tail

_MARKER = b"...[rentctl: earlier log truncated]...\n"


def trim_log_if_large(
    path: Path, max_bytes: int = MAX_LOG_BYTES, keep_bytes: int = KEEP_BYTES
) -> bool:
    """Head-trim ``path`` to its last ~``keep_bytes`` if it exceeds ``max_bytes``.

    Returns ``True`` if a trim happened, ``False`` otherwise (small enough, or an
    I/O error — never raises; a failed trim must not brick the watchdog).
    """
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size <= max_bytes:
        return False
    try:
        with open(path, "r+b") as f:
            f.seek(max(0, size - keep_bytes))
            tail = f.read()
            # Drop the partial first line so the kept text starts clean.
            nl = tail.find(b"\n")
            if nl != -1:
                tail = tail[nl + 1 :]
            f.seek(0)
            f.write(_MARKER + tail)
            f.truncate()
        return True
    except OSError:  # pragma: no cover - defensive; never brick the watchdog
        return False
