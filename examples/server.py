"""Minimal dev server for the example app: mounts the gptlive router and
serves examples/web statically.

    python examples/server.py            # http://localhost:8000
    OPENAI_API_KEY=... python examples/server.py   # works; key is stripped from the voice lane anyway
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from gptlive import mount

WEB_ROOT = Path(__file__).resolve().parent / "web"

app = FastAPI(title="codex-live-voice example")

def require_local_access(request: Request) -> None:
    """Keep the example's account controls on this machine and its own page."""
    port = os.environ.get("PORT", "8000")
    allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    origin = request.headers.get("origin")
    fetch_site = request.headers.get("sec-fetch-site")
    if (request.client is None or request.client.host not in {"127.0.0.1", "::1"}
            or (origin is not None and origin not in allowed_origins)
            or fetch_site == "cross-site"):
        raise HTTPException(status_code=403, detail="local access only")


service = mount(app, prefix="/api/voice", access_dependency=require_local_access)

app.mount("/client", StaticFiles(directory=str(Path(__file__).resolve().parent.parent / "client")), name="client")


@app.get("/")
async def index():
    return FileResponse(WEB_ROOT / "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))
