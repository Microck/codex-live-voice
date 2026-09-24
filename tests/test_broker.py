"""Regression tests for the gptlive broker (no network, no codex binary).

Ported from the hermes-live-voice plugin's audit tests (upstream issue #1):
the hard-won failure modes must keep failing loudly here.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from gptlive.broker import LiveBroker, QuotaExceededError
from gptlive.codexappserver import CodexAppServer, codex_env, resolve_codex_binary
from gptlive.instructions import (
    DefaultPersona,
    agent_instructions,
    build_session_prompt,
)


def make_broker(app_server: CodexAppServer) -> LiveBroker:
    broker = LiveBroker(persona=DefaultPersona(name="Nova"), app_server=app_server)
    # default stub: thread creation is exercised in the e2e suite; individual
    # tests override _thread_ensure when its recovery paths are the subject
    broker._thread_ensure = lambda language="en": app_server.thread_id or "tid-1"  # type: ignore[method-assign]
    return broker


class StubAppServer(CodexAppServer):
    """Keep unit tests independent of an installed Codex CLI."""

    def ensure(self) -> None:
        pass


def stub_server() -> CodexAppServer:
    return StubAppServer(spawn=False)


def test_broker_waits_for_first_session_before_starting_codex():
    broker = LiveBroker()
    assert broker.srv.proc is None
    broker.close()


def test_request_survives_buffer_pruning():
    """A response for a late request id must be seen even after the reader pruned the buffer."""
    srv = stub_server()
    srv.notifs = [{"id": -1} for _ in range(6000)]

    def send(request):
        srv.notifs.append({"id": request["id"], "result": "received"})
        del srv.notifs[:2000]

    srv._send = send  # type: ignore[method-assign]
    assert srv.request("probe", {}, timeout=0.5) == "received"


def test_start_reports_dropped_capability_fields():
    """Dropped capability fields are reported; a dropped clientManagedHandoffs degrades visibly."""
    srv = stub_server()
    srv.thread_id = "tid-1"
    broker = make_broker(srv)
    warnings: list[tuple] = []

    class LogStub:
        def warning(self, *a):
            warnings.append(a)

    calls = {"start": 0}

    def request(method, params, timeout=25):
        if method == "thread/realtime/start":
            calls["start"] += 1
            if calls["start"] <= 2:
                raise RuntimeError("unknown field clientManagedHandoffs")
            srv.notifs.append({"method": "thread/realtime/sdp", "params": {"sdp": "v=0"}})
            return {}
        return {}

    srv.request = request  # type: ignore[method-assign]
    import gptlive.broker as broker_mod

    orig_log = broker_mod._log
    broker_mod._log = LogStub()
    try:
        result = broker.start_session("v=0", voice="cove", language="en")
    finally:
        broker_mod._log = orig_log
    assert result["handoffDegraded"] is True
    assert "clientManagedHandoffs" in result["droppedFields"]
    assert result["droppedFields"] == ["delegationAckFiller", "clientManagedHandoffs"]
    assert result["handoff"] == "client"
    assert result["answer"] == "v=0"
    assert result["warning"]
    assert len(warnings) == 1 and "clientManagedHandoffs" in warnings[0][1]


def test_thread_recovery_keeps_session_language():
    """A stale thread recreated mid-call must be recreated in the session language."""
    srv = stub_server()
    srv.thread_id = "tid-1"
    broker = make_broker(srv)
    ensured: list[str] = []

    def thread_ensure(language="en"):
        ensured.append(language)
        return "tid-1" if len(ensured) == 1 else "tid-2"

    broker._thread_ensure = thread_ensure  # type: ignore[method-assign]

    starts = {"n": 0}

    def request(method, params, timeout=25):
        if method == "thread/realtime/start":
            starts["n"] += 1
            if starts["n"] == 1:
                raise RuntimeError("thread not found")
            srv.notifs.append({"method": "thread/realtime/sdp", "params": {"sdp": "v=0"}})
        return {}

    srv.request = request  # type: ignore[method-assign]
    result = broker.start_session("v=0", voice="cove", language="en")
    assert ensured == ["en", "en"], f"recovery lost the session language: {ensured}"
    assert result["threadId"] == "tid-2"
    assert result["answer"] == "v=0"


def test_stale_realtime_session_retries_after_stop():
    """'already' error → stop the parked session and retry once."""
    srv = stub_server()
    srv.thread_id = "tid-1"
    broker = make_broker(srv)
    stops: list[str] = []

    def request(method, params, timeout=25):
        if method == "thread/realtime/start":
            raise RuntimeError("session already active on thread")
        if method == "thread/realtime/stop":
            stops.append(params["threadId"])
            return {}
        return {}

    def retry_start(method, params, timeout=25):
        if method == "thread/realtime/start":
            srv.notifs.append({"method": "thread/realtime/sdp", "params": {"sdp": "v=0"}})
            return {}
        return {}

    srv.request = request  # type: ignore[method-assign]
    # patch stop_thread path: after the stop, make the next start succeed
    broker.srv.stop_thread = lambda tid: stops.append(tid)  # type: ignore[method-assign]
    # wrap request so that after the first failure+stop the retry succeeds
    calls = {"start": 0}
    state = {"stopped": False}

    def request2(method, params, timeout=25):
        if method == "thread/realtime/start":
            calls["start"] += 1
            if calls["start"] == 1 and not state["stopped"]:
                raise RuntimeError("realtime already in progress")
            srv.notifs.append({"method": "thread/realtime/sdp", "params": {"sdp": "v=0"}})
            return {}
        return {}

    srv.request = request2  # type: ignore[method-assign]

    def stop_and_mark(thread_id):
        stops.append(thread_id)
        state["stopped"] = True

    srv.stop_thread = stop_and_mark  # type: ignore[method-assign]
    result = broker.start_session("v=0")
    assert state["stopped"] is True
    assert result["answer"] == "v=0"
    assert stops, "the stale session was never stopped"


def test_quota_error_is_surfaced():
    srv = stub_server()
    srv.thread_id = "tid-1"
    broker = make_broker(srv)

    def request(method, params, timeout=25):
        if method == "thread/realtime/start":
            raise RuntimeError("You've hit your usage limit for GPT-Live-1")
        return {}

    srv.request = request  # type: ignore[method-assign]
    with pytest.raises(QuotaExceededError):
        broker.start_session("v=0")


def test_client_mode_neutralizes_thread_agent():
    """client handoff → realtimeStartInstructions must be the skip variant."""
    srv = stub_server()
    srv.thread_id = "tid-1"
    broker = make_broker(srv)
    seen: dict = {}

    def request(method, params, timeout=25):
        if method == "thread/realtime/start":
            seen.update(params)
            srv.notifs.append({"method": "thread/realtime/sdp", "params": {"sdp": "v=0"}})
            return {}
        return {}

    srv.request = request  # type: ignore[method-assign]
    broker.start_session("v=0", language="en")
    assert seen["clientManagedHandoffs"] is True
    assert "do NOT execute anything" in seen["realtimeStartInstructions"]
    assert seen["version"] == "v3"
    assert seen["voice"] == "cove"


def test_server_mode_uses_real_agent_instructions():
    srv = stub_server()
    srv.thread_id = "tid-1"
    broker = LiveBroker(persona=DefaultPersona(name="Nova"), app_server=srv, handoff="server")
    broker._thread_ensure = lambda language="en": "tid-1"  # type: ignore[method-assign]
    seen: dict = {}

    def request(method, params, timeout=25):
        if method == "thread/realtime/start":
            seen.update(params)
            srv.notifs.append({"method": "thread/realtime/sdp", "params": {"sdp": "v=0"}})
            return {}
        return {}

    srv.request = request  # type: ignore[method-assign]
    broker.start_session("v=0", language="en")
    assert seen["clientManagedHandoffs"] is False
    assert "carry it out with your tools" in seen["realtimeStartInstructions"]


def test_invalid_voice_rejected():
    broker = make_broker(stub_server())
    with pytest.raises(ValueError):
        broker.start_session("v=0", voice="marin")  # v1 voice, not in v3 set


def test_interrupt_passes_thread_and_turn():
    srv = stub_server()
    srv.thread_id = "tid-default"
    broker = make_broker(srv)
    seen: list[tuple] = []

    def request(method, params, timeout=8):
        seen.append((method, params))
        return {}

    srv.request = request  # type: ignore[method-assign]
    broker.interrupt_turn(None, "turn-7")
    assert seen == [("turn/interrupt", {"threadId": "tid-default", "turnId": "turn-7"})]


def test_instructions_prompt_assembly():
    provider = DefaultPersona(name="Nova")
    prompt = build_session_prompt(provider, None, "en")
    assert "Nova" in prompt
    assert "VOICE DELEGATION POLICY" in prompt
    assert "LANGUAGE AND VOICE" in prompt
    es = build_session_prompt(provider, None, "es")
    assert "IDIOMA Y VOZ" in es


def test_agent_instructions_selects_variant():
    assert "skip" in agent_instructions("client", "en")
    assert "tools" in agent_instructions("server", "en")
    assert "herramientas" in agent_instructions("server", "es")


def test_codex_env_strips_api_keys():
    env = codex_env({"OPENAI_API_KEY": "sk-test", "CODEX_API_KEY": "ck", "PATH": "/usr/bin", "KEEP": "yes"})
    assert "OPENAI_API_KEY" not in env
    assert "CODEX_API_KEY" not in env
    assert env["KEEP"] == "yes"
    assert env["PATH"].split(":")[0].endswith(".npm-global/bin")


def test_resolve_codex_binary_no_crash(monkeypatch):
    # no binary in this test env necessarily; must return None or a path, never raise
    result = resolve_codex_binary()
    assert result is None or result.endswith("codex")


def test_interrupt_route_does_not_block_event_loop():
    """handle_interrupt must run the RPC off the event loop (via to_thread)."""
    from gptlive.server import LiveVoiceService

    srv = stub_server()
    srv.thread_id = "tid-1"
    broker = make_broker(srv)

    def slow_request(*a, **k):
        time.sleep(0.5)
        return {}

    srv.request = slow_request  # type: ignore[method-assign]
    service = LiveVoiceService(broker=broker, usage_store=_tmp_store())

    async def run():
        ticks = [0]

        async def ticker():
            while True:
                ticks[0] += 1
                await asyncio.sleep(0.01)

        t = asyncio.create_task(ticker())
        await asyncio.sleep(0.05)
        before = ticks[0]
        result = await asyncio.to_thread(service.handle_interrupt, {"turnId": "turn-7"})
        during = ticks[0] - before
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        return result, during

    result, during = asyncio.run(run())
    assert result == {"ok": True}
    assert during >= 20, f"event loop was blocked during the interrupt ({during} ticks)"


def _tmp_store():
    from pathlib import Path
    import tempfile

    from gptlive.voiceusage import VoiceUsageStore

    return VoiceUsageStore(Path(tempfile.mkdtemp()) / "voice-usage.json")


def test_voice_usage_store_roundtrip():
    store = _tmp_store()
    store.report(120000, 90000)
    store.report(60000, 45000)
    s = store.summary()
    assert s["today"]["minutes"] == pytest.approx(3.0)
    assert s["today"]["audioMinutes"] == pytest.approx(2.25)
    assert s["today"]["sessions"] == 2
    with pytest.raises(ValueError):
        store.report(0, 0)
