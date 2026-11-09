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
  client -> server  text:   {"type": "hello", "code": ...} first, within 10 s;
                            refused unless code == $ACCESS_CODE
                    binary: 16 kHz mono int16 PCM, any frame size
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
import hmac
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .session import Session

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
CHARTS = {"breakdown.png", "distribution.png"}

# Every session costs real API credit and holds a Deepgram socket open, so
# only a couple may run at once. Read per request so tests can override it.
HELLO_TIMEOUT_S = 10
BAD_CODE_DELAY_S = 1.0   # slows down guessing the access code
_active_sessions = 0


def _max_sessions() -> int:
    try:
        return int(os.getenv("MAX_SESSIONS", "2"))
    except ValueError:
        return 2

app = FastAPI(title="voice-agent web demo", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def revalidate(request, call_next):
    # Without a Cache-Control header Chrome guesses a freshness lifetime from
    # Last-Modified and reuses files without asking, so after an update a
    # visitor could get the old demo.html with the new demo.js. "no-cache"
    # makes it revalidate every time; unchanged files still come back as a
    # cheap 304 thanks to the ETag.
    response = await call_next(request)
    response.headers.setdefault("Cache-Control", "no-cache")
    return response

# Local testing only. With TEST_MODE=1 the demo page offers "Play recording",
# which feeds a prompt from data/ through the same AudioWorklet path the
# microphone uses, so the whole browser pipeline can be tested without
# speaking. Off unless explicitly set; render.yaml never sets it, and
# data/*.wav isn't in git, so a deployment couldn't serve the files anyway.
TEST_MODE = os.getenv("TEST_MODE") == "1"
if TEST_MODE:
    app.mount("/test/prompts", StaticFiles(directory=ROOT / "data"), name="test-prompts")


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
    return {"ok": True, "sessions": _active_sessions, "max_sessions": _max_sessions(),
            "test_mode": TEST_MODE}


async def _reject(ws: WebSocket, code: str, message: str, close_code: int):
    # Browsers can't read the HTTP status of a refused websocket handshake,
    # so accept, explain in a JSON event, then close.
    with contextlib.suppress(Exception):
        await ws.send_text(json.dumps({"type": "error", "code": code, "message": message}))
        await ws.close(code=close_code)


async def _read_hello(ws: WebSocket) -> dict | None:
    try:
        msg = await asyncio.wait_for(ws.receive(), timeout=HELLO_TIMEOUT_S)
        if msg.get("text"):
            hello = json.loads(msg["text"])
            if isinstance(hello, dict) and hello.get("type") == "hello":
                return hello
    except (asyncio.TimeoutError, json.JSONDecodeError, WebSocketDisconnect, RuntimeError):
        pass
    return None


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    global _active_sessions
    await ws.accept()

    hello = await _read_hello(ws)
    expected = os.getenv("ACCESS_CODE", "")
    if not expected:
        # Fail closed: a deployment that forgot the variable must not be
        # open to everyone on the internet, spending the API credit.
        await _reject(ws, "server_misconfigured",
                      "The demo isn't configured yet (no access code is set on the server).", 4500)
        return
    if hello is None:
        await _reject(ws, "bad_hello", "The page and server couldn't agree how to talk. Refresh the page.", 4400)
        return
    if not hmac.compare_digest(str(hello.get("code", "")).encode(), expected.encode()):
        await asyncio.sleep(BAD_CODE_DELAY_S)
        await _reject(ws, "bad_code", "That access code isn't right. Check it and press Start talking again.", 4401)
        return
    if _active_sessions >= _max_sessions():
        await _reject(ws, "busy", f"{_max_sessions()} people are already using the demo. "
                                  "Please try again in a minute.", 4429)
        return

    _active_sessions += 1
    session = Session(send_text=ws.send_text, send_bytes=ws.send_bytes)
    turns = asyncio.create_task(session.run())

    async def receive_audio():
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if msg.get("bytes"):
                session.feed(msg["bytes"])

    receiver = asyncio.create_task(receive_audio())
    try:
        # Ends when the browser leaves, or when the session ends itself
        # (turn limit, idle, unrecoverable error).
        await asyncio.wait({turns, receiver}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        _active_sessions -= 1
        session.disconnect()
        # A turn may be mid-reply; nobody is left to hear it.
        for t in (turns, receiver):
            t.cancel()
        for t in (turns, receiver):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        with contextlib.suppress(Exception):
            await ws.close(code=1000)
