/**
 * gpt-live-client.js — zero-dependency ESM client for GPT-Live-1 voice sessions.
 *
 * Extracted from the hermes-live-voice desktop plugin (React half) into a
 * framework-agnostic library. Talks to the `gptlive.server` HTTP surface and
 * to OpenAI's realtime backend directly over WebRTC:
 *
 *   start() → RTCPeerConnection + DataChannel("oai-events") + mic
 *           → POST {offer} to the broker → setRemoteDescription(answer)
 *   audio flows browser ↔ OpenAI directly (server only signals)
 *
 * Protocol v3 (FramelessBidi / gpt-live-1-codex) essentials encoded here:
 * - Turns ROTATE and turn.done can arrive with PARTIAL transcripts — a
 *   transcript bubble stays open while same-role deltas keep arriving within
 *   GAP_MS (never shrink/close a bubble from turn.done; that duplicated text).
 * - Model audio arrives as a normal WebRTC media track (ontrack) — never build
 *   an output_audio.delta playback path.
 * - `conversation.item.create` does NOT exist in v3; delegation results go via
 *   `delegation.context.append`.
 *
 * Usage:
 *   import { LiveVoice } from './client/gpt-live-client.js'
 *   const voice = new LiveVoice({ brokerBase: '/api/voice' })
 *   voice.onTranscript = (items) => render(items)
 *   await voice.start({ voice: 'cove', language: 'en' })
 *   ...
 *   await voice.close()
 */

const GAP_MS = 3500   // bubble continues while same-role deltas arrive within this window
const IDLE_MS = 8000  // after this silence a bubble is marked done (dimmed at 72%)

export const V3_VOICES = ['cove', 'juniper', 'maple', 'spruce', 'ember', 'vale', 'breeze', 'arbor', 'sol']

function defaults(base) {
  return {
    brokerBase: base ?? '/api/voice',
    voice: 'cove',
    language: (String(navigator.language || 'en').toLowerCase().startsWith('en') ? 'en' : 'es'),
    micId: null,
    outputDeviceId: null,
    onStatus: () => {},        // (stage: string) — mint|offering|connected|failed|closed
    onTranscript: () => {},    // (items: [{role:'user'|'bot'|'tool'|'sys', text, done}])
    onUsage: () => {},         // ({audioMs}) — cumulative model-audio seconds per session
    onDelegation: null,        // async (request: string) => string — client task executor; null = built-in only
    onNotice: () => {},        // (text: string) — degraded-mode and connection notices
  }
}

export class LiveVoice {
  constructor(opts = {}) {
    this.opts = { ...defaults(opts.brokerBase), ...opts }
    this.transcript = []
    this.live = false
    this.muted = false
    this.audioMs = 0
    this._pc = null
    this._dc = null
    this._closeCtl = null
  }

  // ── transcript store (bubble semantics from the plugin) ───────────────────

  _emit() { this.opts.onTranscript(this.transcript.slice()) }

  _lastOpen(role) {
    for (let i = this.transcript.length - 1; i >= 0; i--) {
      const it = this.transcript[i]
      if (it.role === role && !it.done) return it
      if (it.role === role) break
    }
    return null
  }

  _recentOpen(role, now) {
    const it = this._lastOpen(role)
    return it && (now - (it.at || 0)) <= GAP_MS ? it : null
  }

  _push(role, text, done = true) {
    const now = Date.now()
    const open = this._recentOpen(role, now)
    if (open) {
      open.text += (open.text && !open.text.endsWith(' ') && !text.startsWith(' ') ? ' ' : '') + text
      open.at = now
      open.done = done
    } else {
      this.transcript.push({ role, text, at: now, done })
      if (this.transcript.length > 400) this.transcript.splice(0, this.transcript.length - 400)
    }
    this._emit()
  }

  /** Append a user-speech delta; merges into the open user bubble within GAP_MS. */
  _pushUserDelta(text) {
    if (!text) return
    const now = Date.now()
    let it = this._lastOpen('user')
    if (it) {
      it.text += text
      it.at = now
      it.done = false
    } else {
      it = { role: 'user', text, at: now, done: false }
      this.transcript.push(it)
    }
    this._emit()
  }

  /** turn.done for user role: only finalizes if text actually extends/replaces. */
  _finalizeUser(finalText) {
    const t = String(finalText || '').trim()
    const open = this._lastOpen('user')
    if (!t) { if (open) open.done = true; this._emit(); return }
    if (open && t.length > open.text.length) open.text = t
    if (open) open.done = true
    else this._push('user', t, true)
    this._emit()
  }

  /** turn.done for assistant role: close the bubble; NEVER shrink its text. */
  _endBot(finalText) {
    const open = this._lastOpen('bot')
    const t = String(finalText || '').trim()
    if (open) {
      if (t.length > open.text.length) open.text = t
      open.done = true
    } else if (t) {
      this._push('bot', t, true)
    }
    this._emit()
  }

  // ── broker HTTP ───────────────────────────────────────────────────────────

  async _rest(path, { method = 'POST', body, timeoutMs = 30000 } = {}) {
    const ctl = new AbortController()
    const timer = setTimeout(() => ctl.abort(), timeoutMs)
    try {
      const res = await fetch(this.opts.brokerBase.replace(/\/$/, '') + path, {
        method,
        headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
        body: body !== undefined ? JSON.stringify(body) : undefined,
        signal: ctl.signal,
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data?.detail || (res.status + ' ' + res.statusText))
      return data
    } finally {
      clearTimeout(timer)
    }
  }

  // ── delegation (client-managed handoffs) ──────────────────────────────────

  _isJunkDelegation(t) {
    const s = String(t || '').trim()
    if (!s) return true
    if (s.length > 90) return false
    const toks = s.toLowerCase().replace(/[¿?¡!.,;:…()"'«»]/g, ' ').split(/\s+/).filter(Boolean)
    if (!toks.length) return true
    const fill = new Set(['hola', 'hello', 'hey', 'dale', 'ya', 'bueno', 'bien', 'si', 'sí', 'no', 'nope', 'aló', 'alo', 'holi', 'eh', 'emm', 'mmm', 'ah', 'ahá', 'ajá', 'aja', 'ok', 'okay', 'listo', 'perfecto', 'gracias', 'thanks', 'eso', 'mismo', 'y', 'qué', 'que', 'pasó', 'paso', 'final', 'me', 'escuchas', 'estás', 'estas', 'ahí', 'ahi', 'oye', 'pues', 'po'])
    const n = toks.filter(w => fill.has(w)).length
    return n / toks.length >= 0.8
  }

  _plainForVoice(t) {
    let s = String(t || '')
    s = s.replace(/```[\s\S]*?```/g, ' (code block omitted) ')
    s = s.replace(/`([^`]*)`/g, '$1')
    s = s.replace(/\*\*([^*]+)\*\*/g, '$1').replace(/__([^_]+)__/g, '$1')
    s = s.replace(/^#{1,6}\s+/gm, '')
    s = s.replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
    s = s.replace(/\n{3,}/g, '\n\n')
    return s.trim()
  }

  _delegRespond(dc, itemId, text) {
    try {
      dc.send(JSON.stringify({ type: 'delegation.context.append', delegation_item_id: itemId,
        content: [{ type: 'input_text', text: String(text || '').slice(0, 3500) }] }))
    } catch { /* datachannel closing */ }
  }

  _handleDelegation(msg, dc) {
    const item = (msg && msg.item) || {}
    const itemId = String(item.id || '')
    let req = ''
    try {
      if (Array.isArray(item.content)) req = item.content.map(c => (c && c.text) || '').join('')
    } catch { /* malformed item */ }
    req = String(req || '').trim()
    if (!req) {
      for (let i = this.transcript.length - 1; i >= 0; i--) {
        if (this.transcript[i].role === 'user' && this.transcript[i].text) { req = String(this.transcript[i].text).trim(); break }
      }
    }
    this._push('tool', 'delegation: ' + (req || '…').slice(0, 180))
    if (this._handoff === 'server') {
      this._push('sys', 'The server agent is on it; the result will be read when ready.')
      return
    }
    if (this._isJunkDelegation(req)) {
      this._push('tool', 'small talk (not a task) — voice answers itself')
      this._delegRespond(dc, itemId, 'The user was just chatting, not asking for work. Reply briefly and naturally; if they were asking about something in progress, tell them you are still on it. Nothing to run.')
      return
    }
    if (this._delegBusy) {
      this._delegQueue = { req, itemId, at: Date.now() }
      this._push('tool', 'queued (task in progress): ' + req.slice(0, 100))
      this._delegRespond(dc, itemId, 'A task is already in progress. Tell the user you are still on it; if what they said is a correction it will be reflected in the result; if it is something new, wait for the current one to finish.')
      return
    }
    this._runDelegation(dc, itemId, req)
  }

  _runDelegation(dc, itemId, req) {
    this._delegBusy = true
    ;(async () => {
      let out = ''
      try {
        if (this.opts.onDelegation) out = await this.opts.onDelegation(req)
      } catch (e) {
        out = 'Task failed: ' + String((e && e.message) || e).slice(0, 200)
      }
      out = String(out || '').trim()
      if (!out) out = 'The task could not be completed.'
      const forVoice = this._plainForVoice(out)
      this._push('tool', 'result: ' + forVoice.slice(0, 160))
      this._delegRespond(dc, itemId, 'Task result (answer the user with this, briefly and naturally): ' + forVoice)
    })().catch(() => {}).then(() => {
      this._delegBusy = false
      const q = this._delegQueue
      this._delegQueue = null
      if (q && Date.now() - q.at < 600000) {
        this._push('tool', 'resuming queued: ' + q.req.slice(0, 100))
        this._runDelegation(dc, q.itemId, q.req)
      } else if (q) {
        this._push('sys', 'The queued task went stale and was not run.')
        this._delegRespond(dc, q.itemId, 'The queued task went stale and was not run. Tell the user that if they still want it, to say it again and you will run it right away.')
      }
    })
  }

  // ── barge-in ──────────────────────────────────────────────────────────────

  /** Barge-in: user speaks over the bot. Re-enables the mic track and cancels the bot turn. */
  _doBargeIn() {
    const now = Date.now()
    if (now - (this._lastBargeAt || 0) < 1200) return
    if (this.muted) return  // a muted mic cannot be un-muted by barge-in (0.2.1 fix)
    this._lastBargeAt = now
    const pc = this._pc
    try {
      if (pc) pc.getSenders().forEach(s => { if (s.track && s.track.kind === 'audio') s.track.enabled = true })
    } catch { /* pc closing */ }
    this._push('sys', 'interrupted — listening')
    const turnId = this._botTurnId
    if (!turnId || !this._session) return
    this._botTurnId = ''
    const body = { turnId, threadId: this._session.threadId }
    this._rest('/interrupt', { body, timeoutMs: 5000 }).catch(() => {})
  }

  _maybeBarge(micLevel) {
    if (this.muted) { this._botSpeakingSince = 0; return }
    const now = Date.now()
    if (this._botSpeaking) {
      if (!this._botSpeakingSince) this._botSpeakingSince = now
      if (now - this._botSpeakingSince > 600 && micLevel > 0.11) this._doBargeIn()
    } else {
      this._botSpeakingSince = 0
    }
  }

  // ── v3 event handling ─────────────────────────────────────────────────────

  _handleRealtimeEvent(msg, dc) {
    const t = msg && msg.type
    if (!t) return
    if (t === 'session.started' || t === 'session.updated') return
    if (t === 'input_transcript.added') { this._pushUserDelta((msg.item || {}).text); return }
    if (t === 'output_transcript.added') { this._push('bot', (msg.item || {}).text, false); return }
    if (t === 'turn.created') {
      const tu = msg.turn || {}
      if (tu.role === 'assistant' && tu.id) this._botTurnId = String(tu.id)
      return
    }
    if (t === 'turn.done') {
      const tu = msg.turn || {}
      if (tu.role === 'user') this._finalizeUser(tu.transcript || '')
      else if (tu.role === 'assistant') this._endBot(tu.transcript || '')
      return
    }
    if (t === 'turn.delta') return
    if (t === 'session.usage.updated') {
      const au = msg.usage && msg.usage.audio_duration_ms
      if (au) {
        try { this.audioMs = Math.max(this.audioMs || 0, Number(au) || 0); this.opts.onUsage({ audioMs: this.audioMs }) } catch { /* numeric edge */ }
      }
      return
    }
    if (t === 'delegation.created') { this._handleDelegation(msg, dc); return }
    if (t === 'delegation.context.appended') {
      if (this._handoff === 'server') this._push('tool', 'agent response streaming to the voice…')
      return
    }
    if (t === 'output_audio_buffer.started') this._botSpeaking = true
    else if (t === 'output_audio_buffer.stopped') this._botSpeaking = false
    else if (t === 'error') {
      const m = msg.error && (msg.error.message || msg.error.code)
      if (m) this._push('sys', 'error: ' + String(m).slice(0, 160))
    }
  }

  // ── session lifecycle ─────────────────────────────────────────────────────

  /**
   * Start a live session. Resolves once the WebRTC connection is negotiating;
   * transcript/status/usage flow through the callbacks.
   */
  async start({ voice, language, profile } = {}) {
    if (this.live) throw new Error('session already live')
    const o = this.opts
    if (voice) o.voice = voice
    if (language) o.language = language
    this.transcript = []
    this.audioMs = 0
    this._delegBusy = false
    this._delegQueue = null
    this._botTurnId = ''
    this._botSpeaking = false
    this._botSpeakingSince = 0
    o.onStatus('mint')

    // mic (fall back to system default if the picked device fails)
    const audioC = { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
    if (o.micId) audioC.deviceId = { exact: o.micId }
    let stream
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: audioC })
    } catch (e) {
      if (o.micId) {
        try {
          stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } })
          this._push('sys', 'Could not open the selected mic; using the system default.')
        } catch (e2) {
          throw new Error('MIC: ' + ((e2 && e2.name) || e?.name || '') + ' — check permissions or pick another mic')
        }
      } else {
        throw new Error('MIC: ' + (e?.name || '') + ' — check permissions')
      }
    }

    const pc = new RTCPeerConnection()
    this._pc = pc
    this.live = true
    stream.getTracks().forEach(t => pc.addTrack(t, stream))

    const dc = pc.createDataChannel('oai-events')
    this._dc = dc
    dc.onmessage = ev => {
      let msg = null
      try { msg = JSON.parse(ev.data) } catch { return }
      try { this._handleRealtimeEvent(msg, dc) } catch (e) { this._push('sys', 'event error: ' + String(e).slice(0, 120)) }
    }

    let audioEl = null
    pc.ontrack = ev => {
      audioEl = new Audio()
      audioEl.srcObject = ev.streams[0]
      audioEl.autoplay = true
      audioEl.play().catch(() => this.opts.onNotice('click the page to unmute the bot audio'))
      if (o.outputDeviceId && audioEl.setSinkId) {
        audioEl.setSinkId(o.outputDeviceId).catch(err => this.opts.onNotice('output device: ' + err.message))
      }
    }

    let connectedAt = 0
    pc.onconnectionstatechange = () => {
      o.onStatus(pc.connectionState)
      if (pc.connectionState === 'connected' && !connectedAt) connectedAt = Date.now()
      if (pc.connectionState === 'failed') {
        this.close().catch(() => {})
        o.onNotice('connection lost (failed)')
      }
    }

    o.onStatus('offering')
    const offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    let session
    try {
      session = await this._rest('/session', {
        body: { language: o.language, profile: profile || null, voice: o.voice, offer: offer.sdp },
        timeoutMs: 120000,
      })
    } catch (e) {
      try { stream.getTracks().forEach(t => t.stop()) } catch { /* already stopped */ }
      this.live = false
      throw e
    }
    if (!session || !session.answer) {
      try { stream.getTracks().forEach(t => t.stop()) } catch { /* already stopped */ }
      this.live = false
      throw new Error('no SDP answer from broker')
    }
    this._session = session
    this._handoff = session.handoff === 'server' ? 'server' : 'client'
    if (session.handoffDegraded) {
      this.opts.onNotice('this server does not support clientManagedHandoffs; voice tasks may run on the agent ChatGPT lane instead of your client')
    }
    await pc.setRemoteDescription({ type: 'answer', sdp: session.answer })

    // idle-close bubbles so separate interventions don't merge
    const idleTimer = setInterval(() => {
      const now = Date.now()
      let changed = false
      for (const it of this.transcript) {
        if (!it.done && now - (it.at || 0) > IDLE_MS) { it.done = true; changed = true }
      }
      if (changed) this._emit()
    }, 1000)

    // mic level via WebAudio → barge-in + visualizers
    const audioCtx = new AudioContext()
    if (audioCtx.state === 'suspended') { try { await audioCtx.resume() } catch { /* needs gesture */ } }
    const analyser = audioCtx.createAnalyser()
    analyser.fftSize = 512
    const muteGain = audioCtx.createGain()
    muteGain.gain.value = 0
    audioCtx.createMediaStreamSource(stream).connect(analyser)
    analyser.connect(muteGain)
    muteGain.connect(audioCtx.destination)
    const buf = new Uint8Array(analyser.fftSize)
    const meterLoop = setInterval(() => {
      try {
        analyser.getByteTimeDomainData(buf)
        let rms = 0
        for (let i = 0; i < buf.length; i++) { const v = (buf[i] - 128) / 128; rms += v * v }
        const level = Math.min(1, Math.sqrt(rms / buf.length) * 3.2)
        this.micLevel = this.muted ? 0 : level
        this._maybeBarge(this.micLevel)
      } catch { /* context closing */ }
    }, 90)

    this._closeCtl = () => {
      try { clearInterval(idleTimer) } catch { /* timer gone */ }
      try { clearInterval(meterLoop) } catch { /* timer gone */ }
      try { audioCtx.close() } catch { /* already closed */ }
      const durMs = connectedAt ? Date.now() - connectedAt : 0
      if (durMs > 4000) {
        this._rest('/usage/report', { body: { durationMs: Math.round(durMs), audioMs: Math.round(this.audioMs || 0) }, timeoutMs: 8000 }).catch(() => {})
      }
      try { if (audioEl) audioEl.pause() } catch { /* gone */ }
      try { stream.getTracks().forEach(t => t.stop()) } catch { /* gone */ }
      if (session && session.threadId) {
        this._rest('/stop', { body: { threadId: session.threadId }, timeoutMs: 8000 }).catch(() => {})
      }
      try { pc.close() } catch { /* gone */ }
      this._pc = null
      this._dc = null
      this._session = null
      this._botTurnId = ''
      this._botSpeakingSince = 0
    }
    return session
  }

  /** Open the datachannel-backed session is up? Send a text turn (no mic needed). */
  sendText(text) {
    if (!this._dc || this._dc.readyState !== 'open') throw new Error('session not open')
    this._dc.send(JSON.stringify({ type: 'session.context.append', content: [{ type: 'input_text', text: String(text || '') }] }))
  }

  toggleMute() {
    this.muted = !this.muted
    try {
      this._pc && this._pc.getSenders().forEach(s => { if (s.track && s.track.kind === 'audio') s.track.enabled = !this.muted })
    } catch { /* pc gone */ }
    this._push('sys', this.muted ? 'microphone muted' : 'microphone on')
    return this.muted
  }

  /** Hang up; reports usage and closes the broker-side realtime session. */
  async close() {
    const ctl = this._closeCtl
    this._closeCtl = null
    this.live = false
    try { ctl && ctl() } catch { /* best effort */ }
    this.opts.onStatus('closed')
  }
}

export default LiveVoice
