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
import subprocess
import sys

import psutil

from .models import ProcessHandle, ProcInfo

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
