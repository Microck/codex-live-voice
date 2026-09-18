"""gptlive.codexauth — codex login state, device-code login, plan usage.

The voice lane authenticates with the operator's local `codex login`
(`$CODEX_HOME/auth.json` / `~/.codex/auth.json`). This module answers "am I
signed in?", drives the interactive device-code login as a background process
(URL + code surfaced to the UI), and queries the ChatGPT plan usage buckets.

Device login backs up the current auth.json first: starting a login can
replace/clear the file before approval completes (verified upstream 2026-09-11).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .codexappserver import resolve_codex_binary

LOGIN_URL = "https://auth.openai.com/codex/device"
_URL_RE = re.compile(r"https://auth\.openai\.com/codex/device")
_CODE_RE = re.compile(r"\b([A-Z0-9]{4}-[A-Z0-9]{5})\b")
_LOGIN_TIMEOUT_S = 16 * 60


def codex_auth_path() -> Path:
    configured = os.environ.get("CODEX_HOME", "").strip()
    base = Path(configured) if configured else Path.home() / ".codex"
    return base / "auth.json"


def read_account_id() -> str:
    try:
        data = json.loads(codex_auth_path().read_text(encoding="utf-8"))
        return str((data.get("tokens") or {}).get("account_id") or "")
    except Exception:
        return ""


def _terminate(proc) -> None:
    try:
        proc.terminate()
        try:
            proc.wait(timeout=4)
        except Exception:
            proc.kill()
    except Exception:
        pass


class CodexAuthManager:
    """Tracks a device-code login process; thread-safe.

    `status()` folds process state in: while the login process is alive the
    status is `pending`; when it exits, auth.json is re-checked (a `valid` or
    `expired` OAuth state means success — the CLI may exit before the token
    file is re-validated by other readers).
    """

    def __init__(self, binary: str | None = None, auth_probe=None) -> None:
        self._binary = binary
        self._auth_probe = auth_probe or self._default_probe
        self._lock = threading.Lock()
        self._state: dict = {
            "status": "idle",  # idle | pending | done | error
            "url": None,
            "code": None,
            "message": None,
            "started_at": None,
            "proc": None,
            "output": "",
        }

    @staticmethod
    def _default_probe() -> str | None:
        """Probe login state from auth.json without external deps.

        Returns 'valid' | 'expired' | None. Token expiry parsing is best-effort
        across CLI versions; absence of the file or tokens means not logged in.
        """
        path = codex_auth_path()
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        tokens = data.get("tokens") or {}
        if not tokens:
            return None
        # id_token is a JWT; last_key_refresh/timeout fields vary by version.
        exp = None
        for key in ("expires_at", "access_token_expires_at"):
            v = data.get(key) or tokens.get(key)
            if isinstance(v, (int, float)):
                exp = float(v)
                break
        if exp and time.time() > exp:
            return "expired"
        return "valid"

    # ── login lifecycle ──────────────────────────────────────────────────

    def _reader(self, proc) -> None:
        try:
            for line in iter(proc.stdout.readline, ""):
                with self._lock:
                    st = self._state
                    st["output"] = (st["output"] + line)[-4000:]
                    if not st["url"]:
                        m = _URL_RE.search(line)
                        if m:
                            st["url"] = m.group(0)
                    if not st["code"]:
                        m = _CODE_RE.search(line)
                        if m:
                            st["code"] = m.group(1)
        except Exception:
            pass

    def _check(self) -> None:
        """Fold the login process state into self._state (call before reporting)."""
        st = self._state
        proc = st.get("proc")
        if st.get("status") != "pending":
            return
        if proc is not None and proc.poll() is None:
            started = st.get("started_at") or 0
            if time.time() - started > _LOGIN_TIMEOUT_S:
                _terminate(proc)
                st.update(status="error", message="timeout: the code expired without approval", proc=None)
            return
        state = self._auth_probe()
        if state in {"valid", "expired"}:
            st.update(status="done", message="signed in", proc=None, url=None, code=None, output="")
        else:
            tail = " / ".join((st.get("output") or "").strip().splitlines()[-3:])
            st.update(status="error", message=("could not sign in" + (f": {tail}" if tail else "")), proc=None)

    def start_login(self) -> dict:
        with self._lock:
            self._check()
            proc = self._state.get("proc")
            if proc is not None and proc.poll() is None:
                _terminate(proc)
            binary = self._binary or resolve_codex_binary()
            if not binary:
                return {"ok": False, "message": "codex CLI not found on the server"}
            auth_path = codex_auth_path()
            if auth_path.exists():
                try:
                    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
                    shutil.copy2(auth_path, auth_path.with_name(f"auth.json.bak-pre-login-{ts}"))
                except OSError:
                    pass
            env = os.environ.copy()
            proc = subprocess.Popen(
                [binary, "login", "--device-auth"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
                cwd=str(Path.home()),
            )
            self._state.update(status="pending", url=None, code=None, message=None,
                               started_at=time.time(), proc=proc, output="")
            threading.Thread(target=self._reader, args=(proc,), daemon=True).start()
        # the reader needs a moment to capture URL/code
        deadline = time.time() + 8
        while time.time() < deadline and not (self._state.get("url") and self._state.get("code")):
            time.sleep(0.2)
        return {"ok": True, "url": self._state.get("url") or LOGIN_URL,
                "code": self._state.get("code"), "status": self._state["status"]}

    def cancel_login(self) -> None:
        with self._lock:
            proc = self._state.get("proc")
            if proc is not None and proc.poll() is None:
                _terminate(proc)
            self._state.update(status="idle", proc=None, url=None, code=None, message=None, output="")
        self.restore_backup_if_needed()

    def restore_backup_if_needed(self) -> None:
        """A cancelled login can leave auth.json cleared; restore the newest backup."""
        path = codex_auth_path()
        if path.exists():
            return
        backups = sorted(path.parent.glob("auth.json.bak-pre-login-*"))
        if backups:
            try:
                shutil.copy2(backups[-1], path)
            except OSError:
                pass

    def logout(self) -> dict:
        path = codex_auth_path()
        if not path.exists():
            return {"ok": True, "message": "no session to close"}
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        bak = path.with_name(f"auth.json.bak-voice-logout-{ts}")
        try:
            shutil.copy2(path, bak)
            path.unlink()
        except OSError as exc:
            return {"ok": False, "message": f"logout failed: {exc}"}
        return {"ok": True, "message": f"signed out (backup: {bak.name})"}

    def status(self) -> dict:
        with self._lock:
            self._check()
            return {k: self._state.get(k) for k in ("status", "url", "code", "message")}

    def logged_in(self) -> bool:
        return self._auth_probe() in {"valid", "expired"}


# ── plan usage (ChatGPT subscription buckets) ────────────────────────────────

def plan_usage(access_token: str, account_id: str = "", timeout: float = 12.0) -> dict:
    """Query wham/usage with the subscription token; returns {ok, plan?, windows}.

    Needs an access token (from auth.json) — NOT an API key. Raises on HTTP
    failure so callers can distinguish "not signed in" from "endpoint gone".
    """
    import urllib.request

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": "codex-cli",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    payload = None
    last_status = None
    last_err = None
    for url in (
        "https://chatgpt.com/backend-api/wham/usage",
        "https://api.openai.com/api/codex/usage",
    ):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                last_status = resp.status
                if resp.status == 200:
                    payload = json.loads(resp.read().decode("utf-8") or "{}")
                    break
        except Exception as exc:
            last_err = exc
            try:
                last_status = getattr(exc, "code", None)  # HTTPError carries status
            except Exception:
                pass
    if payload is None:
        raise RuntimeError(f"usage http {last_status}" + (f" ({last_err})" if last_status != 200 and last_err else ""))
    rl = payload.get("rate_limit") or {}
    windows: dict = {}
    for key, fallback in (("primary_window", "5h"), ("secondary_window", "weekly")):
        w = rl.get(key) or {}
        if not (isinstance(w, dict) and w):
            continue
        mins = w.get("window_minutes")
        secs = w.get("limit_window_seconds")
        if not mins and secs:
            try:
                mins = int(secs) // 60
            except Exception:
                mins = None
        if mins:
            mins = int(mins)
            label = "weekly" if mins >= 6 * 1440 else f"{max(1, round(mins / 60))}h"
        else:
            label = fallback
        windows[label] = {
            "usedPercent": w.get("used_percent"),
            "windowMinutes": mins,
            "resetsAt": w.get("reset_at") or w.get("resets_at"),
        }
    return {
        "ok": True,
        "plan": payload.get("plan_type") or payload.get("plan") or None,
        "windows": windows,
    }
