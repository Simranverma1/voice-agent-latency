"""FastAPI app for the browser demo.

    uvicorn web.server:app --port 8000

Routes
  GET  /          landing page (web/static/index.html)
  GET  /demo      live demo page (web/static/demo.html)
  GET  /static/*  page assets, including the mic AudioWorklet
  GET  /reports/{breakdown,distribution}.png   committed benchmark charts
  GET  /health    liveness check for Render and the page's wake-up probe
  WS   /ws        the voice session

Each websocket connection gets its own Session (its own Deepgram socket, HTTP
pool and conversation history), so two browsers never share state.

Protocol
  client -> server  binary: 16 kHz mono int16 PCM, any frame size
                    text:   {"type": "hello", "code": ...} first
  server -> client  binary: 24 kHz mono int16 PCM (TTS audio)
                    text:   JSON events
                      {"type": "ready", "profile": ...}
                      {"type": "listening"}
                      {"type": "transcript", "text": ...}
                      {"type": "reply_chunk", "text": ...}  one per TTS chunk
                      {"type": "reply", "text": ...}        full reply
                      {"type": "audio_end"}                 no more audio this turn
                      {"type": "metrics", "turn_id", "metrics",
                       "critical_path", "errors", "measurement"}
                      {"type": "notice"|"error", "code", "message"}
"""
import asyncio
import contextlib
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .session import Session

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
CHARTS = {"breakdown.png", "distribution.png"}

app = FastAPI(title="voice-agent web demo", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")

if os.getenv("DEMO_DEV_PROMPTS") == "1":
    # Local testing only: lets an automated browser test feed the recorded
    # prompts in as a fake microphone. Never enabled on the deployed server
    # (and data/*.wav isn't in git, so it couldn't be).
    app.mount("/dev/prompts", StaticFiles(directory=ROOT / "data"), name="dev-prompts")


@app.get("/")
async def landing():
    return FileResponse(STATIC / "index.html")


@app.get("/demo")
async def demo():
    return FileResponse(STATIC / "demo.html")


@app.get("/reports/{name}")
async def chart(name: str):
    if name not in CHARTS:
        raise HTTPException(404)
    return FileResponse(ROOT / "reports" / name)


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    session = Session(send_text=ws.send_text, send_bytes=ws.send_bytes)
    turns = asyncio.create_task(session.run())
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes"):
                session.feed(msg["bytes"])
    except WebSocketDisconnect:
        pass
    finally:
        session.disconnect()
        # A turn may be mid-reply; nobody is left to hear it.
        turns.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await turns
