"""gptlive.broker — session lifecycle on top of CodexAppServer.

Flow: the browser sends a WebRTC SDP offer; the broker spawns/uses the
app-server, opens a thread, starts a realtime session (v3 → gpt-live-1-codex)
and returns the SDP answer. Audio then flows browser ↔ OpenAI directly over
WebRTC; the broker only signals.

Retry/recovery logic ported from the talk-desktop plugin (production-hardened):

- `unknown field` → the installed app-server lacks a capability; drop that
  param group and retry, reporting what was dropped (clientManagedHandoffs
  being dropped degrades handoff visibly).
- `thread not found` → the app-server restarted; recreate the thread, keeping
  the session's language (recreating with a bare default re-registered the
  session in the wrong language and forced a paid new thread on the next call).
- `usage limit` → surfaced as QuotaExceededError.
- `already` → a stale realtime session is parked on the thread; stop it and
  retry once.
"""
from __future__ import annotations

import logging
import threading
import time

from .codexappserver import (
    DEFAULT_VOICE,
    CodexAppServer,
    V3_VOICES,
)
from .instructions import (
    PersonaProvider,
    DefaultPersona,
    agent_instructions,
    build_session_prompt,
)

_log = logging.getLogger("gptlive")

# Drop order for "unknown field" retries: one group per attempt, fixed order.
_DROP_GROUPS = (("delegationAckFiller",), ("clientManagedHandoffs",), ("realtimeStartInstructions", "prompt"))


class QuotaExceededError(RuntimeError):
    pass


class LiveBroker:
    """Mints and stops GPT-Live-1 realtime sessions over one app-server lane."""

    def __init__(
        self,
        persona: PersonaProvider | None = None,
        *,
        app_server: CodexAppServer | None = None,
        agent_model: str | None = None,
        handoff: str = "client",
        request_timeout: float = 30.0,
    ) -> None:
        self.persona: PersonaProvider = persona or DefaultPersona()
        self._owned = app_server is None
        self.srv = app_server or CodexAppServer()
        self.request_timeout = request_timeout
        self.agent_model = agent_model
        self.handoff = "server" if str(handoff).lower() == "server" else "client"
        self._lock = threading.Lock()

    # ── thread management ────────────────────────────────────────────────

    def _thread_ensure(self, language: str = "en") -> str:
        language = "en" if language == "en" else "es"
        want = self.agent_model
        tid = self.srv.thread_id
        if tid and self.srv.thread_model == want and self.srv.thread_language == language:
            return tid
        body = {"cwd": "/", "modelProvider": "openai"}
        if want:
            body["model"] = want
        th = self.srv.request("thread/start", body, timeout=self.request_timeout)
        tid = ((th or {}).get("thread") or {}).get("id")
        if not tid:
            raise RuntimeError("codex: no se pudo crear el thread")
        self.srv.thread_id = tid
        self.srv.thread_model = want
        self.srv.thread_language = language
        return tid

    # ── session start ────────────────────────────────────────────────────

    def start_session(self, offer: str, voice: str | None = None, language: str = "en",
                      profile: str | None = None) -> dict:
        """Negotiate a realtime session for a client SDP offer.

        Returns {answer, threadId, realtimeSessionId?, version, engine, handoff,
        droppedFields?, handoffDegraded?, warning?}.
        """
        voice = (voice or DEFAULT_VOICE).strip().lower()
        if voice not in V3_VOICES:
            raise ValueError(f"voice '{voice}' no disponible (v3: {', '.join(V3_VOICES)})")
        with self._lock:
            return self._start_locked(offer, voice, language, profile)

    def _start_locked(self, offer: str, voice: str, language: str, profile: str | None) -> dict:
        self.srv.ensure()
        tid = self._thread_ensure(language)
        persona = build_session_prompt(self.persona, profile, language)
        client_managed = self.handoff != "server"
        params = {
            "threadId": tid,
            "transport": {"type": "webrtc", "sdp": offer},
            "outputModality": "audio",
            "version": "v3",
            "voice": voice,
            "prompt": persona,
            "realtimeStartInstructions": agent_instructions("server" if not client_managed else "client", language),
            "clientManagedHandoffs": client_managed,
            "delegationAckFiller": True,
        }
        dropped: list[str] = []
        start = len(self.srv.notifs)
        gi = 0
        while True:
            try:
                self.srv.request("thread/realtime/start", params, timeout=25)
                break
            except RuntimeError as exc:
                low = str(exc).lower()
                if "unknown field" in low and gi < len(_DROP_GROUPS):
                    for f in _DROP_GROUPS[gi]:
                        if f in params:
                            params.pop(f)
                            dropped.append(f)
                    gi += 1
                    start = len(self.srv.notifs)
                    continue
                if "thread" in low and ("not found" in low or "no such" in low or "not loaded" in low or "missing" in low):
                    # stale thread: recreate (keeping language/model) and retry once
                    self.srv.thread_id = None
                    self.srv.thread_model = None
                    tid2 = self._thread_ensure(language)
                    params["threadId"] = tid2
                    start = len(self.srv.notifs)
                    self.srv.request("thread/realtime/start", params, timeout=25)
                    break
                if "usage limit" in low or "hit your usage" in low:
                    raise QuotaExceededError(
                        "the ChatGPT plan hit its limit (Live Voice shares that quota); "
                        "wait for the weekly reset"
                    ) from exc
                if "already" in low:
                    # stale realtime session parked on the thread: stop and retry once
                    self.srv.stop_thread(params.get("threadId"))
                    start = len(self.srv.notifs)
                    self.srv.request("thread/realtime/start", params, timeout=25)
                    break
                raise
        answer = None
        rsid = None
        t0 = time.time()
        while time.time() - t0 < 45:
            for m in self.srv.notifications_from(start):
                meth = m.get("method") or ""
                if meth == "thread/realtime/sdp":
                    answer = (m.get("params") or {}).get("sdp") or answer
                elif meth == "thread/realtime/started":
                    rsid = (m.get("params") or {}).get("realtimeSessionId") or rsid
                elif meth == "thread/realtime/error":
                    raise RuntimeError("codex live: " + str((m.get("params") or {}).get("message"))[:300])
            if answer:
                break
            time.sleep(0.15)
        if not answer:
            raise TimeoutError("codex live: sin SDP de respuesta")
        result = {
            "answer": answer,
            "threadId": params.get("threadId") or tid,
            "version": "v3",
            "engine": "codex",
            "voice": voice,
            "handoff": "client" if client_managed else "server",
        }
        if rsid:
            result["realtimeSessionId"] = rsid
        if dropped:
            _log.warning("codex live: app-server lacks %s (dropped)", ", ".join(dropped))
            result["droppedFields"] = dropped
            if "clientManagedHandoffs" in dropped and client_managed:
                result["handoffDegraded"] = True
                result["warning"] = (
                    "app-server does not support clientManagedHandoffs: voice delegations may execute "
                    "on the thread agent (ChatGPT lane) instead of your client"
                )
        return result

    # ── session stop ─────────────────────────────────────────────────────

    def stop_session(self, thread_id: str | None) -> None:
        with self._lock:
            self.srv.stop_thread(thread_id)

    def interrupt_turn(self, thread_id: str | None, turn_id: str) -> None:
        """Barge-in support: cut the current voice turn."""
        tid = thread_id or self.srv.thread_id
        if not tid or not turn_id:
            return
        self.srv.request("turn/interrupt", {"threadId": tid, "turnId": turn_id}, timeout=8)

    def close(self) -> None:
        if self._owned:
            self.srv.close()
