"""gptlive.voiceusage — local voice-minute metering.

The ChatGPT plan's realtime allowance counter is not exposed to clients
(wham/usage, rate-limit endpoints and session events all leave it null), so
usage is metered locally from `audio_duration_ms` reported by the client when
a call ends. JSON file storage, no database.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path


class VoiceUsageStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("sessions"), list):
                return data
        except Exception:
            pass
        return {"sessions": []}

    def _save(self, data: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def report(self, duration_ms: int, audio_ms: int) -> None:
        dur = max(0, int(duration_ms or 0))
        aud = max(0, int(audio_ms or 0))
        if dur <= 0:
            raise ValueError("durationMs required")
        now_ms = int(time.time() * 1000)
        with self._lock:
            data = self._load()
            sess = data.setdefault("sessions", [])
            sess.append({"t": now_ms, "ms": dur, "audio_ms": aud})
            keep = [s for s in sess if isinstance(s, dict) and int(s.get("t") or 0) > now_ms - 8 * 24 * 3600 * 1000]
            data["sessions"] = keep[-2000:]
            self._save(data)

    def summary(self) -> dict:
        import datetime

        with self._lock:
            data = self._load()
        now_ms = int(time.time() * 1000)
        midnight = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        today_ms = int(midnight.timestamp() * 1000)
        sess = [s for s in (data.get("sessions") or []) if isinstance(s, dict)]

        def agg(since_ms: int) -> dict:
            ms = aud = n = 0
            for s in sess:
                if int(s.get("t") or 0) >= since_ms:
                    ms += int(s.get("ms") or 0)
                    aud += int(s.get("audio_ms") or 0)
                    n += 1
            return {"minutes": ms / 60000.0, "audioMinutes": aud / 60000.0, "sessions": n}

        return {
            "rolling5h": agg(now_ms - 5 * 3600 * 1000),
            "rolling24h": agg(now_ms - 24 * 3600 * 1000),
            "week": agg(now_ms - 7 * 24 * 3600 * 1000),
            "today": agg(today_ms),
        }
