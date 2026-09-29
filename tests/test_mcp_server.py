"""Tests for the MCP shell: tools registered, envelopes surface (incl. errors)."""

from __future__ import annotations

import json
from datetime import datetime

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import RequestParams

from fakesup import FakeSupervision
from rentctl import mcp_server as m
from rentctl.core import service as service_mod
from rentctl.core.service import Service

from conftest import CDT, Clock


@pytest.fixture
def world(devctl_home):
    return FakeSupervision(devctl_home, Clock(datetime(2026, 7, 14, 8, 0, tzinfo=CDT)))


@pytest.fixture
def mcp_service(devctl_home, write_registry, sample_registry_data, monkeypatch, world):
    write_registry(sample_registry_data)
    svc = Service(
        devctl_home,
        now_fn=world.clock,
        supervision=world,
        term_grace_s=0.05,
        kill_grace_s=0.05,
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
    assert res["stopped"] is True


def test_env_down_waits_a_bounded_15s_then_reports_pending(mcp_service, world):
    """R6: an agent that asked for a stop normally sees it complete; a stop that
    takes longer comes back `pending` (still ok) after 15 s, not later."""
    m.env_up("webapp")
    world.hung = True
    start = world.monotonic()
    res = m.env_down("webapp")
    assert res["ok"] is True and res["pending"] is True
    assert world.monotonic() - start == pytest.approx(m.MCP_DOWN_WAIT_S, abs=0.2)
    world.hung = False
    world.tick()


def test_env_down_wait_s_is_clamped(mcp_service, world):
    m.env_up("webapp")
    world.hung = True
    start = world.monotonic()
    res = m.env_down("webapp", wait_s=10_000)
    assert res["pending"] is True
    assert world.monotonic() - start == pytest.approx(m.MCP_MAX_WAIT_S, abs=0.2)
    world.hung = False
    world.tick()


def test_env_down_wait_zero_is_fire_and_forget(mcp_service, world):
    m.env_up("webapp")
    res = m.env_down("webapp", wait_s=0)
    assert res["pending"] is True
    world.tick()
    assert m.env_ls()["environments"] == []


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


# --- ADR-0018 §4/§5: annotations and per-call session identity over the protocol --


def _over_the_protocol(body):
    """Run ``body(client)`` against this module's server through a real MCP
    client session (in-memory streams), so `tools/list` and `tools/call` —
    `_meta` included — cross the protocol exactly as a client sends them."""

    async def main():
        async with create_connected_server_and_client_session(m.mcp) as client:
            return await body(client)

    return anyio.run(main)


def _payload(result):
    assert not result.isError, result
    return json.loads(result.content[0].text)


def test_tools_list_carries_honest_annotations():
    """`codex exec` refuses a tool with no annotations; it passes one that is
    read-only, or not destructive and closed-world. env_sweep stays behind an
    approval because under strict enforcement it can reclaim a foreign port."""

    async def body(client):
        return {t.name: t.annotations for t in (await client.list_tools()).tools}

    ann = _over_the_protocol(body)
    assert ann["env_ls"].readOnlyHint is True and ann["env_ls"].openWorldHint is False
    for name in ("env_up", "env_down"):
        a = ann[name]
        assert (a.readOnlyHint, a.destructiveHint, a.idempotentHint, a.openWorldHint) == (
            False, False, True, False,
        ), name
    assert ann["env_sweep"].destructiveHint is True and ann["env_sweep"].openWorldHint is False
    # Codex's no-approval rule passes every tool except env_sweep.
    passes = {
        n: bool(a.readOnlyHint) or (a.destructiveHint is False and a.openWorldHint is False)
        for n, a in ann.items()
    }
    assert passes == {"env_ls": True, "env_up": True, "env_down": True, "env_sweep": False}


def test_the_context_parameter_is_not_part_of_any_tool_schema():
    for tool in m.mcp._tool_manager.list_tools():
        assert "ctx" not in tool.parameters.get("properties", {}), tool.name


@pytest.fixture
def no_env_session(monkeypatch):
    for name in service_mod.SESSION_ID_ENVS:
        monkeypatch.delenv(name, raising=False)


def test_a_codex_thread_id_in_meta_is_the_claim(mcp_service, no_env_session, devctl_home):
    """Codex sends the session id per call in `_meta.threadId`, equal to the
    SessionEnd hook's stdin `session_id` — so the claim must be recorded under
    it, or the hook's release finds nothing of its session to release."""

    async def up(client):
        return await client.call_tool("env_up", {"project": "webapp"}, meta={"threadId": "T1"})

    res = _payload(_over_the_protocol(up))
    assert res["ok"] is True
    assert [c["session"] for c in res["claims"]] == ["T1"]
    (lease_file,) = list(devctl_home.leases_dir.glob("*.json"))
    assert set(json.loads(lease_file.read_text())["claims"]) == {"T1"}

    async def down(client):
        return await client.call_tool("env_down", {"project": "webapp"}, meta={"threadId": "T1"})

    out = _payload(_over_the_protocol(down))
    assert out["ok"] is True and out["stopped"] is True


def test_the_turn_metadata_session_id_is_the_fallback(mcp_service, no_env_session):
    async def body(client):
        return await client.call_tool(
            "env_up", {"project": "webapp"},
            meta={"x-codex-turn-metadata": {"session_id": "S2", "turn_id": "t"}},
        )

    res = _payload(_over_the_protocol(body))
    assert [c["session"] for c in res["claims"]] == ["S2"]
    m.env_down("webapp", force=True)


def test_no_meta_falls_back_to_the_environment(mcp_service, monkeypatch, no_env_session):
    """Claude Code sends no session in `_meta`; its id is in the server's env."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cc-1")

    async def body(client):
        return await client.call_tool("env_up", {"project": "webapp"})

    res = _payload(_over_the_protocol(body))
    assert [c["session"] for c in res["claims"]] == ["cc-1"]
    m.env_down("webapp", force=True)


def test_another_threads_down_releases_only_its_own_claim(mcp_service, no_env_session):
    """Two Codex threads in one checkout share one server process: each call's
    `_meta` decides whose claim it is."""

    async def body(client):
        await client.call_tool("env_up", {"project": "webapp"}, meta={"threadId": "A"})
        await client.call_tool("env_up", {"project": "webapp"}, meta={"threadId": "B"})
        return await client.call_tool("env_down", {"project": "webapp"}, meta={"threadId": "A"})

    out = _payload(_over_the_protocol(body))
    assert out["ok"] is True and out["stopped"] is False
    assert [e["project"] for e in m.env_ls()["environments"]] == ["webapp"]
    m.env_down("webapp", force=True)


def test_env_sweep_echoes_the_meta_session(mcp_service, no_env_session):
    async def body(client):
        return await client.call_tool("env_sweep", {}, meta={"threadId": "T9"})

    res = _payload(_over_the_protocol(body))
    assert res["ok"] is True and res["session"] == "T9"


class _Req:
    def __init__(self, meta):
        self.meta = meta


class _Ctx:
    def __init__(self, meta=None, raises=False):
        self._meta, self._raises = meta, raises

    @property
    def request_context(self):
        if self._raises:
            raise ValueError("Context is not available outside of a request")
        return _Req(self._meta)


@pytest.mark.parametrize(
    ("ctx", "expected"),
    [
        (None, None),
        (_Ctx(raises=True), None),
        (_Ctx(None), None),
        (_Ctx({"threadId": "T"}), "T"),
        (_Ctx({"threadId": "T", "x-codex-turn-metadata": {"session_id": "S"}}), "T"),
        (_Ctx({"x-codex-turn-metadata": {"session_id": "S"}}), "S"),
        (_Ctx({"threadId": ""}), None),
        (_Ctx({"threadId": 7}), None),
        (_Ctx({"threadId": "bad\nid"}), None),
        (_Ctx({"threadId": "x" * 300}), None),
        (_Ctx({"x-codex-turn-metadata": "not an object"}), None),
        (_Ctx(RequestParams.Meta(threadId="M")), "M"),
        (_Ctx(RequestParams.Meta(progressToken=1)), None),
        (_Ctx(object()), None),
    ],
)
def test_meta_session_reads_every_shape_and_never_raises(ctx, expected):
    assert m.meta_session(ctx) == expected
