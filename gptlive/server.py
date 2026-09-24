"""gptlive.server — drop-in FastAPI router exposing the broker over HTTP.

Mount into any FastAPI/Starlette app:

    from fastapi import FastAPI
    from gptlive.server import mount

    app = FastAPI()
    mount(app, access_dependency=require_user, prefix="/api/voice")

Routes (all JSON):
    GET  /status                  → {ok, codexFound, loggedIn, voices}
    POST /session                 → {offer(sdp), voice?, language?, profile?} → {answer, threadId, ...}
    POST /interrupt               → {threadId, turnId} → {ok}
    POST /stop                    → {threadId?} → {ok}
    GET  /auth/status             → codex login state
    POST /auth/login              → {url, code} device-code login
    POST /auth/login/cancel       → cancel pending login
    POST /auth/logout             → remove auth.json (backed up)
    GET  /usage                   → local voice-minute metering
    GET  /plan-usage              → ChatGPT plan buckets (needs signed-in codex)

The browser half uses `client/gpt-live-client.js`, which only needs
`POST /session`, `POST /interrupt`, `POST /stop` (endpoints configurable).
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from .broker import LiveBroker, QuotaExceededError
from .codexappserver import resolve_codex_binary, V3_VOICES
from .codexauth import CodexAuthManager, codex_auth_path, plan_usage, read_account_id
from .voiceusage import VoiceUsageStore

_log = logging.getLogger("gptlive.server")

try:
    from fastapi import APIRouter, Depends, HTTPException, Request
except ImportError:  # pragma: no cover
    APIRouter = None
    Depends = None
    HTTPException = None
    Request = None


def _json_body(request) -> dict:
    async def _read():
        try:
            body = await request.json()
            return body if isinstance(body, dict) else {}
        except Exception:
            return {}
    return _read


class LiveVoiceService:
    """Bundles broker + auth + usage into one service object with route factories."""

    def __init__(
        self,
        broker: LiveBroker | None = None,
        data_dir: str | None = None,
        usage_store: VoiceUsageStore | None = None,
        auth_manager: CodexAuthManager | None = None,
    ) -> None:
        self.broker = broker or LiveBroker()
        self.usage = usage_store or VoiceUsageStore(
            (data_dir or "./data") + "/voice-usage.json"
        )
        self.auth = auth_manager or CodexAuthManager()

    # -- pure-python handlers (usable without FastAPI, e.g. other frameworks) --

    def handle_status(self) -> dict:
        return {
            "ok": True,
            "codexFound": resolve_codex_binary() is not None,
            "loggedIn": self.auth.logged_in(),
            "voices": list(V3_VOICES),
            "handoff": self.broker.handoff,
        }

    def handle_session(self, body: dict) -> dict:
        offer = str(body.get("offer") or "")
        if not offer:
            raise ValueError("missing SDP offer")
        voice = str(body.get("voice") or "cove")
        language = str(body.get("language") or "en")
        profile = str(body.get("profile") or "").strip() or None
        try:
            result = self.broker.start_session(offer, voice=voice, language=language, profile=profile)
        except QuotaExceededError:
            raise
        except Exception as exc:
            raise RuntimeError(str(exc)[:400]) from exc
        return {"ok": True, **result}

    def handle_interrupt(self, body: dict) -> dict:
        turn_id = str(body.get("turnId") or "").strip()
        thread_id = str(body.get("threadId") or "").strip()
        if not turn_id:
            return {"ok": False, "error": "turnId required"}
        self.broker.interrupt_turn(thread_id or None, turn_id)
        return {"ok": True}

    def handle_stop(self, body: dict) -> dict:
        thread_id = str(body.get("threadId") or "").strip() or None
        self.broker.stop_session(thread_id)
        return {"ok": True}

    def handle_auth_status(self) -> dict:
        st = self.auth.status()
        return {"ok": True, "login": st}

    def handle_plan_usage(self) -> dict:
        try:
            token = _read_access_token()
            if not token:
                return {"ok": False, "error": "not signed in (no access token in auth.json)"}
            return plan_usage(token, read_account_id())
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:200]}


def _read_access_token() -> str:
    try:
        data = json.loads(codex_auth_path().read_text(encoding="utf-8"))
        return str((data.get("tokens") or {}).get("access_token") or "")
    except Exception:
        return ""


def make_router(service: LiveVoiceService, *, access_dependency: Callable[..., Any]):
    """Build routes behind an app-owned FastAPI access dependency."""
    if APIRouter is None:
        raise ImportError("fastapi is required for make_router(); pip install codex-live-voice[server]")
    if not callable(access_dependency):
        raise TypeError("access_dependency must be a FastAPI dependency")
    router = APIRouter(dependencies=[Depends(access_dependency)])

    @router.get("/status")
    async def status() -> dict:
        return service.handle_status()

    @router.post("/session")
    async def session(request: Request) -> dict:
        body = await _json_body(request)()
        import asyncio
        try:
            return await asyncio.to_thread(service.handle_session, body)
        except QuotaExceededError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)[:400]) from exc

    @router.post("/interrupt")
    async def interrupt(request: Request) -> dict:
        body = await _json_body(request)()
        import asyncio
        return await asyncio.to_thread(service.handle_interrupt, body)

    @router.post("/stop")
    async def stop(request: Request) -> dict:
        body = await _json_body(request)()
        import asyncio
        return await asyncio.to_thread(service.handle_stop, body)

    @router.get("/auth/status")
    async def auth_status() -> dict:
        return service.handle_auth_status()

    @router.post("/auth/login")
    async def auth_login() -> dict:
        import asyncio
        return await asyncio.to_thread(service.auth.start_login)

    @router.post("/auth/login/cancel")
    async def auth_login_cancel() -> dict:
        import asyncio
        await asyncio.to_thread(service.auth.cancel_login)
        return {"ok": True}

    @router.post("/auth/logout")
    async def auth_logout() -> dict:
        import asyncio
        return await asyncio.to_thread(service.auth.logout)

    @router.post("/usage/report")
    async def usage_report(request: Request) -> dict:
        body = await _json_body(request)()
        import asyncio

        def _do():
            service.usage.report(int(body.get("durationMs") or 0), int(body.get("audioMs") or 0))
            return {"ok": True}

        try:
            return await asyncio.to_thread(_do)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/usage")
    async def usage() -> dict:
        import asyncio
        return await asyncio.to_thread(service.usage.summary)

    @router.get("/plan-usage")
    async def plan_usage_route() -> dict:
        import asyncio
        return await asyncio.to_thread(service.handle_plan_usage)

    return router


def mount(app, *, access_dependency: Callable[..., Any], service: LiveVoiceService | None = None,
          prefix: str = "/api/voice",
          cors_origins: list[str] | None = None, data_dir: str | None = None):
    """Mount the voice routes on a FastAPI app; returns the service.

    access_dependency: app-owned FastAPI dependency that denies unauthorized
    requests. It covers session creation, login/logout, and usage routes.
    cors_origins: list of origins allowed to call these endpoints from a
    browser (e.g. ["http://localhost:5173"]). None disables CORS handling.
    data_dir: where voice-usage.json is stored (defaults to ./data — pass an
    app-owned path in production).
    """
    service = service or LiveVoiceService(data_dir=data_dir)
    app.include_router(make_router(service, access_dependency=access_dependency), prefix=prefix)
    if cors_origins:
        try:
            from fastapi.middleware.cors import CORSMiddleware
            app.add_middleware(
                CORSMiddleware,
                allow_origins=cors_origins,
                allow_methods=["*"],
                allow_headers=["*"],
            )
        except ImportError:
            _log.warning("cors_origins given but fastapi CORSMiddleware unavailable")
    return service
