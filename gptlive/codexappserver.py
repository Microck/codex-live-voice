"""gptlive.codexappserver — JSON-RPC stdio driver for `codex app-server`.

The open-source Codex CLI (>= 0.154) ships an app-server mode that brokers
realtime voice sessions backed by the operator's ChatGPT/Codex subscription.
This module owns the child process and the stdio JSON-RPC conversation:

    initialize → thread/start → thread/realtime/start → notifications

Design notes carried over from production use:

- Responses are located by scanning the whole notification buffer by id.
  A positional cursor goes stale when the reader prunes the buffer, and a
  pruned cursor silently drops the response you are waiting for.
- The child never sees OPENAI_API_KEY / CODEX_API_KEY: the voice lane must
  authenticate with the local `codex login` subscription, not an API key.
- `model_provider=openai` is forced because boxes with a custom default
  provider (a proxy) fail realtime negotiation.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

_log = logging.getLogger("gptlive")

DEFAULT_VOICE = "cove"
V3_VOICES = ("cove", "juniper", "maple", "spruce", "ember", "vale", "breeze", "arbor", "sol")


class CodexBinaryNotFoundError(RuntimeError):
    pass


def resolve_codex_binary(extra_candidates: list[str] | None = None) -> str | None:
    """Resolve the codex binary even when the service PATH lacks ~/.npm-global/bin."""
    try:
        found = shutil.which("codex")
    except Exception:  # pragma: no cover - shutil.which rarely raises
        found = None
    if found:
        return found
    home = Path.home()
    candidates = [
        home / ".npm-global" / "bin" / "codex",
        Path("/usr/local/bin/codex"),
        home / ".local" / "bin" / "codex",
        Path("/usr/bin/codex"),
    ] + [Path(c) for c in (extra_candidates or [])]
    for cand in candidates:
        try:
            if cand.exists() and os.access(cand, os.X_OK):
                return str(cand)
        except OSError:  # pragma: no cover
            continue
    try:
        out = subprocess.run(
            ["bash", "-lc", "command -v codex"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip().splitlines()[0]
    except Exception:  # pragma: no cover
        pass
    return None


def codex_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Child env: PATH augmented with common codex install dirs, API keys stripped.

    Stripping the keys is load-bearing: if an OPENAI_API_KEY is visible the
    app-server switches off subscription auth and the session 401s or bills
    the wrong lane.
    """
    env = dict(base if base is not None else os.environ)
    env["PATH"] = (str(Path.home() / ".npm-global" / "bin") + ":" + env.get("PATH", "")).strip(":")
    env.pop("OPENAI_API_KEY", None)
    env.pop("CODEX_API_KEY", None)
    return env


class CodexAppServer:
    """One `codex app-server` child process; JSON-RPC over stdio.

    Thread-safety: request() holds an RLock for the send+poll cycle. The
    internal transport is serialized single-lane — exactly like the plugin
    this was extracted from — and session minting is additionally serialized
    by LiveBroker's lock.
    """

    def __init__(
        self,
        binary: str | None = None,
        spawn: bool = True,
        client_name: str = "gpt-live-voice",
        client_version: str = "1.0",
        env: dict[str, str] | None = None,
        on_notification=None,
    ) -> None:
        self._bin = binary
        self._env = env
        self._client_name = client_name
        self._client_version = client_version
        self._on_notification = on_notification
        self.proc: subprocess.Popen | None = None
        self.notifs: list[dict] = []
        self.seq = 0
        self.thread_id: str | None = None
        self.thread_model: str | None = None
        self.thread_language: str | None = None
        self._sendlock = threading.RLock()
        if spawn:
            self.ensure()

    # ── transport ────────────────────────────────────────────────────────

    def _send(self, obj: dict) -> None:
        proc = self.proc
        if proc is None or proc.poll() is not None:
            raise RuntimeError("codex app-server no disponible")
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def _reader(self, proc) -> None:
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                self.notifs.append(msg)
                if len(self.notifs) > 6000:
                    del self.notifs[:2000]
                if self._on_notification is not None:
                    try:
                        self._on_notification(msg)
                    except Exception:  # pragma: no cover - callback must not kill reader
                        _log.exception("on_notification callback failed")
        except Exception:
            pass

    def request(self, method: str, params: dict, timeout: float = 30.0):
        """Send a JSON-RPC request and wait for its response by id.

        Scans the full buffer for the id: the reader prunes the front of the
        buffer, so a positional start cursor can go stale and lose a response
        that arrived while pruning. Scanning is O(n) on a bounded buffer.
        """
        with self._sendlock:
            self.seq += 1
            rid = self.seq
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            t0 = time.time()
            while time.time() - t0 < timeout:
                for m in list(self.notifs):
                    if m.get("id") == rid:
                        if "error" in m:
                            raise RuntimeError(str(m["error"].get("message") or m["error"])[:400])
                        return m.get("result")
                time.sleep(0.05)
            raise TimeoutError(f"codex app-server: {method} sin respuesta")

    def notifications_from(self, start: int) -> list[dict]:
        return list(self.notifs)[start:]

    # ── lifecycle ────────────────────────────────────────────────────────

    def ensure(self) -> None:
        proc = self.proc
        if proc is not None and proc.poll() is None:
            return
        binary = self._bin or resolve_codex_binary()
        if not binary:
            raise CodexBinaryNotFoundError(
                "codex no encontrado: instala el Codex CLI (>= 0.154) y ejecuta `codex login`"
            )
        env = codex_env(self._env)
        proc = subprocess.Popen(
            [binary, "app-server", "--listen", "stdio://",
             "--enable", "realtime_conversation",
             "-c", "model_provider=openai",
             "-c", "suppress_unstable_features_warning=true"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, env=env,
        )
        self.proc = proc
        self.notifs = []
        self.thread_id = None
        self.thread_model = None
        self.thread_language = None
        threading.Thread(target=self._reader, args=(proc,), daemon=True).start()
        self.request(
            "initialize",
            {"clientInfo": {"name": self._client_name, "version": self._client_version},
             "capabilities": {"experimentalApi": True}},
            timeout=25,
        )

    def stop_thread(self, thread_id: str | None) -> None:
        if not thread_id:
            return
        try:
            self.request("thread/realtime/stop", {"threadId": thread_id}, timeout=10)
        except Exception:
            pass

    def close(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=4)
            except Exception:
                proc.kill()
        except Exception:
            pass
