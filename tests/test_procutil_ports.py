"""Tests for port-owner detection (lsof parsing + a real listening socket)."""

from __future__ import annotations

import os
import socket

import pytest

from rentctl.core import procutil
from rentctl.core.models import ProcInfo


# --- pure lsof -F field parsing -------------------------------------------

def test_parse_lsof_fields_single_process():
    text = "p4242\ncnode\nn127.0.0.1:5180\n"
    assert procutil._parse_lsof_fields(text) == ProcInfo(pid=4242, name="node", cmdline=())


def test_parse_lsof_fields_takes_first_process():
    text = "p4242\ncnode\np9999\ncpython\n"
    got = procutil._parse_lsof_fields(text)
    assert got.pid == 4242 and got.name == "node"


def test_parse_lsof_fields_empty_is_none():
    assert procutil._parse_lsof_fields("") is None
    assert procutil._parse_lsof_fields("\n\n") is None


# --- real socket round-trip through lsof ----------------------------------

def test_port_owner_finds_this_process():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        owner = procutil.port_owner(port)
        assert owner is not None
        assert owner.pid == os.getpid()
    finally:
        srv.close()


def test_port_owner_free_port_is_none():
    # Grab a port, close it, then query it — almost certainly free.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert procutil.port_owner(port) is None


# --- ADR-0008: "cannot determine" is not "nobody is listening" -------------
#
# The branch these cover was `# pragma: no cover - lsof is present on macOS`,
# which was an accurate statement about the dev machine and exactly why the
# fail-open survived. Both backends are forced unavailable here, so the tests
# do not depend on which platform they run on.


def _both_backends_unavailable(monkeypatch):
    monkeypatch.setattr(procutil.shutil, "which", lambda _name: None)

    def _denied(**_kw):
        raise procutil.psutil.AccessDenied(pid=None)

    monkeypatch.setattr(procutil.psutil, "net_connections", _denied)


def test_port_owner_raises_when_no_backend_is_available(monkeypatch):
    """The whole point: unrunnable must not be reported as free."""
    _both_backends_unavailable(monkeypatch)
    with pytest.raises(procutil.ProbeUnavailable):
        procutil.port_owner(5100)


def test_probe_unavailable_names_both_failed_backends(monkeypatch):
    """An operator has to be able to tell WHY nothing could answer."""
    _both_backends_unavailable(monkeypatch)
    with pytest.raises(procutil.ProbeUnavailable) as ei:
        procutil.port_owner(5100)
    msg = str(ei.value)
    assert "lsof" in msg and "psutil" in msg


def test_probe_unavailable_is_not_falsy_or_none_shaped():
    """Guard against re-collapsing the three states into two.

    A future refactor that returns a sentinel instead of raising would silently
    restore the fold — `None` already means verified-free, and a truthy sentinel
    would read as "a listener is present" at the draw site.
    """
    assert issubclass(procutil.ProbeUnavailable, Exception)


def test_lsof_backend_missing_binary_raises_not_none(monkeypatch):
    monkeypatch.setattr(procutil.shutil, "which", lambda _name: None)
    with pytest.raises(procutil.ProbeUnavailable):
        procutil._port_owner_lsof(5100)


def test_lsof_backend_subprocess_failure_raises_not_none(monkeypatch):
    """A probe that errored is not a probe that found nothing."""
    monkeypatch.setattr(procutil.shutil, "which", lambda _name: "/usr/sbin/lsof")

    def _boom(*_a, **_kw):
        raise OSError("no fork for you")

    monkeypatch.setattr(procutil.subprocess, "run", _boom)
    with pytest.raises(procutil.ProbeUnavailable):
        procutil._port_owner_lsof(5100)


def test_psutil_backend_finds_this_process():
    """The Linux path, exercised on whatever platform CI runs."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        try:
            owner = procutil._port_owner_psutil(port)
        except procutil.ProbeUnavailable:
            pytest.skip("psutil.net_connections is not permitted on this host")
        assert owner is not None
        assert owner.pid == os.getpid()
    finally:
        srv.close()


def test_psutil_backend_denied_raises(monkeypatch):
    def _denied(**_kw):
        raise procutil.psutil.AccessDenied(pid=None)

    monkeypatch.setattr(procutil.psutil, "net_connections", _denied)
    with pytest.raises(procutil.ProbeUnavailable):
        procutil._port_owner_psutil(5100)


def test_psutil_backend_unattributable_listener_is_not_free(monkeypatch):
    """A listener we can see but cannot name is "cannot determine", not "free".

    Reporting it as free would route a draw straight onto an occupied port.
    """

    class _Addr:
        port = 5100

    class _Conn:
        status = procutil.psutil.CONN_LISTEN
        laddr = _Addr()
        pid = None

    monkeypatch.setattr(procutil.psutil, "net_connections", lambda **_kw: [_Conn()])
    with pytest.raises(procutil.ProbeUnavailable):
        procutil._port_owner_psutil(5100)


class _FakeAddr:
    def __init__(self, port):
        self.port = port


class _FakeConn:
    def __init__(self, port, pid, status=None):
        self.status = status if status is not None else procutil.psutil.CONN_LISTEN
        self.laddr = _FakeAddr(port) if port is not None else None
        self.pid = pid


def test_psutil_backend_returns_owner_for_matching_listener(monkeypatch):
    """Happy path of the Linux backend, exercised without a Linux host."""
    monkeypatch.setattr(
        procutil.psutil, "net_connections", lambda **_kw: [_FakeConn(5100, os.getpid())]
    )
    got = procutil._port_owner_psutil(5100)
    assert got is not None and got.pid == os.getpid()


def test_psutil_backend_skips_non_listening_and_other_ports(monkeypatch):
    """Verified-free means the loop ran and matched nothing — not that it errored."""
    conns = [
        _FakeConn(5100, 1, status="ESTABLISHED"),   # right port, not listening
        _FakeConn(5199, 2),                        # listening, wrong port
        _FakeConn(None, 3),                        # no laddr at all
    ]
    monkeypatch.setattr(procutil.psutil, "net_connections", lambda **_kw: conns)
    assert procutil._port_owner_psutil(5100) is None


def test_psutil_backend_unnamable_process_still_reports_the_pid(monkeypatch):
    """Losing the process NAME must not lose the finding — the pid is the point."""
    monkeypatch.setattr(
        procutil.psutil, "net_connections", lambda **_kw: [_FakeConn(5100, 999999)]
    )

    def _gone(_pid):
        raise procutil.psutil.NoSuchProcess(999999)

    monkeypatch.setattr(procutil.psutil, "Process", _gone)
    got = procutil._port_owner_psutil(5100)
    assert got is not None and got.pid == 999999 and got.name == ""


def test_lsof_backend_empty_output_is_verified_free(monkeypatch):
    monkeypatch.setattr(procutil.shutil, "which", lambda _n: "/usr/sbin/lsof")

    class _Out:
        returncode = 0
        stdout = ""

    monkeypatch.setattr(procutil.subprocess, "run", lambda *_a, **_kw: _Out())
    assert procutil._port_owner_lsof(5100) is None


class _LsofOut:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_lsof_backend_usage_error_raises_not_verified_free(monkeypatch):
    """A non-zero exit that COMPLAINED is a broken probe, not an empty result.

    This is the ADR-0008 fold's last hiding place: `lsof` exits non-zero with no
    stdout both for "nothing is listening" and for a usage error, and returning
    `None` for the second reports a probe that never ran as verified-free. On
    macOS this is the PRIMARY backend, and `port_owner` only falls back on
    `ProbeUnavailable` — so a `None` here is passed straight through as fact.
    """
    monkeypatch.setattr(procutil.shutil, "which", lambda _n: "/usr/sbin/lsof")
    monkeypatch.setattr(
        procutil.subprocess,
        "run",
        lambda *_a, **_kw: _LsofOut(
            returncode=1, stdout="", stderr="lsof: unsupported option -- Fpcn"
        ),
    )
    with pytest.raises(procutil.ProbeUnavailable) as ei:
        procutil._port_owner_lsof(5100)
    # The operator needs the tool's own words, or this is undiagnosable.
    assert "unsupported option" in str(ei.value)


def test_lsof_backend_silent_nonzero_exit_is_still_verified_free(monkeypatch):
    """The other half of the same branch — don't over-correct into false alarms.

    `lsof -iTCP:PORT -sTCP:LISTEN` exits 1 with no output and NOTHING on stderr
    when genuinely nothing is listening. That is a real answer and must stay
    `None`; if this raised, every free port would report as unprobeable and the
    port draw would have nothing to work with.
    """
    monkeypatch.setattr(procutil.shutil, "which", lambda _n: "/usr/sbin/lsof")
    monkeypatch.setattr(
        procutil.subprocess,
        "run",
        lambda *_a, **_kw: _LsofOut(returncode=1, stdout="", stderr=""),
    )
    assert procutil._port_owner_lsof(5100) is None


def test_lsof_backend_passes_dash_w_to_keep_stderr_meaningful(monkeypatch):
    """`-w` is load-bearing for the check above, so assert it is actually passed.

    Without it lsof warns about unreadable mount points on a perfectly good run,
    stderr is non-empty for healthy probes, and the usage-error branch above turns
    into a false `ProbeUnavailable` on every call.
    """
    monkeypatch.setattr(procutil.shutil, "which", lambda _n: "/usr/sbin/lsof")
    seen = []

    def _capture(argv, **_kw):
        seen.append(argv)
        return _LsofOut(returncode=0, stdout="")

    monkeypatch.setattr(procutil.subprocess, "run", _capture)
    procutil._port_owner_lsof(5100)
    assert "-w" in seen[0]


def test_non_darwin_prefers_the_psutil_backend(monkeypatch):
    """ADR-0005 §4's platform dispatch, asserted rather than assumed."""
    monkeypatch.setattr(procutil.sys, "platform", "linux")
    calls = []

    def _psutil_backend(_port):
        calls.append("psutil")
        return None

    def _lsof_backend(_port):  # pragma: no cover - must not be reached
        calls.append("lsof")
        return None

    monkeypatch.setattr(procutil, "_port_owner_psutil", _psutil_backend)
    monkeypatch.setattr(procutil, "_port_owner_lsof", _lsof_backend)
    assert procutil.port_owner(5100) is None
    assert calls == ["psutil"]


def test_port_owner_falls_back_to_the_other_backend(monkeypatch):
    """One backend missing is not "cannot determine" — the other still answers."""
    sentinel = ProcInfo(pid=4242, name="node", cmdline=())

    def _unavailable(_port):
        raise procutil.ProbeUnavailable("primary is out")

    if procutil.sys.platform == "darwin":
        monkeypatch.setattr(procutil, "_port_owner_lsof", _unavailable)
        monkeypatch.setattr(procutil, "_port_owner_psutil", lambda _p: sentinel)
    else:
        monkeypatch.setattr(procutil, "_port_owner_psutil", _unavailable)
        monkeypatch.setattr(procutil, "_port_owner_lsof", lambda _p: sentinel)

    assert procutil.port_owner(5100) == sentinel
