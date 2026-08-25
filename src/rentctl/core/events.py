# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Drake

"""The lease event log — an append-only record of what rentctl started and killed.

Lease files are *live claims*: they exist while an environment does and vanish
when it dies. That makes them useless as history. A session-end kill (cleanup
layer 2) and a watchdog expiry kill (layer 3) leave byte-identical evidence
behind — an absent lease file — so nothing rentctl wrote could say **which layer
fired**, and the pilot's G4 criterion (spec §11.1) was unprovable from the
system's own output. This module is that missing record.

Every teardown carries three facts the lease could not:

* ``reason``        — why it ended (see the reason table below).
* ``layer``         — which of the four cleanup layers (spec §8) fired.
* ``reason_source`` — ``declared`` when the caller stated the reason,
  ``inferred`` when rentctl guessed it from the call shape. A human typing
  ``rent down --all`` looks exactly like the SessionEnd hook, so the guess is
  recorded *as a guess* rather than folded in with the real thing
  (``declare-what-a-check-assumes``). Gate scoring counts declared evidence.

Also recorded is ``killed`` — whether rentctl actually signalled a process, as
opposed to merely removing the lease of one that was already gone. "rentctl tore
down 9 environments" and "rentctl killed 9 processes" are different claims, and
only one of them is about blast radius.

**Design constraints**

* *Append-only, no daemon.* Concurrent watchdogs, CLI hooks, and MCP instances
  all write this file with no lock. Each record is one ``O_APPEND`` write of a
  single short line, which POSIX makes atomic — so interleaved writers cannot
  tear each other's records.
* *Fails open, always.* A logging failure must never brick a teardown. Every
  write is best-effort and returns a bool; nothing here raises.
* *Bounded by code, not by prose.* The file rotates at a size cap, keeping one
  previous generation (``retention-enforced-by-code``). An append-only log with
  a documented-but-unenforced retention policy is a chore that silently lapses.

**Not recorded:** a ``down`` that found no lease. The SessionEnd hook fires in
every enrolled session, including the many that never leased anything; logging
those would bury the real events under no-ops. Every line in this file
corresponds to a real environment.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# --- event kinds ----------------------------------------------------------

UP = "up"
UP_FAILED = "up_failed"
DOWN = "down"

# A human saying "you killed something I was using" (spec §11.1 G3, WI-0016).
#
# G3 was the one gate criterion with no instrument. Every other criterion is
# scored from something rentctl observed; this one asks whether a kill was
# *unwanted*, which rentctl cannot observe by construction — the teardown looked
# identical from the inside either way. Only the person who lost the process
# knows, so the channel has to be an inbound one.
#
# It lands in this same log rather than anywhere new: the log is already the
# gate's evidence base, already append-only, already rotated, and already read
# by `events`. A separate file would be a second thing to remember to look at.
FALSE_KILL = "false_kill"

# --- teardown reasons, and the cleanup layer each one evidences (spec §8) ---

EXPLICIT = "explicit"            # layer 1 — the LLM/human called down on a named project
SESSION_END = "session-end"      # layer 2 — SessionEnd hook: down --all --cwd
EXPIRY = "expiry"                # layer 3 — watchdog stopped a lease past its deadline
PROCESS_GONE = "process-gone"    # layer 3 — watchdog found the process already dead
SWEEP_EXPIRED = "sweep-expired"  # layer 4 — reconcile stopped an expired lease
SWEEP_DEAD = "sweep-dead"        # layer 4 — reconcile dropped a lease whose process was gone

LAYER_BY_REASON: dict[str, int] = {
    EXPLICIT: 1,
    SESSION_END: 2,
    EXPIRY: 3,
    PROCESS_GONE: 3,
    SWEEP_EXPIRED: 4,
    SWEEP_DEAD: 4,
}

DECLARED = "declared"
INFERRED = "inferred"

# Recorded in an event's ``session`` field when no runtime session variable was
# set in the calling process's environment. It is a statement about what rentctl
# could read, never a claim that the runtime has no session identity — rentctl
# spent 33 events asserting the latter while reading the wrong variable name
# (WI-0036), and the honest reading of this value is "not attributed", not
# "unattributable".
UNATTRIBUTED = "unknown"

# --- retention ------------------------------------------------------------

MAX_EVENT_BYTES = 2 * 1024 * 1024  # rotate past this; one previous generation kept
_ROTATED_SUFFIX = ".1"


def _now_local() -> datetime:
    """Timezone-aware local time (carries the offset into every stamp)."""
    return datetime.now().astimezone()


class EventLog:
    """Append-only JSONL event log. Every write is best-effort and never raises."""

    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = MAX_EVENT_BYTES,
        now_fn=_now_local,
    ) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self._now = now_fn

    # --- writing ----------------------------------------------------------

    def record(self, event: str, project: str, **fields: Any) -> bool:
        """Append one event. Returns whether it landed; never raises (fail open)."""
        record = {
            "ts": self._now().isoformat(),
            "event": event,
            "project": project,
            **{k: v for k, v in fields.items() if v is not None},
        }
        try:
            # `ensure_ascii=False` matters most here of anywhere: `record_false_kill`
            # stores a user's own words verbatim, and that note is G3's only input.
            # A complaint filed as `\uXXXX` is degraded evidence in the one place
            # the pilot cannot reconstruct the original from anything else.
            line = (json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return False
        return self._append(line)

    def record_up(
        self,
        project: str,
        *,
        profile: str,
        port: int,
        pid: int,
        session: str,
        cwd: str,
        lease_expires: str,
        already_running: bool,
        spawn_cwd: str | None = None,
    ) -> bool:
        """``cwd`` is the caller's directory; ``spawn_cwd`` is where the process
        actually ran, recorded only when it differs (ADR-0010 re-rooting). The
        two are separate fields because the caller is what teardown matches on
        and the spawn directory is what was served — conflating them would make
        a lane's evidence unreadable after the fact."""
        return self.record(
            UP,
            project,
            op=UP,
            profile=profile,
            port=port,
            pid=pid,
            session=session,
            cwd=cwd,
            spawn_cwd=spawn_cwd,
            lease_expires=lease_expires,
            already_running=already_running,
        )

    def record_up_failed(
        self, project: str, *, profile: str, error: str, port: int | None = None
    ) -> bool:
        return self.record(UP_FAILED, project, op=UP, profile=profile, error=error, port=port)

    def record_false_kill(
        self,
        project: str,
        *,
        note: str,
        port: int | None = None,
        matched: dict[str, Any] | None = None,
    ) -> bool:
        """Record a human's report that a teardown killed something wanted.

        ``matched`` is the teardown event this report was tied to, when one could
        be found. Carrying it makes the complaint self-contained: whoever scores
        G3 later sees which kill is being disputed without re-deriving it from
        timestamps, and a report that matched *nothing* stays visibly unmatched
        rather than quietly looking like the others.
        """
        return self.record(
            FALSE_KILL,
            project,
            port=port,
            note=note,
            matched=matched,
            unmatched=None if matched else True,
        )

    def record_down(
        self,
        project: str,
        *,
        op: str,
        reason: str,
        reason_source: str,
        killed: bool,
        port: int | None = None,
        pid: int | None = None,
        stop_failed: bool | None = None,
    ) -> bool:
        """Record a teardown. ``layer`` is derived from ``reason`` — never passed in,
        so the reason table above stays the single source of the mapping.

        ``stop_failed`` marks the case that used to be invisible: rentctl signalled
        the process and it is **still alive**. Recorded only when true (``None``
        is dropped by :meth:`record`), so its presence in the log always means
        something went wrong rather than being a field to skim past. Without it,
        a teardown that failed and a teardown that worked wrote identical rows —
        and `killed` was the *intent*, not the outcome.
        """
        return self.record(
            DOWN,
            project,
            op=op,
            reason=reason,
            layer=LAYER_BY_REASON.get(reason),
            reason_source=reason_source,
            killed=killed,
            stop_failed=stop_failed,
            port=port,
            pid=pid,
        )

    def _append(self, line: bytes) -> bool:
        """One atomic ``O_APPEND`` write, rotating first if the file is at its cap."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate_if_large()
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
            return True
        except OSError:
            return False  # fail open — a lost event must never block a teardown

    def _rotate_if_large(self) -> None:
        """Move the current file aside once it passes the cap, keeping one generation.

        Two processes rotating in the same instant can cost one generation of
        history (both ``os.replace`` onto the same target). That is an acceptable
        trade against holding a machine-wide lock on the hot path of every kill.
        """
        try:
            if self.path.stat().st_size < self.max_bytes:
                return
        except OSError:
            return  # no file yet, or unreadable — nothing to rotate
        try:
            os.replace(self.path, self.path.with_name(self.path.name + _ROTATED_SUFFIX))
        except OSError:  # pragma: no cover - defensive
            pass

    # --- reading ----------------------------------------------------------

    def read(
        self,
        *,
        project: str | None = None,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Events oldest-first, optionally filtered. ``limit`` keeps the *newest* N.

        Unparseable lines are skipped rather than fatal: this file is written
        without a lock by processes that can be SIGKILLed mid-write, so a torn
        tail line is a foreseeable state, not a corruption to fail on.
        """
        events = [
            e
            for e in self._read_all()
            if (project is None or e.get("project") == project)
            and (since is None or _at_or_after(e, since))
        ]
        if limit is not None and limit >= 0:
            events = events[-limit:] if limit else []
        return events

    def _read_all(self) -> Iterator[dict[str, Any]]:
        # Rotated generation first — it holds the older half of the history.
        for path in (self.path.with_name(self.path.name + _ROTATED_SUFFIX), self.path):
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                if not line.strip():
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue  # torn line from an interrupted write
                if isinstance(parsed, dict):
                    yield parsed


def _at_or_after(event: dict[str, Any], since: datetime) -> bool:
    ts = _parse_ts(event.get("ts"))
    return ts is not None and ts >= since


def _parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


# --- summary --------------------------------------------------------------

_SINCE_RE = re.compile(r"^(\d+)([dhm])$")
_SINCE_UNITS = {"d": "days", "h": "hours", "m": "minutes"}


def parse_since(raw: str, now: datetime) -> datetime:
    """Parse ``7d`` / ``24h`` / ``90m`` or an ISO timestamp into an absolute time.

    A bare ISO *date* is read as local midnight, so ``--since 2026-07-23`` means
    the whole of that day rather than an instant.
    """
    m = _SINCE_RE.match(raw.strip())
    if m:
        return now - timedelta(**{_SINCE_UNITS[m.group(2)]: int(m.group(1))})
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError as e:
        raise ValueError(f"unrecognized --since {raw!r}: use 7d/24h/90m or an ISO timestamp") from e
    return parsed if parsed.tzinfo else parsed.astimezone()


def summarize(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Fold events into the shape the pilot gate is scored from.

    The ``layers`` block is the point of the whole module: it answers "has
    cleanup layer N ever been observed firing, on evidence rather than memory?"
    ``declared`` counts only teardowns whose caller stated its reason — the
    inferred ones are reported alongside, never merged in.
    """
    events = list(events)
    counts: dict[str, int] = {}
    layers: dict[str, dict[str, Any]] = {}
    kills = 0
    lease_cleanups = 0

    for e in events:
        kind = str(e.get("event", "unknown"))
        counts[kind] = counts.get(kind, 0) + 1
        if kind != DOWN:
            continue
        if e.get("killed"):
            kills += 1
        else:
            lease_cleanups += 1
        layer = e.get("layer")
        if layer is None:
            continue
        bucket = layers.setdefault(
            str(layer),
            {"count": 0, "reasons": {}, "declared": 0, "inferred": 0, "first": None, "last": None},
        )
        bucket["count"] += 1
        reason = str(e.get("reason", "unknown"))
        bucket["reasons"][reason] = bucket["reasons"].get(reason, 0) + 1
        if e.get("reason_source") == DECLARED:
            bucket["declared"] += 1
        else:
            bucket["inferred"] += 1
        ts = e.get("ts")
        if isinstance(ts, str):
            bucket["first"] = bucket["first"] or ts
            bucket["last"] = ts

    stamps = [e["ts"] for e in events if isinstance(e.get("ts"), str)]
    reports = [e for e in events if e.get("event") == FALSE_KILL]
    return {
        "total": len(events),
        "window": {"from": min(stamps), "to": max(stamps)} if stamps else None,
        "counts": counts,
        "layers": layers,
        "kills": kills,
        "lease_cleanups": lease_cleanups,
        "sessions": _session_summary(events),
        "false_kills": _false_kill_summary(reports, kills),
    }


def _session_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    """G1's score: how many distinct sessions have actually taken a lease.

    A session "used rentctl" when it took a lease, so this folds ``up`` events
    only — a teardown is the same session's second appearance, not a new one.

    ``unattributed`` is the load-bearing field and the reason this block exists
    rather than a bare integer. G1 asks for a count of real work sessions, and
    for 33 events rentctl could not supply one: every lease recorded
    :data:`UNATTRIBUTED` because the runtime's session variable was read under
    the wrong name (WI-0036). The tempting workaround — infer sessions from lease
    counts, or from clusters of ``cwd`` and timestamp — is exactly the
    fabrication this log was built to make unnecessary, so it is not done here
    and must not be done by a reader either.

    Hence ``complete``: false whenever any lease is unattributed, so a caller
    cannot read ``distinct`` as a total. `declare-what-a-check-assumes` — a count
    over partial data and a count over whole data must not print the same shape.
    """
    ups = [e for e in events if e.get("event") == UP]
    seen: list[str] = []
    unattributed = 0
    for e in ups:
        sid = e.get("session")
        if not isinstance(sid, str) or not sid or sid == UNATTRIBUTED:
            unattributed += 1
            continue
        if sid not in seen:
            seen.append(sid)
    return {
        "distinct": len(seen),
        "attributed_leases": len(ups) - unattributed,
        "unattributed_leases": unattributed,
        "complete": unattributed == 0,
        "note": (
            "`distinct` counts sessions rentctl could identify. "
            f"{unattributed} lease(s) carry no session id and are counted by "
            "NOBODY — they are not folded in, and must not be estimated from "
            "lease counts or timestamps."
        ) if unattributed else "Every lease in this window is attributed to a session.",
    }


def _false_kill_summary(reports: list[dict[str, Any]], kills: int) -> dict[str, Any]:
    """G3's score, stated so that zero cannot be misread as proof.

    Every other criterion folds evidence rentctl gathered itself. This one folds
    the *absence* of complaints, and absence of complaint is not evidence of
    absence — the whole reason G3 sat unscored. So the summary never says "pass":
    it reports what was received, against how many kills were performed, and says
    in words that the denominator is unobserved.

    Reporting `0` bare would be the ADR-0008 fold once more, in the surface that
    scores the gate: "nobody complained" and "nothing went wrong" would print the
    same, and the gate would be certified by silence.
    """
    unmatched = sum(1 for r in reports if r.get("unmatched"))
    return {
        "reported": len(reports),
        "unmatched": unmatched,
        "kills_in_window": kills,
        "basis": "self-reported",
        "note": (
            "Self-reported by users; rentctl cannot observe whether a kill was unwanted. "
            "Zero reports means nobody reported, NOT that no false kill occurred — "
            "G3 needs real pilot use behind these numbers, not just an empty list."
        ),
    }
