"""Minimal dev server for the example app: mounts the gptlive router and
serves examples/web statically.

    python examples/server.py            # http://localhost:8000
    OPENAI_API_KEY=... python examples/server.py   # works; key is stripped from the voice lane anyway
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from gptlive import mount

WEB_ROOT = Path(__file__).resolve().parent / "web"

app = FastAPI(title="gpt-live-voice example")

# CORS for browser clients served from another origin in development.
service = mount(app, prefix="/api/voice", cors_origins=["*"])

app.mount("/client", StaticFiles(directory=str(Path(__file__).resolve().parent.parent / "client")), name="client")


@app.get("/")
async def index():
    return FileResponse(WEB_ROOT / "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))
