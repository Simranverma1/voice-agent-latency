"""FastAPI app for the browser demo.

    uvicorn web.server:app --port 8000

One websocket endpoint, /ws. Each connection gets its own Session (its own
Deepgram socket, HTTP pool and conversation history), so two browsers never
share state.

Protocol
  client -> server  binary: 16 kHz mono int16 PCM, any frame size
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
"""
import asyncio
import contextlib

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from .session import Session

app = FastAPI(title="voice-agent web demo")


@app.get("/healthz")
async def healthz() -> dict:
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
