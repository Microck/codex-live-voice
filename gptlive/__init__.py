"""gpt-live-voice — standalone GPT-Live-1 realtime voice engine.

Extracted from the hermes-live-voice plugin so any web app can deploy
full-duplex voice backed by a ChatGPT/Codex subscription:

- `gptlive.broker.LiveBroker` — session lifecycle over `codex app-server`.
- `gptlive.server.mount` — optional FastAPI routes for browser clients.
- `client/gpt-live-client.js` — zero-dependency browser client (WebRTC + v3).

No API key on the voice lane: the local `codex login` subscription negotiates
the session; audio flows browser ↔ OpenAI directly over WebRTC.
"""
from .broker import LiveBroker, QuotaExceededError
from .codexappserver import CodexAppServer, CodexBinaryNotFoundError, V3_VOICES, DEFAULT_VOICE
from .codexauth import CodexAuthManager, codex_auth_path, plan_usage, read_account_id
from .instructions import (
    AGENT_INSTR,
    AGENT_INSTR_SKIP,
    DefaultPersona,
    PersonaProvider,
    VOICE_POLICY,
    agent_instructions,
    build_session_prompt,
)
from .voiceusage import VoiceUsageStore

__version__ = "1.0.0"

__all__ = [
    "LiveBroker",
    "QuotaExceededError",
    "CodexAppServer",
    "CodexBinaryNotFoundError",
    "CodexAuthManager",
    "VoiceUsageStore",
    "PersonaProvider",
    "DefaultPersona",
    "V3_VOICES",
    "DEFAULT_VOICE",
    "AGENT_INSTR",
    "AGENT_INSTR_SKIP",
    "VOICE_POLICY",
    "agent_instructions",
    "build_session_prompt",
    "codex_auth_path",
    "plan_usage",
    "read_account_id",
    "__version__",
]


def __getattr__(name):  # lazy: fastapi is optional
    if name in ("LiveVoiceService", "make_router", "mount"):
        from . import server as _server
        return getattr(_server, name)
    raise AttributeError(name)
