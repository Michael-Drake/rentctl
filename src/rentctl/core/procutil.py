# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Drake

"""Process-table and port queries — the OS-is-ground-truth boundary (spec §3.1).

The one impure island the rest of core reconciles against. The pure decision
logic (``start_time_matches``) is separated from the impure observation
(``observe_start_time``) so the PID-recycling comparison can be unit-tested
without a real process (spec §10 "PID-recycling comparison logic").
"""

from __future__ import annotations

import math
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

import psutil

from .models import ProcessHandle, ProcInfo, Survivor

# psutil.create_time() is stable across re-reads of the same process, but a
# JSON round-trip of the float can drift in the last bits. A 1 s tolerance is
# far tighter than any real PID-recycle gap (a reused PID belongs to a process
# that started much more than a second apart) yet immune to float-repr noise.
_START_TIME_TOL_S = 1.0


def start_time_matches(
    expected: float, observed: float | None, tol: float = _START_TIME_TOL_S
) -> bool:
    """Pure: does an observed start time match the one captured at spawn?

    ``observed is None`` means the PID is gone → no match. This is the whole
    PID-recycle guard: only ``True`` means "same process we started."
    """
    if observed is None:
        return False
    return math.isclose(expected, observed, abs_tol=tol, rel_tol=0.0)


def observe_start_time(pid: int) -> float | None:
    """Impure: current create_time for ``pid``, or ``None`` if it is gone/zombie."""
    try:
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return None
        return proc.create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None


def is_alive(handle: ProcessHandle, tol: float = _START_TIME_TOL_S) -> bool:
    """Impure convenience: is the *same* process behind this handle still alive?

    Combines observation + the pure comparison. ``False`` if the PID is gone
    OR was recycled — either way, nothing of ours is there to kill.
    """
    return start_time_matches(handle.pid_start_time, observe_start_time(handle.pid), tol)


def process_group_of(pid: int) -> int | None:
    """Impure: the process group id for ``pid``, or ``None`` if it cannot be read.

    ``None`` means *could not determine* — the process is gone, or the caller
    lacks permission. It never means "no group". Callers must not read it as
    "not ours": the process runner spawns with ``start_new_session=True``, so a
    dev server's children share the leader's pgid, and that is the only thing
    distinguishing our listener from a squatter that grabbed the same port.
    """
    try:
        return os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        return None


def snapshot_start_times(pids: list[int]) -> dict[int, float | None]:
    """Impure: build a {pid: start_time|None} table for a set of pids.

    The caller passes this snapshot into the pure reconciler, keeping the
    reconcile decisions testable against a fake table (spec §10 unit tests).
    """
    return {pid: observe_start_time(pid) for pid in pids}


# ==========================================================================
# Session membership (ADR-0016 §2, §3)
# ==========================================================================
#
# The workload is not the leader pid. It is every process in session S that
# was born no earlier than S's owner. 1.0.x watched the leader, so a SIGTERM-
# ignoring child outlived its dead shell and the lease was deleted over it
# (S1). Membership comes from a full process-table scan by SID, never from
# `children(recursive=True)`: once an intermediate parent exits its children
# reparent to launchd/init, and ancestry finds nothing (ADR-0016 E3/E4).


class Membership(str, Enum):
    """What a scan of session S established. Three answers, never two.

    ``AMBIGUOUS`` is the one that matters: PID S is held by a process that is
    not S's owner, so the number may have been reused and nothing found under
    it can be proved ours. It is deliberately not folded into ``EMPTY`` (that
    would delete a lease over a live server) nor into ``MEMBERS`` (that would
    signal a stranger's session). Callers refuse and surface it (ADR-0008).
    """

    MEMBERS = "members"
    EMPTY = "empty"
    AMBIGUOUS = "ambiguous"


class SignalResult(str, Enum):
    """The outcome of one :func:`verified_signal`. Only ``SIGNALLED`` sent anything."""

    SIGNALLED = "signalled"
    GONE = "gone"                    # exited (or is a zombie) before the signal
    IDENTITY_MISMATCH = "identity_mismatch"  # the pid now names a different process
    LEFT_SESSION = "left_session"    # same process, but it setsid()'d out of S
    DENIED = "denied"                # EPERM: another uid (sudo); cannot be ours to stop


@dataclass(frozen=True)
class ProcRow:
    """One process as the table saw it. ``None`` fields could not be read."""

    pid: int
    sid: int | None
    start_time: float | None
    pgid: int | None = None
    name: str = ""
    status: str = ""
    ppid: int | None = None

    @property
    def zombie(self) -> bool:
        return self.status == psutil.STATUS_ZOMBIE

    def survivor(self) -> Survivor:
        return Survivor(pid=self.pid, start_time=self.start_time, name=self.name, status=self.status)


@dataclass(frozen=True)
class SessionScan:
    """The tri-state answer plus the members it found (empty unless ``MEMBERS``)."""

    state: Membership
    members: tuple[ProcRow, ...] = ()
    reason: str = ""


class IdentityAmbiguous(RuntimeError):
    """Raised by :func:`session_members` when the owner cannot be verified.

    An exception for the same reason as :class:`ProbeUnavailable`: an empty
    list already means "verified empty", so returning one here would be read
    as "nothing of ours is left" by every caller that did not think to ask.
    """


class ProcessTable(Protocol):
    """The seam between the membership logic and the OS process table.

    The real table is :class:`OsProcessTable`. Tests substitute a fake so the
    decisions — reuse, escapers, a pid that changes identity between scan and
    signal — can be driven deterministically; real processes cannot hit those
    races on demand.
    """

    def session_rows(self, sid: int) -> list[ProcRow]:
        """Every process whose ``getsid() == sid`` right now (zombies included)."""
        ...

    def row(self, pid: int) -> ProcRow | None:
        """One process by pid, or ``None`` if there is no such pid."""
        ...

    def descendant_rows(self, root: int) -> list[ProcRow]:
        """Every descendant of ``root`` (children, recursively; zombies included).

        Consulted only for a Linux supervisor that holds the child-subreaper
        attribute, where the subtree is closed: an orphan inside it reparents to
        the nearest living ancestor subreaper, never out of it (ADR-0016 §5).
        """
        ...

    def send(
        self, pid: int, sig: int, verify: Callable[[], SignalResult | None]
    ) -> SignalResult:
        """Signal ``pid`` if ``verify()`` returns ``None``; otherwise return its refusal.

        ``verify`` runs *inside* ``send`` so a table that can pin the target
        first (a Linux pidfd) re-checks identity after pinning, which closes the
        check-to-signal race rather than narrowing it (ADR-0016 §3).
        """
        ...


# Linux ≥ 5.3 with Python ≥ 3.9. Feature-detected rather than platform-checked:
# a restricted container can lack the syscall on a kernel that is new enough.
_HAVE_PIDFD = hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal")


class OsProcessTable:
    """The real process table: ``psutil.pids()`` + ``os.getsid`` (ADR-0016 E1).

    psutil has no SID accessor, and ``os.getsid`` on another user's process is
    unprivileged on macOS, so this is a full scan. It costs well under a
    millisecond on a laptop-sized table.
    """

    def session_rows(self, sid: int) -> list[ProcRow]:
        out: list[ProcRow] = []
        for pid in psutil.pids():
            if pid == 0:
                # getsid(0) means "the caller", not pid 0: the probe's first run
                # returned the harness's own session for kernel_task (E1).
                continue
            try:
                if os.getsid(pid) != sid:
                    continue
            except OSError:
                # Exited mid-scan, or (macOS) a zombie, whose getsid is ESRCH.
                # Neither can be a member, so skipping loses nothing.
                continue
            row = self.row(pid)
            if row is not None and row.sid == sid:
                out.append(row)
        return out

    def row(self, pid: int) -> ProcRow | None:
        if pid <= 0:
            return None
        try:
            proc = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return None
        try:
            # create_time and status both answer for a zombie on macOS; the
            # zombie then reads as holding its pid, which is what it is doing.
            start: float | None = proc.create_time()
            status = proc.status()
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return None
        except psutil.AccessDenied:
            start, status = None, ""
        try:
            name = proc.name()
        except psutil.Error:
            name = ""
        try:
            ppid: int | None = proc.ppid()
        except psutil.Error:
            ppid = None
        return ProcRow(
            pid=pid,
            sid=_os_id(os.getsid, pid),
            start_time=start,
            pgid=_os_id(os.getpgid, pid),
            name=name,
            status=status,
            ppid=ppid,
        )

    def descendant_rows(self, root: int) -> list[ProcRow]:
        try:
            kids = psutil.Process(root).children(recursive=True)
        except psutil.Error:
            return []
        rows = (self.row(k.pid) for k in kids)
        return [r for r in rows if r is not None]

    def send(
        self, pid: int, sig: int, verify: Callable[[], SignalResult | None]
    ) -> SignalResult:
        if _HAVE_PIDFD:  # pragma: no cover - Linux only; the macOS legs cannot run it
            try:
                fd = os.pidfd_open(pid)
            except ProcessLookupError:
                return SignalResult.GONE
            except OSError:
                fd = None  # the syscall is present but refused (seccomp): plain path
            if fd is not None:
                try:
                    refused = verify()
                    if refused is not None:
                        return refused
                    signal.pidfd_send_signal(fd, sig)
                    return SignalResult.SIGNALLED
                except ProcessLookupError:
                    return SignalResult.GONE
                except PermissionError:
                    return SignalResult.DENIED
                finally:
                    os.close(fd)
        # macOS: no pidfd. The window between verify() and kill() needs the member
        # to exit AND its pid to wrap round 99999 inside microseconds. That is the
        # stated residual limit (ADR-0016 §3), not an unexamined one.
        refused = verify()
        if refused is not None:
            return refused
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return SignalResult.GONE
        except PermissionError:
            return SignalResult.DENIED
        return SignalResult.SIGNALLED


def _os_id(fn: Callable[[int], int], pid: int) -> int | None:
    try:
        return fn(pid)
    except OSError:
        return None


_OS_TABLE = OsProcessTable()


def classify_session(
    rows: Iterable[ProcRow],
    owner_row: ProcRow | None,
    sid: int,
    owner_start: float | None,
    exclude: Collection[int] = (),
    tol: float = _START_TIME_TOL_S,
    tree: Iterable[ProcRow] = (),
) -> SessionScan:
    """Pure: the membership decision over one snapshot (ADR-0016 §2).

    ``rows`` is what the scan found in session ``sid``; ``owner_row`` is
    whatever currently holds PID ``sid`` (``None`` if nothing does).

    ``tree`` is the Linux addition (§5, plan step 9): the descendants of a
    supervisor that holds the child-subreaper attribute. They are members
    whatever their SID, which is what captures a ``setsid``/daemonize escaper
    that is still in the supervisor's subtree. Empty everywhere else.

    The order of the checks is the design:

    1. **No live process in session S at all → EMPTY**, whatever PID S now is.
       A session exists only while something is in it, so with nobody in S
       there is nothing of ours to find and nothing to signal; a reused number
       is moot. Checking this first is what lets a lease whose workload ended
       long ago be cleaned instead of stuck as ambiguous forever.
    2. **Owner unverifiable → AMBIGUOUS.** Either no owner start time was ever
       recorded (the 0.0 sentinel), or PID S is held by a process whose start
       time is not the owner's. A process that took PID S and called setsid()
       would itself be "in S" and newer than our owner — i.e. it would pass the
       membership test. So once the number is in doubt, nothing under it is
       signalled.
    3. **Otherwise MEMBERS or EMPTY** by ADR §2: in S, not a zombie, not in
       ``exclude`` (the supervisor), and born no earlier than the owner less
       the tolerance.

    A live row in S whose start time could not be read is kept as a member.
    With the owner verified, everything in S is ours; condition 3 exists only
    to defend against reuse, which step 2 has already excluded. Keeping it
    means it is reported as a survivor — :func:`verified_signal` will not
    signal a process it cannot identify — rather than silently dropped.
    """
    live = [r for r in rows if r.sid == sid and not r.zombie]
    in_session = {r.pid for r in live}
    live += [r for r in tree if not r.zombie and r.pid not in in_session]
    if not live:
        return SessionScan(Membership.EMPTY)
    if owner_start is None or owner_start <= 0:
        return SessionScan(
            Membership.AMBIGUOUS, reason=f"no start time was recorded for session {sid}'s owner"
        )
    if owner_row is not None and not start_time_matches(owner_start, owner_row.start_time, tol):
        return SessionScan(
            Membership.AMBIGUOUS,
            reason=f"pid {sid} is now held by a different process ({owner_row.name or 'unnamed'})",
        )
    members = tuple(
        sorted(
            (
                r
                for r in live
                if r.pid not in exclude
                and (r.start_time is None or r.start_time >= owner_start - tol)
            ),
            key=lambda r: r.pid,
        )
    )
    return SessionScan(Membership.MEMBERS if members else Membership.EMPTY, members)


def session_scan(
    sid: int,
    owner_start: float | None,
    exclude: Collection[int] = (),
    *,
    table: ProcessTable | None = None,
    tree_root: int | None = None,
) -> SessionScan:
    """Impure: scan the table and classify session ``sid`` (tri-state).

    ``tree_root`` adds the descendants of that pid (a subreaper supervisor,
    i.e. the caller itself) to the member set; see :func:`classify_session`.
    """
    t = table or _OS_TABLE
    tree = t.descendant_rows(tree_root) if tree_root is not None else ()
    return classify_session(t.session_rows(sid), t.row(sid), sid, owner_start, exclude, tree=tree)


def session_members(
    sid: int,
    owner_start: float | None,
    exclude: Collection[int] = (),
    *,
    table: ProcessTable | None = None,
) -> list[ProcRow]:
    """The verified members of session ``sid``. Raises :class:`IdentityAmbiguous`.

    ``[]`` means verified empty and nothing else. Use :func:`session_scan` to
    handle the ambiguous case as a value rather than an exception.
    """
    scan = session_scan(sid, owner_start, exclude, table=table)
    if scan.state is Membership.AMBIGUOUS:
        raise IdentityAmbiguous(scan.reason)
    return list(scan.members)


def owner_reused(
    sid: int, owner_start: float | None, *, table: ProcessTable | None = None
) -> bool:
    """Is PID ``sid`` held by something we cannot prove is the session's owner?

    ``False`` when the pid is free (the owner exited; S stays reserved while it
    has members) or still names the owner. ``True`` means the number may have
    been reused — the input :func:`classify_session` turns into ``AMBIGUOUS``
    when the session is not empty.
    """
    row = (table or _OS_TABLE).row(sid)
    if row is None:
        return False
    if owner_start is None or owner_start <= 0:
        return True
    return not start_time_matches(owner_start, row.start_time)


def verified_signal(
    pid: int,
    ctime: float | None,
    sid: int,
    sig: int,
    *,
    table: ProcessTable | None = None,
    tree_root: int | None = None,
) -> SignalResult:
    """Send ``sig`` to ``pid`` only if it is still the process we enumerated.

    Identity is re-checked **immediately before** the signal (ADR-0016 §3):
    the start time must equal ``ctime`` (captured at enumeration) *and* the
    process must still be in session ``sid`` — or, when ``tree_root`` is given
    (a Linux subreaper supervisor), still be a descendant of it. One pid, never
    a group. A pid of 0 or below is refused outright — to ``kill()`` those mean
    "my group" and "everything", which no caller here can intend — and so is
    our own pid.

    On Linux the table pins the target with a pidfd *before* ``verify`` runs
    and signals through that pidfd, so a pid recycled after the check cannot
    receive the signal (``OsProcessTable.send``).
    """
    if pid <= 0 or pid == os.getpid() or ctime is None:
        return SignalResult.IDENTITY_MISMATCH
    t = table or _OS_TABLE

    def verify() -> SignalResult | None:
        row = t.row(pid)
        if row is None or row.zombie:
            return SignalResult.GONE
        if not start_time_matches(ctime, row.start_time):
            return SignalResult.IDENTITY_MISMATCH
        if row.sid != sid and not (tree_root is not None and in_tree(row, tree_root, table=t)):
            return SignalResult.LEFT_SESSION
        return None

    return t.send(pid, sig, verify)


_MAX_TREE_DEPTH = 256  # a parent chain longer than this is a cycle or a race; not ours


def in_tree(row: ProcRow, root: int, *, table: ProcessTable | None = None) -> bool:
    """Is ``row``'s process a descendant of ``root``? Walks the parent chain.

    ``False`` whenever a link cannot be read: an unprovable descendant is not
    signalled. pid 1 (init) and 0 end the walk — nothing above them is ours.
    """
    t = table or _OS_TABLE
    ppid = row.ppid
    for _ in range(_MAX_TREE_DEPTH):
        if ppid is None or ppid <= 1:
            return False
        if ppid == root:
            return True
        parent = t.row(ppid)
        ppid = None if parent is None else parent.ppid
    return False


# ==========================================================================
# Linux extras (ADR-0016 §5, plan step 9): the child subreaper and pidfds
# ==========================================================================
#
# Both are feature-detected, never assumed from sys.platform alone: a
# restricted container can refuse either on a kernel that has it. A host that
# lacks them degrades to the macOS guarantee, and the level in effect is
# recorded on the lease (SupervisorRef.supervision) and reported by doctor.

#: Only processes that stay in session S are supervised (macOS; degraded Linux).
SUPERVISION_SESSION = "session"
#: Session S plus every descendant of the supervisor, which holds the Linux
#: child-subreaper attribute so orphans reparent to it instead of init.
SUPERVISION_SUBREAPER = "session+subreaper"

# <linux/prctl.h>. Both since Linux 3.4 (PR_SET_CHILD_SUBREAPER(2const),
# PR_GET_CHILD_SUBREAPER(2const)).
PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37


def _load_libc():  # pragma: no cover - exercised on Linux only; tests inject a loader
    import ctypes

    return ctypes.CDLL(None, use_errno=True)


def enable_child_subreaper(
    *, platform: str | None = None, libc_loader: Callable[[], object] | None = None
) -> bool:
    """Make the **calling process** a child subreaper; return whether it now is.

    ``prctl(PR_SET_CHILD_SUBREAPER, 1)``, then read it back with
    ``PR_GET_CHILD_SUBREAPER``: the read-back is what decides, so a call that
    "succeeds" without effect is not reported as the stronger guarantee. Any
    failure — not Linux, no libc symbol, a non-zero return (seccomp), an
    exception from ctypes — returns ``False`` and the caller stays at
    :data:`SUPERVISION_SESSION`. Unprivileged by design (ADR-0016 L5).

    Call it only in the supervisor, before its first child exists: the
    attribute is per process, not inherited by children, and kept across exec.
    """
    if not (platform or sys.platform).startswith("linux"):
        return False
    import ctypes

    try:
        libc = (libc_loader or _load_libc)()
        prctl = libc.prctl  # type: ignore[attr-defined]
        # prctl(2) is variadic over unsigned long; declare it so ctypes passes
        # full-width arguments rather than C ints.
        prctl.argtypes = [ctypes.c_int] + [ctypes.c_ulong] * 4
        prctl.restype = ctypes.c_int
        if prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
            return False
        isset = ctypes.c_int(0)
        if prctl(PR_GET_CHILD_SUBREAPER, ctypes.addressof(isset), 0, 0, 0) != 0:
            return False
    except Exception:
        return False
    return isset.value != 0


def pidfd_available() -> bool:
    """Can this process open and signal through a pidfd right now? (Linux ≥ 5.3)

    Executes the capability rather than checking for the attribute: a pidfd on
    ourselves, signal 0 through it (an existence check that delivers nothing),
    then closed. ``False`` wherever that does not work, including macOS.
    """
    if not _HAVE_PIDFD:
        return False
    try:  # pragma: no cover - Linux only; the macOS legs have no pidfd
        fd = os.pidfd_open(os.getpid())
        try:
            signal.pidfd_send_signal(fd, 0)
        finally:
            os.close(fd)
    except OSError:  # pragma: no cover - Linux only
        return False
    return True  # pragma: no cover - Linux only


def reap_orphans(keep: Collection[int] = ()) -> list[int]:
    """Reap every exited **direct child** of this process except those in ``keep``.

    A subreaper inherits orphans it never spawned, and nothing else will wait
    for them (ADR-0016 §5). The ADR sketches ``waitpid(-1, WNOHANG)``; this is
    deliberately per pid instead, because ``waitpid(-1)`` would also collect
    the leader, whose exit status belongs to its ``Popen`` (``keep``) — Popen
    would then read ECHILD and invent a return code of 0. ``WNOHANG`` on a
    child that is still running is a no-op. Returns the pids reaped.
    """
    try:
        kids = [k.pid for k in psutil.Process().children()]
    except psutil.Error:  # pragma: no cover - our own process is always readable
        return []
    reaped: list[int] = []
    for pid in kids:
        if pid in keep:
            continue
        try:
            done, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            continue  # reaped already (or never ours): nothing to do
        if done:
            reaped.append(done)
    return reaped


class ProbeUnavailable(RuntimeError):
    """No usable backend could answer whether a port has a listener (ADR-0008).

    Deliberately an exception rather than a third return value. ``None`` already
    means *verified free* and every call site reads it that way, so a sentinel
    would be silently absorbed into the old fail-open (or, being truthy, read as
    "a listener is present" — a fail-closed by accident rather than by decision).
    Raising forces each caller to say what it wants when the check cannot run.
    """


def _port_owner_lsof(port: int) -> ProcInfo | None:
    """``lsof`` backend — the macOS path (ADR-0005 §4).

    Chosen over ``psutil.net_connections`` there because that raises
    ``AccessDenied`` for an ordinary user on macOS, and devctl's use is exactly
    the privileged case: finding a listener it does *not* own.
    """
    lsof = shutil.which("lsof")
    if lsof is None:
        raise ProbeUnavailable("lsof is not installed")
    try:
        out = subprocess.run(
            # `-w` suppresses lsof's benign warnings (unreadable mount points and
            # the like). It is not cosmetic: without it stderr is noisy on a
            # perfectly good run, so stderr cannot be used to tell a usage error
            # from "nothing is listening" — which is what the check below needs.
            [lsof, "-w", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpcn"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        # Previously swallowed to None — i.e. reported as "port is free" when the
        # probe had in fact failed to run. That is the ADR-0008 fold.
        raise ProbeUnavailable(f"lsof failed to run: {e}") from e
    if out.returncode != 0 and not out.stdout:
        # lsof exits non-zero with no stdout for BOTH "nothing is listening" and
        # a usage error (bad flag, unsupported syntax on an unexpected build).
        # This used to return None for both, which reports a probe that never ran
        # as verified-free — the exact ADR-0008 fold, reintroduced through the
        # backend that is PRIMARY on macOS, and a silent breach of the README's
        # promise that "No squatters found" means something looked.
        #
        # An earlier comment here claimed separating the two needs a version
        # sniff. It does not. With `-w` quieting benign warnings, a non-empty
        # stderr on a non-zero exit is the usage error; a silent non-zero exit is
        # the real "no listener record" answer.
        if out.stderr.strip():
            raise ProbeUnavailable(
                f"lsof exited {out.returncode} with no output: {out.stderr.strip()}"
            )
        return None
    if not out.stdout:
        return None
    return _parse_lsof_fields(out.stdout)


def _port_owner_psutil(port: int) -> ProcInfo | None:
    """``psutil`` backend — the Linux path (ADR-0005 §4).

    Unprivileged there, and psutil is already a declared dependency, so this
    needs no new package and no subprocess. ADR-0005's Alternatives rejected
    shelling out to ``ss`` for exactly this reason; ADR-0008 §4 re-rejected it.
    """
    try:
        conns = psutil.net_connections(kind="tcp")
    except (psutil.AccessDenied, PermissionError) as e:
        raise ProbeUnavailable(f"psutil.net_connections denied: {e}") from e
    for c in conns:
        if c.status != psutil.CONN_LISTEN or c.laddr is None:
            continue
        if getattr(c.laddr, "port", None) != port:
            continue
        if c.pid is None:
            # A listener we can see but cannot attribute is NOT "nobody there."
            raise ProbeUnavailable(
                f"a listener on {port} could not be attributed to a pid"
            )
        name = ""
        try:
            name = psutil.Process(c.pid).name()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
        return ProcInfo(pid=c.pid, name=name, cmdline=())
    return None


def port_owner(port: int) -> ProcInfo | None:
    """Impure: the process LISTENing on ``port``, at **any** bind address.

    The address-agnostic part is load-bearing and used to be documented
    backwards ("on localhost"). Neither backend restricts by address — ``lsof``
    matches ``-iTCP:<port>`` and psutil filters on ``laddr.port`` alone — which
    is what lets the readiness probe see a server bound to a specific
    non-loopback interface. The old wording described a narrower behaviour than
    the code had, in the one direction that would discourage the caller that
    needed it.

    Three outcomes, per [ADR-0008]:

    - a ``ProcInfo``      — someone is listening
    - ``None``            — **verified** free; the probe ran and found nothing
    - ``ProbeUnavailable`` raised — the probe could not answer at all

    The third used to be folded into ``None``, which made an unrunnable check
    indistinguishable from a clean one. Used for the ``PORT_SQUATTED`` owner
    report (F7) and squatter detection in ``env_ls``.

    Backend per ADR-0005 §4: ``lsof`` on macOS, ``psutil`` elsewhere, with a
    cross-fallback so a host that has only one of them still gets an answer.
    """
    if sys.platform == "darwin":
        primary, secondary = _port_owner_lsof, _port_owner_psutil
    else:
        primary, secondary = _port_owner_psutil, _port_owner_lsof
    try:
        return primary(port)
    except ProbeUnavailable as first:
        try:
            return secondary(port)
        except ProbeUnavailable as second:
            raise ProbeUnavailable(
                f"no usable port probe: {first}; then {second}"
            ) from second


def _parse_lsof_fields(text: str) -> ProcInfo | None:
    """Parse ``lsof -F`` field output (one ``<tag><value>`` per line).

    ``p`` = pid, ``c`` = command name. We take the first process record.
    """
    pid: int | None = None
    name = ""
    for line in text.splitlines():
        if not line:
            continue
        tag, value = line[0], line[1:]
        if tag == "p":
            if pid is not None:
                break  # next process record; keep the first
            pid = int(value)
        elif tag == "c" and pid is not None:
            name = value
    if pid is None:
        return None
    return ProcInfo(pid=pid, name=name, cmdline=())
