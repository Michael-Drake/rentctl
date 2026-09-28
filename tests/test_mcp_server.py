"""Tests for the MCP shell: tools registered, envelopes surface (incl. errors)."""

from __future__ import annotations

import pytest

from rentctl import mcp_server as m
from rentctl.core.models import Readiness
from rentctl.core.service import Service

from conftest import FakeRunner


@pytest.fixture
def mcp_service(devctl_home, write_registry, sample_registry_data, monkeypatch):
    write_registry(sample_registry_data)
    runner = FakeRunner()
    svc = Service(
        devctl_home,
        runner_factory=lambda name: runner,
        readiness_fn=lambda port, timeout, pgid: Readiness.ANSWERED,
        watchdog_spawn=lambda p: None,
        # Injected rather than monkeypatched: nothing here fakes a squatter, so
        # there is no later `setattr` to leave room for. Both probes must be
        # stubbed or these tests read the developer's machine — they asserted
        # port 5180 and got 5182 once the pilot project held 5180 and 5181.
        port_owner_fn=lambda port: None,
        port_answering_fn=lambda port: False,
    )
    monkeypatch.setattr(m, "_service", svc)
    return svc


def test_four_tools_registered():
    names = {t.name for t in m.mcp._tool_manager.list_tools()}
    assert names == {"env_up", "env_down", "env_ls", "env_sweep"}


def test_env_up_success_envelope(mcp_service):
    res = m.env_up("webapp")
    assert res["ok"] is True
    assert res["port"] == 5180
    assert res["url"] == "http://localhost:5180"


def test_env_up_error_envelope(mcp_service):
    res = m.env_up("ghost")
    assert res["ok"] is False
    assert res["error"] == "UNKNOWN_PROJECT"


def test_env_ls_and_sweep(mcp_service):
    m.env_up("webapp")
    ls = m.env_ls()
    assert ls["ok"] is True
    assert [e["project"] for e in ls["environments"]] == ["webapp"]
    sweep = m.env_sweep()
    assert sweep["ok"] is True


def test_env_down(mcp_service):
    m.env_up("webapp")
    res = m.env_down("webapp")
    assert res["ok"] is True


def test_svc_lazily_built(devctl_home, monkeypatch):
    # devctl_home points DEVCTL_* at a tmp dir, so this touches no real state.
    monkeypatch.setattr(m, "_service", None)
    svc = m._svc()
    assert isinstance(svc, Service)


def test_handshake_reports_rentctl_version_not_the_sdks(devctl_home):
    """The real stdio handshake: `serverInfo.version` is rentctl's own version.

    Runs the server as a subprocess over stdio, the way a client does, rather than
    reading the attribute back — the attribute is private SDK API, and what matters
    is the number that actually crosses the wire.
    """
    import json
    import subprocess
    import sys

    from rentctl import __version__

    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    }
    p = subprocess.run(
        [sys.executable, "-c", "from rentctl.mcp_server import main; main()"],
        input=json.dumps(init) + "\n",
        capture_output=True,
        text=True,
        timeout=60,
    )
    reply = json.loads(p.stdout.splitlines()[0])
    assert reply["result"]["serverInfo"] == {"name": "rentctl", "version": __version__}
