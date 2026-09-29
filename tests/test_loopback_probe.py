"""WI-0072: the loopback probe must ask BOTH loopbacks.

`localhost` is not one address. Node >= 17 resolves it to ``::1`` first on
macOS, so a Vite dev server started with defaults listens on IPv6 loopback
only. A probe that dials ``127.0.0.1`` alone reported that server as
`readiness: listening` with a note that http://localhost "will not reach it" —
false, the browser reaches it fine — and `healthy: false` on every board.

These tests use REAL listeners on ephemeral ports: the thing under test is
which addresses a socket connect reaches, and a fake cannot answer that.
"""

from __future__ import annotations

import socket
from datetime import datetime

import pytest
from conftest import CDT, Clock
from fakesup import FakeSupervision

from rentctl.core import procutil
from rentctl.core import service as service_mod
from rentctl.core.service import Service


def _listener(family: int, host: str) -> socket.socket:
    """A listening socket on ``host`` at a kernel-chosen port (never 5100–5129)."""
    s = socket.socket(family, socket.SOCK_STREAM)
    try:
        s.bind((host, 0))
        s.listen()
    except OSError:
        s.close()
        raise
    return s


def _ipv6_listener_or_skip() -> socket.socket:
    """Bind ``::1`` for real, or skip — detected, not assumed.

    Linux CI containers sometimes ship without IPv6 loopback; `socket.has_ipv6`
    only says the Python build supports it, not that this host has ``::1``.
    """
    if not socket.has_ipv6:
        pytest.skip("Python built without IPv6 support")
    try:
        return _listener(socket.AF_INET6, "::1")
    except OSError as e:
        pytest.skip(f"IPv6 loopback (::1) unavailable on this host: {e}")


def test_probe_answers_for_a_server_on_ipv6_loopback_only():
    srv = _ipv6_listener_or_skip()
    try:
        port = srv.getsockname()[1]
        assert service_mod._port_answering(port) is True
    finally:
        srv.close()


def test_probe_answers_for_a_server_on_ipv4_loopback_only():
    srv = _listener(socket.AF_INET, "127.0.0.1")
    try:
        port = srv.getsockname()[1]
        assert service_mod._port_answering(port) is True
    finally:
        srv.close()


def test_probe_is_false_when_nothing_listens():
    # Bound but NOT listening: the port is reserved for the test's lifetime, so
    # no other process can take it mid-test, and a connect to it is refused.
    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        held.bind(("127.0.0.1", 0))
        port = held.getsockname()[1]
        assert service_mod._port_answering(port) is False
    finally:
        held.close()


def test_env_up_and_ls_treat_an_ipv6_only_server_as_answering(
    devctl_home, write_registry, sample_registry_data, monkeypatch
):
    """Through env_up/env_ls, with the REAL probe doing the dialling.

    Readiness is probed by the supervisor since ADR-0016, with this module's
    ``_port_answering``; the fake supervisor reports what that call returns.
    `healthy` on the board is dialled by the service itself, for real.

    The registry hands out webapp's block (5180), which is a real project's
    port on the developer's Mac — so the lease port is never bound here.
    Instead the health seam routes the lease port to the ephemeral ``::1``
    listener and calls the shipped ``_port_answering`` on it.
    """
    srv = _ipv6_listener_or_skip()
    try:
        real_port = srv.getsockname()[1]
        write_registry(sample_registry_data)
        monkeypatch.setattr(procutil, "port_owner", lambda port: None)
        clock = Clock(datetime(2026, 7, 14, 8, 0, tzinfo=CDT))
        # The supervisor probes readiness with this same `_port_answering`
        # (supervisor.py imports it), so the fake supervisor's "answered" stands
        # for exactly the call proven on ::1 above.
        world = FakeSupervision(
            devctl_home, clock,
            readiness="answered" if service_mod._port_answering(real_port) else "listening",
        )
        svc = Service(
            devctl_home,
            now_fn=clock,
            supervision=world,
            session_id_fn=lambda: "sess-1",
            port_answering_fn=lambda port: service_mod._port_answering(real_port),
        )

        up = svc.env_up("webapp", cwd="/proj/webapp")
        assert up["ok"] is True, up
        assert up["readiness"] == "answered"
        # The false claim this work item exists to remove.
        assert "readiness_detail" not in up

        envs = {e["project"]: e for e in svc.env_ls()["environments"]}
        assert envs["webapp"]["healthy"] is True
    finally:
        srv.close()
