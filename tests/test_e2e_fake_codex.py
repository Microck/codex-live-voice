"""End-to-end smoke: a fake `codex` binary speaks the app-server JSON-RPC
protocol; a full session mint must succeed through LiveBroker and the FastAPI
layer (TestClient), without any real codex/OpenAI involvement.

Also proves the env contract: the spawned child never sees OPENAI_API_KEY.
"""
from __future__ import annotations

import json
import os
import stat
import textwrap
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from gptlive.codexappserver import CodexAppServer  # noqa: E402
from gptlive.instructions import DefaultPersona  # noqa: E402
from gptlive.server import LiveVoiceService, mount  # noqa: E402


FAKE_CODEX = textwrap.dedent(
    """
    #!/usr/bin/env python3
    import json, os, sys

    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()

    # contract: the broker strips API keys so subscription auth is used
    assert "OPENAI_API_KEY" not in os.environ, "OPENAI_API_KEY leaked into app-server env"
    assert "CODEX_API_KEY" not in os.environ, "CODEX_API_KEY leaked into app-server env"

    initialized = False
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        msg = json.loads(raw)
        method, params, rid = msg.get("method"), msg.get("params") or {}, msg.get("id")
        if method == "initialize":
            initialized = True
            send({"id": rid, "result": {"serverInfo": {"name": "fake-app-server", "version": "0.154"}}})
        elif method == "thread/start":
            assert initialized
            send({"id": rid, "result": {"thread": {"id": "fake-thread-1"}}})
        elif method == "thread/realtime/start":
            send({"method": "thread/realtime/sdp", "params": {"sdp": "v=0\\r\\no=fake-answer"}})
            send({"method": "thread/realtime/started", "params": {"realtimeSessionId": "sess-1"}})
            send({"id": rid, "result": {}})
        elif method == "thread/realtime/stop":
            send({"id": rid, "result": {}})
        elif method == "turn/interrupt":
            send({"id": rid, "result": {}})
        else:
            send({"id": rid, "error": {"message": f"unknown method {method}"}})
    """
)


@pytest.fixture()
def fake_codex(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    binary = bin_dir / "codex"
    binary.write_text(FAKE_CODEX.lstrip("\n"))  # shebang must be byte 0 or exec() fails
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-never-leak")  # must be stripped by codex_env
    return binary


@pytest.fixture()
def app_with_fake_codex(fake_codex, tmp_path, monkeypatch):
    # force the broker to use the fake binary regardless of which/PATH
    from gptlive.broker import LiveBroker

    srv = CodexAppServer(binary=str(fake_codex))
    broker = LiveBroker(persona=DefaultPersona(name="Nova"), app_server=srv)
    app = FastAPI()
    service = mount(app, prefix="/api/voice", data_dir=str(tmp_path))
    service.broker = broker
    return app, service


def test_full_session_mint_through_http(app_with_fake_codex, tmp_path):
    app, service = app_with_fake_codex
    client = TestClient(app)

    status = client.get("/api/voice/status").json()
    assert status["ok"] is True
    assert "cove" in status["voices"]

    resp = client.post("/api/voice/session", json={"offer": "v=0\r\no=fake-offer", "voice": "cove", "language": "en"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["answer"].startswith("v=0")
    assert "fake-answer" in data["answer"]
    assert data["threadId"] == "fake-thread-1"
    assert data["handoff"] == "client"
    assert data["engine"] == "codex"

    # interrupt + stop round-trips
    assert client.post("/api/voice/interrupt", json={"threadId": "fake-thread-1", "turnId": "turn-1"}).json() == {"ok": True}
    assert client.post("/api/voice/stop", json={"threadId": "fake-thread-1"}).json() == {"ok": True}

    # usage reporting flows through the mounted routes
    assert client.post("/api/voice/usage/report", json={"durationMs": 60000, "audioMs": 30000}).json() == {"ok": True}
    usage = client.get("/api/voice/usage").json()
    assert usage["today"]["sessions"] == 1
    service.broker.close()


def test_session_rejects_missing_offer(app_with_fake_codex):
    app, _ = app_with_fake_codex
    client = TestClient(app)
    resp = client.post("/api/voice/session", json={})
    assert resp.status_code == 400
    service_close(app)


def service_close(app):
    pass


def test_env_stripping_verified_by_fake_server(fake_codex, tmp_path):
    """The fake codex asserts on startup that API keys are absent — reaching a
    successful mint here proves the strip happened in the real spawn path."""
    srv = CodexAppServer(binary=str(fake_codex))
    from gptlive.broker import LiveBroker

    broker = LiveBroker(persona=DefaultPersona(name="Nova"), app_server=srv)
    result = broker.start_session("v=0")
    assert result["answer"]
    broker.close()
