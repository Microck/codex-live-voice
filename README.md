# gpt-live-voice 🎙️

Standalone **GPT-Live-1 full-duplex voice engine** — deploy live voice into any web app.

Runs `gpt-live-1-codex` realtime sessions on a **ChatGPT/Codex subscription** through the local
[Codex CLI](https://github.com/openai/codex) app-server: no API key on the voice lane. Audio flows
**browser ↔ OpenAI directly over WebRTC**; the broker only signals (SDP negotiation + session
lifecycle). Extracted from the production [hermes-live-voice](https://github.com/Synero/hermes-live-voice)
plugin so it can be embedded in any application.

## Architecture

```
┌──────────────┐  SDP offer   ┌────────────────────┐   stdio JSON-RPC   ┌──────────────────┐
│  Browser      │ ──────────▶ │  gptlive broker     │ ─────────────────▶ │ codex app-server │
│  (your app)   │ ◀────────── │  (FastAPI or your   │                    │  (subscription)  │
│  WebRTC + v3  │  SDP answer │   own framework)    │                    └────────┬─────────┘
│  events       │             └────────────────────┘                             │
└──────┬───────┘                                                                 │
       │  audio (WebRTC media, direct)                                           │
       └──────────────────────────────▶ OpenAI realtime ◀────────────────────────┘
```

Two halves, independently usable:

- **`gptlive/`** (Python, stdlib-only core) — spawns/locks `codex app-server`, opens threads,
  negotiates `thread/realtime/start` (protocol v3 → `gpt-live-1-codex`), with the
  production-hardened retry/recovery paths (unknown-field drops, stale-thread recreation,
  quota surfacing, stale-session stop+retry). Optional FastAPI adapter (`gptlive.server`).
- **`client/gpt-live-client.js`** (zero-dependency ESM) — WebRTC session, `oai-events`
  datachannel, live transcript with v3 bubble semantics (turns rotate; `turn.done` is never
  trusted to shrink text), client-managed delegation queue, mute, barge-in, usage metering.
  Framework-agnostic: give it callbacks; render in React/Vue/Svelte/plain DOM yourself.

## Requirements

- **Codex CLI ≥ 0.154** on the broker host, logged in: `codex login` (device auth).
- Python 3.11+ for the broker. The browser half needs no build step.

## Quick start

```bash
pip install -e ".[server]"       # fastapi only for the example server
cd examples && bash run.sh       # http://localhost:8000
```

Open the page, allow the mic, **Start call**. First run on a fresh machine: click **Sign in** on
the auth card (or run `codex login` on the broker host).

### Embed the server into your own app

```python
from fastapi import FastAPI
from gptlive import mount

app = FastAPI()
mount(app, prefix="/api/voice", cors_origins=["https://yourapp.example"])
```

Or use only the broker (any framework — Flask, Litestar, Rails proxy, …):

```python
from gptlive import LiveBroker, DefaultPersona

broker = LiveBroker(persona=DefaultPersona(name="Nova"))
result = broker.start_session(sdp_offer, voice="cove", language="en")
# result["answer"] → SDP answer for the browser; result["threadId"] for stop/interrupt
broker.interrupt_turn(thread_id, turn_id)   # barge-in
broker.stop_session(thread_id)              # hang up
```

### Embed the client into your own frontend

```html
<script type="module">
  import { LiveVoice } from '/client/gpt-live-client.js'

  const voice = new LiveVoice({
    brokerBase: '/api/voice',
    voice: 'cove',                       // V3_VOICES: cove, juniper, maple, spruce, ember, vale, breeze, arbor, sol
    onStatus: s => console.log('rtc:', s),
    onTranscript: items => renderBubbles(items),   // [{role: user|bot|tool|sys, text, done}]
    onUsage: ({ audioMs }) => updateMinutes(audioMs),
    onDelegation: async req => runInMyAgent(req),  // optional: execute voice-delegated tasks in YOUR app
  })
  await voice.start({ language: 'en' })
  // voice.toggleMute(); voice.sendText('hello'); await voice.close()
</script>
```

### HTTP surface (when mounted)

| method | path | body | returns |
|---|---|---|---|
| GET | `/status` | — | codexFound, loggedIn, voices, handoff |
| POST | `/session` | `{offer, voice?, language?, profile?}` | `{answer, threadId, handoff, droppedFields?, handoffDegraded?, warning?}` |
| POST | `/interrupt` | `{threadId?, turnId}` | `{ok}` (barge-in) |
| POST | `/stop` | `{threadId?}` | `{ok}` |
| GET | `/auth/status` | — | codex device-login state |
| POST | `/auth/login` | — | `{url, code}` → open URL, type code |
| POST | `/auth/login/cancel` | — | cancels; restores auth.json backup |
| POST | `/auth/logout` | — | removes auth.json (backed up) |
| GET | `/usage` | — | local voice minutes (5h/24h/week/today) |
| GET | `/plan-usage` | — | ChatGPT plan buckets (primary/secondary windows) |

### Persona & delegation (app integration points)

- **Persona** — pass `persona=` implementing `PersonaProvider` (`persona(profile, language)` +
  `display_name(profile)`) to `LiveBroker` to speak with your product's identity. The prompt is
  assembled in `gptlive.instructions`: persona + language mirroring + live etiquette +
  **delegation policy** (without it the model delegates every fragment and floods your task lane).
- **Delegation** — with `handoff="client"` (default) the voice model marks tasks as
  `delegation.created`; the *browser client* executes them via your `onDelegation` callback and
  feeds results back with `delegation.context.append`. The thread agent is explicitly
  neutralized ("reply skip, no tools") so the ChatGPT plan never pays for phantom background
  tool work. `handoff="server"` instead lets the Codex thread agent execute tasks.

## Configuration

| env / setting | meaning |
|---|---|
| `CODEX_HOME` | where `auth.json` lives (default `~/.codex`) |
| `TALK_CODEX_AGENT_MODEL` or `LiveBroker(agent_model=...)` | force a ChatGPT-valid thread model (e.g. `gpt-5.6-sol`) when the machine default is a custom proxy — otherwise delegated turns 400 |
| `LiveBroker(handoff=...)` | `client` (default) or `server` |

## Gotchas encoded in code (learned the hard way)

- The broker strips `OPENAI_API_KEY`/`CODEX_API_KEY` from the app-server env — otherwise the
  session leaves subscription auth and 401s/bills the wrong lane.
- On servers, `codex` often isn't on the service `PATH` (`~/.npm-global/bin` missing) — resolved
  via candidate paths + `bash -lc` fallback.
- `codex app-server` needs `-c model_provider=openai` on boxes with a custom default provider.
- JSON-RPC responses are located by **id scan** of the whole buffer; a positional cursor goes
  stale when the reader prunes and silently drops your response.
- Voice allowance is a separate rolling 5-hour bucket per plan (not exposed by the API) — the
  client meters `audio_duration_ms` locally and reports on hang-up.
- Old app-servers may reject `clientManagedHandoffs`/`delegationAckFiller` — the broker drops
  them one group per `unknown field` error and reports `droppedFields`/`handoffDegraded` so the
  UI can warn instead of failing silently.

## Tests

```bash
pip install pytest
python -m pytest -q
```

Regression tests (no network, no `codex` binary needed) cover: buffer-pruning-safe RPC,
unknown-field drop reporting + handoff degradation, stale-thread recovery keeping session
language, interrupt off the event loop, junk-delegation filtering, bubble rotation semantics,
plus a **fake app-server end-to-end smoke**: a stub `codex` binary speaks the JSON-RPC protocol
and a full `/session` mint succeeds through the HTTP layer.

## Credits & license

MIT — see [LICENSE](LICENSE). Extracted from
[Synero/hermes-live-voice](https://github.com/Synero/hermes-live-voice), whose voice auth and
session plumbing derive from [TheSmokeDev/hermes-talk](https://github.com/TheSmokeDev/hermes-talk) (MIT).
Codex and GPT are OpenAI products; this project is **not affiliated with or endorsed by OpenAI**.
Respect the OpenAI terms for your account; never share accounts or bypass rate limits.
