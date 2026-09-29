"""A deterministic fake process table for the ADR-0016 membership seam.

Real processes cannot hit the races the membership logic exists for — a pid
reused inside a session, an identity that changes between enumeration and the
signal — on demand. This table can. It implements ``procutil.ProcessTable``,
records every signal it is asked to deliver, and models the dispositions that
matter: dies on TERM, ignores TERM, survives even SIGKILL (a D-state process or
another uid), and forks a new member when TERMed.
"""

from __future__ import annotations

import signal
from collections.abc import Callable
from dataclasses import dataclass, field

from rentctl.core.procutil import ProcRow, SignalResult


@dataclass
class FakeProc:
    pid: int
    sid: int
    start: float | None
    pgid: int | None = None
    name: str = "proc"
    status: str = "sleeping"
    ignores_term: bool = False
    unkillable: bool = False
    on_term: Callable[["FakeProcessTable", "FakeProc"], None] | None = None
    terms: int = field(default=0)
    ppid: int | None = None


class FakeProcessTable:
    def __init__(self) -> None:
        self.procs: dict[int, FakeProc] = {}
        self.sent: list[tuple[int, int]] = []
        # Called with the pid just before verify() runs: the hook for "the
        # identity changed between enumeration and the signal".
        self.before_verify: Callable[[int], None] | None = None

    def add(self, pid: int, sid: int, start: float | None, **kw) -> FakeProc:
        kw.setdefault("pgid", sid)
        proc = FakeProc(pid=pid, sid=sid, start=start, **kw)
        self.procs[pid] = proc
        return proc

    def kill_now(self, pid: int) -> None:
        self.procs.pop(pid, None)

    # --- ProcessTable ------------------------------------------------------

    def session_rows(self, sid: int) -> list[ProcRow]:
        return [self._row(p) for p in list(self.procs.values()) if p.sid == sid]

    def row(self, pid: int) -> ProcRow | None:
        p = self.procs.get(pid)
        return None if p is None else self._row(p)

    def descendant_rows(self, root: int) -> list[ProcRow]:
        out: list[ProcRow] = []
        frontier = [root]
        while frontier:
            parent = frontier.pop()
            for p in list(self.procs.values()):
                if p.ppid == parent and p.pid != root:
                    out.append(self._row(p))
                    frontier.append(p.pid)
        return out

    def send(self, pid: int, sig: int, verify) -> SignalResult:
        if self.before_verify is not None:
            self.before_verify(pid)
        refused = verify()
        if refused is not None:
            return refused
        self.sent.append((pid, sig))
        p = self.procs[pid]
        if sig == signal.SIGTERM:
            p.terms += 1
            if p.on_term is not None:
                p.on_term(self, p)
            if not p.ignores_term:
                self.procs.pop(pid, None)
        elif sig == signal.SIGKILL and not p.unkillable:
            self.procs.pop(pid, None)
        return SignalResult.SIGNALLED

    def signals_to(self, pid: int) -> list[int]:
        return [s for p, s in self.sent if p == pid]

    @staticmethod
    def _row(p: FakeProc) -> ProcRow:
        return ProcRow(pid=p.pid, sid=p.sid, start_time=p.start, pgid=p.pgid, name=p.name,
                       status=p.status, ppid=p.ppid)


class FakeClock:
    """``clock``/``sleep`` pair for ``stop_workload``: sleeping advances time."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s
