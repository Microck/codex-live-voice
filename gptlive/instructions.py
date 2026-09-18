"""gptlive.instructions — persona / session-instruction builders.

The voice model's behavior is shaped entirely by the `prompt` handed to
`thread/realtime/start`. Two layers:

1. A **persona provider**: the host application supplies identity sections
   (who the assistant is). Ships with a default standalone persona; override
   with `PersonaProvider` to inject your app's identity.
2. Fixed engine layers extracted from production: language mirroring, live
   voice etiquette, and the delegation policy (without it the model delegates
   every fragment of conversation to the backend and floods your task lane).
"""
from __future__ import annotations

from typing import Protocol

LANGUAGE_DIRECTIVES = {
    "es": (
        "\n\nIDIOMA Y VOZ: Habla en el idioma en que te habla el usuario — si te habla en español, responde en "
        "español natural; si te habla en inglés, en inglés. Nunca mezcles idiomas ni respondas en "
        "inglés cuando te hablan en español. Tu voz debe sonar como la de un hablante nativo del idioma que "
        "estés usando."
    ),
    "en": (
        "\n\nLANGUAGE AND VOICE: Speak the language the user speaks to you — if they speak Spanish, reply in "
        "Spanish; if they speak English, reply in English. Never mix languages or reply in "
        "English when the user speaks Spanish. Your voice should sound like a native speaker of the language "
        "you are using."
    ),
}

VOICE_ETIQUETTE = {
    "es": (
        "\n\nVOZ: hablas con el usuario en una sesión de voz en vivo. Respuestas breves, naturales y al grano."
    ),
    "en": (
        "\n\nVOICE: You are speaking with the user in a live voice session. Keep replies brief, natural, and to the point."
    ),
}

VOICE_POLICY = (
    "\n\nVOICE DELEGATION POLICY (live call):\n"
    "- Delegate to the chat/backend ONLY when the user asks you to DO something (check, review, find, run, make, fix, send, remember) or when answering needs real facts/actions from the backend.\n"
    "- Do NOT delegate greetings, small talk, acknowledgements, filler, thinking out loud, fragments, or repeats of something already answered (e.g. 'sure', 'ok', 'hello', 'can you hear me?'). Answer those yourself, briefly, or stay quiet.\n"
    "- If the request is unclear, ask ONE short clarifying question yourself instead of delegating.\n"
    "- While a task is running: say ONE brief line in the user's language ('sure, I will check') and WAIT silently; do not guess results, do not delegate again in the meantime; if the user speaks, tell them you are still on it.\n"
    "- When the result arrives, read the key facts back in one or two short sentences."
)

AGENT_INSTR = {
    "es": (
        "Estás conectado a una sesión de voz en vivo con el usuario. Cuando la sesión de voz te delegue una "
        "petición, actúala con tus herramientas y responde conciso: tu texto se leerá en voz alta."
    ),
    "en": (
        "You are connected to a live voice session with the user. When the voice session delegates a request "
        "to you, carry it out with your tools and reply concisely: your text will be read aloud."
    ),
}

# Client mode: the CLIENT executes delegated requests. The Codex core still
# routes every delegation into the thread agent as a phantom fallback — if that
# agent did real tool work it would burn the ChatGPT plan and duplicate the
# client's work. This instruction neutralizes it: reply 'skip', no tools.
AGENT_INSTR_SKIP = {
    "es": (
        "Estás conectado a una sesión de voz en vivo, pero es el CLIENTE (la app) quien ejecuta las "
        "peticiones de esa sesión. Si recibes un mensaje <realtime_delegation>, NO ejecutes nada, NO uses "
        "herramientas y NO leas archivos: responde únicamente la palabra: skip"
    ),
    "en": (
        "You are connected to a live voice session, but the CLIENT (the app) executes the requests from that "
        "session. If you receive a <realtime_delegation> message, do NOT execute anything, do NOT use tools, "
        "and do NOT read files: reply with only the word: skip"
    ),
}


class PersonaProvider(Protocol):
    """Host applications supply identity; the engine stays app-agnostic."""

    def persona(self, profile: str | None, language: str = "en") -> str:
        """Return the base persona prompt for the requested profile (or default)."""
        ...

    def display_name(self, profile: str | None) -> str:
        ...


class DefaultPersona:
    """Standalone persona for hosts with no identity system."""

    def __init__(self, name: str = "Assistant", persona_text: str | None = None) -> None:
        self.name = name
        self.persona_text = persona_text

    def persona(self, profile: str | None, language: str = "en") -> str:
        lang = "en" if language == "en" else "es"
        if self.persona_text:
            return self.persona_text.strip()
        return (
            f"You are {self.name}, a helpful live voice assistant. "
            f"Answer as {self.name} — never as a generic model." if lang == "en" else
            f"Eres {self.name}, un asistente de voz en vivo. "
            f"Responde como {self.name} — nunca como un modelo genérico."
        )

    def display_name(self, profile: str | None) -> str:
        return self.name


def build_session_prompt(
    provider: PersonaProvider,
    profile: str | None,
    language: str = "en",
) -> str:
    """Full voice-model prompt: persona + language directive + etiquette + delegation policy."""
    lang = "en" if language == "en" else "es"
    name = provider.display_name(profile)
    base = provider.persona(profile, lang)
    if "You are Hermes, speaking live" in base:  # legacy upstream phrasing guard
        base = base.replace("You are Hermes, speaking live", f"You are {name}, speaking live", 1)
    return (
        base
        + LANGUAGE_DIRECTIVES[lang]
        + VOICE_ETIQUETTE[lang]
        + f" IDENTITY: You are {name}; if asked who you are, answer as {name} in that role — never as a generic assistant."
        + VOICE_POLICY
    )


def agent_instructions(handoff: str, language: str = "en") -> str:
    """realtimeStartInstructions for the thread agent (executor vs neutralized)."""
    lang = "en" if language == "en" else "es"
    return (AGENT_INSTR if handoff == "server" else AGENT_INSTR_SKIP)[lang]
