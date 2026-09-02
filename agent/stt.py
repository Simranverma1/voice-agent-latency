"""Speech-to-text backends.

batch     : you must have the whole utterance before you can start. The clock
            only starts once the user is done -> STT latency is fully additive.
streaming : audio is uploaded while the user is still talking, so by the time
            they stop, the transcript is essentially already there. This is the
            single biggest structural win in the STT stage.
"""
import asyncio
import io
import json
import wave

import httpx
import websockets

from .config import DEEPGRAM_API_KEY, MIC_SAMPLE_RATE, OPENAI_API_KEY
from .metrics import now
from .net import with_retry

DG_WS = "wss://api.deepgram.com/v1/listen"
DG_REST = "https://api.deepgram.com/v1/listen"


def _wav_bytes(pcm: bytes, samplerate: int = MIC_SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(samplerate)
        w.writeframes(pcm)
    return buf.getvalue()


# --------------------------------------------------------------------------
# Batch
# --------------------------------------------------------------------------
async def transcribe_openai(pcm: bytes, client: httpx.AsyncClient) -> str:
    files = {"file": ("audio.wav", _wav_bytes(pcm), "audio/wav")}
    data = {"model": "whisper-1", "response_format": "json", "language": "en"}
    r = await client.post(
        "https://api.openai.com/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        files=files, data=data, timeout=15.0,
    )
    r.raise_for_status()
    return r.json().get("text", "").strip()


async def transcribe_deepgram(pcm: bytes, client: httpx.AsyncClient) -> str:
    params = {
        "model": "nova-3", "encoding": "linear16",
        "sample_rate": str(MIC_SAMPLE_RATE), "channels": "1",
        "punctuate": "true", "smart_format": "true",
    }

    async def _call():
        r = await client.post(
            DG_REST, params=params,
            headers={"Authorization": f"Token {DEEPGRAM_API_KEY}",
                     "Content-Type": "audio/raw"},
            content=pcm, timeout=15.0,
        )
        r.raise_for_status()
        return r

    # A per-read timeout doesn't bound total time if a connection dribbles
    # data slowly across many small reads within one call. Cap wall-clock
    # time across both retry attempts combined.
    r = await asyncio.wait_for(with_retry(_call), timeout=25.0)
    alts = r.json()["results"]["channels"][0]["alternatives"]
    return alts[0]["transcript"].strip() if alts else ""


# --------------------------------------------------------------------------
# Streaming
# --------------------------------------------------------------------------
class DeepgramStream:
    """Live websocket transcription with server-side endpointing.

    Emits events onto `self.events`:
        {"type": "interim"|"final"|"speech_final"|"utterance_end", "text": str, "ts": float}

    `speech_final` fires when Deepgram's endpointer decides the speaker stopped
    (controlled by `endpointing_ms`). We use it both as the transcript AND as
    the end-of-turn signal in profile v5.
    """

    def __init__(self, endpointing_ms: int = 300, utterance_end_ms: int = 1000,
                 model: str = "nova-3"):
        self.endpointing_ms = endpointing_ms
        self.utterance_end_ms = utterance_end_ms
        self.model = model
        self.events: asyncio.Queue = asyncio.Queue()
        self.transcript_parts: list[str] = []
        self._ws = None
        self._rx = None
        self.connect_ms: float | None = None

    def _url(self) -> str:
        q = {
            "model": self.model,
            "encoding": "linear16",
            "sample_rate": str(MIC_SAMPLE_RATE),
            "channels": "1",
            "interim_results": "true",
            "punctuate": "true",
            "smart_format": "true",
            "vad_events": "true",
            "endpointing": str(self.endpointing_ms),
            "utterance_end_ms": str(self.utterance_end_ms),
        }
        return DG_WS + "?" + "&".join(f"{k}={v}" for k, v in q.items())

    async def connect(self):
        t0 = now()
        self._ws = await websockets.connect(
            self._url(),
            additional_headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"},
            ping_interval=5, ping_timeout=20, max_size=None,
        )
        self.connect_ms = (now() - t0) * 1000
        self._rx = asyncio.create_task(self._receive())

    async def _receive(self):
        try:
            async for raw in self._ws:
                ts = now()
                msg = json.loads(raw)
                mtype = msg.get("type")
                if mtype == "Results":
                    alts = msg["channel"]["alternatives"]
                    text = alts[0]["transcript"].strip() if alts else ""
                    if msg.get("speech_final"):
                        if text:
                            self.transcript_parts.append(text)
                        await self.events.put({"type": "speech_final", "text": self.text(), "ts": ts})
                    elif msg.get("is_final"):
                        if text:
                            self.transcript_parts.append(text)
                        await self.events.put({"type": "final", "text": text, "ts": ts})
                    elif text:
                        await self.events.put({"type": "interim", "text": text, "ts": ts})
                elif mtype == "UtteranceEnd":
                    await self.events.put({"type": "utterance_end", "text": self.text(), "ts": ts})
                elif mtype == "SpeechStarted":
                    await self.events.put({"type": "speech_started", "text": "", "ts": ts})
        except (websockets.ConnectionClosed, asyncio.CancelledError):
            pass

    def text(self) -> str:
        return " ".join(p for p in self.transcript_parts if p).strip()

    async def send(self, pcm: bytes):
        if self._ws:
            try:
                await self._ws.send(pcm)
            except websockets.ConnectionClosed:
                pass

    async def finalize(self):
        """Ask Deepgram to flush anything buffered immediately."""
        if self._ws:
            try:
                await self._ws.send(json.dumps({"type": "Finalize"}))
            except websockets.ConnectionClosed:
                pass

    async def close(self):
        if self._ws:
            try:
                await self._ws.send(json.dumps({"type": "CloseStream"}))
            except websockets.ConnectionClosed:
                pass
            await asyncio.sleep(0.05)
            await self._ws.close()
        if self._rx:
            self._rx.cancel()
