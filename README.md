# codex-live-voice

`codex-live-voice` adds two-way voice calls to a web app through a locally signed-in Codex CLI. The Python broker asks `codex app-server` for a realtime session. The browser sends audio to OpenAI over WebRTC. The broker handles signaling, session control, and optional delegation to your app.

It uses the Codex login in `~/.codex/auth.json` or `$CODEX_HOME/auth.json`. You do not need a separate OpenAI API key for the voice session. The broker runs under your Codex account, so keep its HTTP routes behind your app's access controls.

## what is included

- `gptlive/`: Python broker and optional FastAPI router.
- `client/gpt-live-client.js`: browser WebRTC client with transcript, mute, barge-in, usage, and delegation callbacks.
- `examples/`: a local demo served on `127.0.0.1:8000`.

The browser sends an SDP offer to the broker. The broker exchanges it with `codex app-server` and returns an SDP answer. After that, audio goes directly between the browser and OpenAI. The browser client has no build step.

## quickstart

Requires Python 3.11 or newer, a browser with microphone and WebRTC support, and Codex CLI 0.154 or newer on the same machine as the broker.

```bash
git clone https://github.com/Microck/codex-live-voice.git
cd codex-live-voice
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[server]"
codex login
python examples/server.py
```

Open `http://127.0.0.1:8000`, allow microphone access, and select **Start call**. The example binds to loopback and rejects browser requests from other origins. Run `codex login` in the same user account that starts the server. If you use `CODEX_HOME`, set it for both commands.

The example is for local use. To put this in a hosted app, use the access dependency shown below. Do not expose the example server or an unprotected router on a public address.

## use it in an app

### FastAPI

Pass a FastAPI dependency that checks the caller before any voice route runs:

```python
from fastapi import FastAPI
from gptlive import mount
from myapp.auth import require_signed_in_user

app = FastAPI()
mount(app, access_dependency=require_signed_in_user, prefix="/api/voice")
```

The dependency must cover the calling user's access to this Codex account. If your app uses cookies, also protect the state-changing routes against cross-site requests. Limit who can start sessions and how often. The broker uses one Codex app-server lane, so treat it as one active call at a time. `mount` does not choose an authentication scheme for your app. It uses no cross-origin access by default; set `cors_origins` only for trusted frontend origins.

The router exposes:

| method | path | purpose |
| --- | --- | --- |
| `GET` | `/status` | Codex availability, login state, voices, handoff mode |
| `POST` | `/session` | Exchange an SDP offer for an answer and thread ID |
| `POST` | `/interrupt`, `/stop` | Interrupt a turn or end a session |
| `GET` | `/auth/status` | Device-login state |
| `POST` | `/auth/login`, `/auth/login/cancel`, `/auth/logout` | Change the broker user's Codex login |
| `GET` | `/usage`, `/plan-usage` | Local voice time and Codex plan usage |
| `POST` | `/usage/report` | Record client-reported voice time |

The auth routes can replace or remove the broker user's `auth.json`. Keep them available only to an operator if regular app users should not control the broker login. Your app can apply route-specific checks inside the access dependency.

### Browser

Serve `client/gpt-live-client.js` as an ES module and point it at your mounted broker:

```js
import { LiveVoice } from '/client/gpt-live-client.js'

const voice = new LiveVoice({
  brokerBase: '/api/voice',
  onStatus: status => console.log(status),
  onTranscript: items => renderTranscript(items),
  onDelegation: async request => runTaskInMyApp(request),
})

await voice.start({ voice: 'cove', language: 'en' })
// voice.toggleMute(); voice.sendText('hello'); await voice.close()
```

`onDelegation` is optional. With the default `handoff="client"`, the browser receives delegated work and can return the result to the voice session. If you use `handoff="server"`, the Codex thread agent may run tools on the broker host. Give that mode access only to people you trust to use those tools.

The source checkout keeps the browser module in `client/`. The Python wheel also includes it at `gptlive/client/gpt-live-client.js`, accessible with `importlib.resources.files("gptlive").joinpath("client/gpt-live-client.js")`. Your app is responsible for serving it.

### Python broker without FastAPI

```python
from gptlive import LiveBroker

broker = LiveBroker()
try:
    session = broker.start_session(sdp_offer, voice="cove", language="en")
    answer = session["answer"]
    thread_id = session["threadId"]
    # Send answer to the browser. Later: broker.stop_session(thread_id)
finally:
    broker.close()
```

Pass a `PersonaProvider` to `LiveBroker(persona=...)` to set your product's voice identity. `DefaultPersona` is used otherwise.

## configuration and limits

| setting | effect |
| --- | --- |
| `CODEX_HOME` | Directory containing `auth.json`; defaults to `~/.codex` |
| `LiveBroker(agent_model=...)` | Selects the Codex thread model |
| `LiveBroker(handoff=...)` | `client` by default, or `server` for Codex tool execution |
| `mount(..., data_dir=...)` | Directory for local voice-usage data; defaults to `./data` |

The broker removes `OPENAI_API_KEY` and `CODEX_API_KEY` from the child app-server process so it uses the local Codex login. It does not send `auth.json` to the browser. Session availability still depends on the installed Codex CLI, the signed-in account, and OpenAI's current realtime support and limits. The app-server realtime protocol is experimental and may change.

## tests

```bash
python -m pip install -e ".[server,dev]"
python -m pytest -q
bash tools/client-load-test.sh
python -m pip install build
python -m build --wheel
```

The Python tests use a fake Codex app-server and do not contact OpenAI. They check session negotiation, recovery, access dependencies, auth isolation, and usage. The client check loads the browser module in Node. A real voice call needs a browser, microphone, signed-in Codex CLI, and network access.
