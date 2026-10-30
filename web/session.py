"""One browser connection = one Session, running the v5_full pipeline.

Why this file re-implements the turn orchestration instead of using
agent.pipeline.VoiceAgent: pipeline.py imports agent.audio_io, which imports
sounddevice (needs PortAudio, absent on a headless Linux server), and it plays
audio through a local sound card rather than a network socket. So the
building blocks are reused unchanged (DeepgramStream, llm.stream,
tts.chunk_stream, tts.speak_deepgram, metrics.Turn, PROFILES["v5_full"]) and
only the orchestration is rewritten here. Every step records the same timing
marks as pipeline.py and says which lines it mirrors, so the two can be
checked side by side. agent/ is not modified.

What "playback_start" means here: the server cannot hear the browser's
speaker, so it is the moment the first TTS audio frame was sent down the
websocket. Live records carry a "measurement" field saying so.
"""
import asyncio
import contextlib
import json
from typing import Awaitable, Callable

import httpx
import websockets

from agent import llm as llm_mod
from agent import stt as stt_mod
from agent import tts as tts_mod
from agent.config import MIC_BLOCK_BYTES, MIC_BLOCK_MS, MIC_SAMPLE_RATE, PROFILES
from agent.metrics import LOG_DIR, Turn, now

from .vad import EnergyVAD

PROFILE = PROFILES["v5_full"]
LOG_FILE = LOG_DIR / "live_web.jsonl"
MEASUREMENT = (
    "web_server: playback_start = first audio frame sent, playback_done = last "
    "audio frame sent; excludes browser network and playback"
)

# 100 ms of 24 kHz int16 per websocket frame: small enough that the browser
# can start playing early, large enough to keep per-message overhead low.
AUDIO_FRAME_BYTES = 4800

# Deepgram closes a streaming socket after ~10 s without audio. While the
# agent is talking we stop forwarding mic audio, so ping it instead.
KEEPALIVE_S = 3.0

SAFETY_NET_BLOCKS = 2500 // MIC_BLOCK_MS
NO_SPEECH_RESET_BYTES = 2 * MIC_SAMPLE_RATE * 15


class _Disconnected(Exception):
    """The browser went away mid-turn."""


class _DeepgramStream(stt_mod.DeepgramStream):
    async def keepalive(self):
        if self._ws:
            with contextlib.suppress(websockets.ConnectionClosed):
                await self._ws.send(json.dumps({"type": "KeepAlive"}))


def _strip_wav_header(audio: bytes) -> bytes:
    """Drop a RIFF/WAV header if the TTS response has one.

    The local Player would play those 44 bytes as a click; in the browser
    they would also be decoded as samples, so they are removed here.
    """
    if audio[:4] != b"RIFF":
        return audio
    i = audio.find(b"data", 12)
    return audio[i + 8:] if i >= 0 else audio


class _AudioOut:
    """Stands in for agent.audio_io.Player: same feed() role, but the
    "speaker" is the websocket."""

    def __init__(self, send_bytes: Callable[[bytes], Awaitable[None]]):
        self._send = send_bytes
        self._carry = b""
        self.first_send_ts: float | None = None

    async def feed(self, pcm: bytes):
        # int16 samples are 2 bytes; never split one across frames (same
        # odd-byte rule as Player, audio_io.py:~301).
        data = self._carry + pcm
        cut = len(data) - (len(data) % 2)
        data, self._carry = data[:cut], data[cut:]
        for i in range(0, len(data), AUDIO_FRAME_BYTES):
            await self._send(data[i:i + AUDIO_FRAME_BYTES])
            if self.first_send_ts is None:
                self.first_send_ts = now()


class Session:
    def __init__(self, send_text: Callable[[str], Awaitable[None]],
                 send_bytes: Callable[[bytes], Awaitable[None]]):
        self.p = PROFILE
        self._send_text_raw = send_text
        self._send_bytes_raw = send_bytes
        # The feeder (audio) and dispatcher (reply text) send concurrently;
        # one lock keeps websocket writes from interleaving.
        self._send_lock = asyncio.Lock()
        self.history: list[dict] = []
        self.client: httpx.AsyncClient | None = None
        self.dg: _DeepgramStream | None = None
        # One VAD per connection, not per turn: if it recalibrated each turn,
        # a user who starts talking right after a reply would calibrate it on
        # their own voice and it would never detect speech.
        self.vad = EnergyVAD()
        self.audio_in: asyncio.Queue = asyncio.Queue()
        self._partial = b""
        self.listening = False
        self._keepalive_task: asyncio.Task | None = None

    # ------------------------------------------------------------ transport
    async def send_json(self, obj: dict):
        async with self._send_lock:
            await self._send_text_raw(json.dumps(obj))

    async def _send_audio(self, frame: bytes):
        async with self._send_lock:
            await self._send_bytes_raw(frame)

    def feed(self, data: bytes):
        """Called by the server for every binary frame from the browser.

        Browsers send whatever frame size their audio API produces; re-block
        into the 20 ms blocks the VAD calibration and safety-net counts
        assume. Audio arriving while the agent is replying is dropped
        (half-duplex: no barge-in).
        """
        if not self.listening:
            self._partial = b""
            return
        ts = now()
        buf = self._partial + data
        n = len(buf) - (len(buf) % MIC_BLOCK_BYTES)
        for i in range(0, n, MIC_BLOCK_BYTES):
            self.audio_in.put_nowait((ts, buf[i:i + MIC_BLOCK_BYTES]))
        self._partial = buf[n:]

    def disconnect(self):
        self.audio_in.put_nowait(None)

    # ---------------------------------------------------------------- setup
    async def open(self):
        # Mirrors pipeline.py:28-45 (keep-alive pool + TLS warm-up).
        limits = httpx.Limits(max_keepalive_connections=10, max_connections=20,
                              keepalive_expiry=300.0)
        self.client = httpx.AsyncClient(limits=limits, http2=False)
        with contextlib.suppress(Exception):
            await self.client.get("https://api.groq.com/openai/v1/models", timeout=10.0)
        self._keepalive_task = asyncio.create_task(self._keepalive())

    async def close(self):
        if self._keepalive_task:
            self._keepalive_task.cancel()
        if self.dg:
            with contextlib.suppress(Exception):
                await self.dg.close()
            self.dg = None
        if self.client:
            await self.client.aclose()

    async def _keepalive(self):
        while True:
            await asyncio.sleep(KEEPALIVE_S)
            if not self.listening and self.dg is not None:
                await self.dg.keepalive()

    async def _ensure_dg(self):
        # Mirrors pipeline.py:54-68: connect before the user speaks, and only
        # assign self.dg after connect() succeeds, so a failed handshake
        # leaves it None and the next turn retries.
        if self.dg is None:
            dg = _DeepgramStream(endpointing_ms=self.p.silence_ms)
            await dg.connect()
            self.dg = dg

    # ------------------------------------------------------- listen + detect
    async def _listen(self, turn: Turn) -> bytes:
        """Mirrors pipeline.py:71-118, deepgram_endpointing branch."""
        # Leftovers from the previous turn (e.g. a late UtteranceEnd) must
        # not end this one.
        while not self.dg.events.empty():
            self.dg.events.get_nowait()
        self.dg.transcript_parts = []
        while not self.audio_in.empty():
            if self.audio_in.get_nowait() is None:
                raise _Disconnected

        pcm = bytearray()
        speech_started = False
        silent_blocks = 0
        last_speech_ts = None
        # Open the gate before announcing it, so a client that starts
        # streaming the instant it sees "listening" loses no audio.
        self.listening = True
        try:
            await self.send_json({"type": "listening"})
            while True:
                item = await self.audio_in.get()
                if item is None:
                    raise _Disconnected
                ts, block = item
                pcm.extend(block)
                await self.dg.send(block)

                if self.vad.is_speech(block):
                    speech_started = True
                    silent_blocks = 0
                    last_speech_ts = ts
                    turn.mark("speech_started", ts)
                elif speech_started:
                    silent_blocks += 1

                ev = self._poll_dg_event()
                if ev and ev["type"] in ("speech_final", "utterance_end") and self.dg.text():
                    turn.mark("eot_detected", ev["ts"])
                    turn.mark("stt_final", ev["ts"])
                    break
                if speech_started and silent_blocks > SAFETY_NET_BLOCKS:
                    turn.mark("eot_detected", ts)
                    break

                # pipeline.py:112 gives up after 15 s of no speech. A browser
                # mic stays open indefinitely, so just drop the silence to
                # bound memory and keep listening.
                if not speech_started and len(pcm) > NO_SPEECH_RESET_BYTES:
                    pcm.clear()
        finally:
            self.listening = False

        if last_speech_ts is None:
            # Deepgram heard words the local VAD did not; there is no
            # trustworthy speech_end_true, so flag the turn.
            turn.set(vad_missed_speech=True)
        turn.mark("speech_end_true", last_speech_ts or now())
        return bytes(pcm)

    def _poll_dg_event(self):
        try:
            return self.dg.events.get_nowait()
        except asyncio.QueueEmpty:
            return None

    # ------------------------------------------------------------------ STT
    async def _transcribe(self, turn: Turn) -> str:
        """Mirrors pipeline.py:127-142, deepgram_stream branch."""
        await self.dg.finalize()
        text = self.dg.text()
        deadline = now() + 1.5
        while not text and now() < deadline:
            try:
                ev = await asyncio.wait_for(self.dg.events.get(), timeout=0.2)
                if ev["type"] in ("final", "speech_final", "utterance_end"):
                    text = self.dg.text()
            except asyncio.TimeoutError:
                pass
        turn.mark("stt_final")
        self.dg.transcript_parts = []
        return text

    # -------------------------------------------------------------- speaking
    async def _respond(self, user_text: str, turn: Turn) -> str:
        """Mirrors pipeline.py:186-258 (Path B: streamed + chunked)."""
        self.history.append({"role": "user", "content": user_text})
        synth = tts_mod.synth(self.p.tts_provider)
        out = _AudioOut(self._send_audio)
        text_q: asyncio.Queue = asyncio.Queue()
        collected: list[str] = []

        async def token_source():
            async for tok in llm_mod.stream(self.p.llm_provider, self.p.llm_model,
                                            self.history, self.client, self.p.max_tokens):
                turn.mark("llm_first_token")
                collected.append(tok)
                yield tok
            turn.mark("llm_last_token")

        async def producer():
            try:
                async for chunk in tts_mod.chunk_stream(
                    token_source(),
                    first_min=self.p.first_chunk_min_chars,
                    rest_min=self.p.rest_chunk_min_chars,
                ):
                    await text_q.put(chunk)
            finally:
                await text_q.put(None)

        async def fetch_chunk_audio(chunk: str) -> bytes:
            turn.mark("tts_request_sent")
            audio = bytearray()
            async for b in synth(chunk, self.client, self.p.tts_model):
                turn.mark("tts_first_audio")
                audio.extend(b)
            if audio[:4] == b"RIFF":
                turn.set(tts_wav_header=True)
            return _strip_wav_header(bytes(audio))

        fetches: list[asyncio.Task] = []

        async def consumer():
            # Pipelined TTS, mirrors pipeline.py:218-248: the dispatcher
            # starts a TTS task per chunk the moment its text is ready
            # (pipeline.py:239); the feeder awaits those tasks strictly in
            # chunk order (pipeline.py:241-246), so playback order is kept
            # even when a later chunk's audio lands first.
            fetch_q: asyncio.Queue = asyncio.Queue()

            async def dispatcher():
                while True:
                    chunk = await text_q.get()
                    if chunk is None:
                        await fetch_q.put(None)
                        break
                    task = asyncio.create_task(fetch_chunk_audio(chunk))
                    fetches.append(task)
                    await fetch_q.put(task)
                    await self.send_json({"type": "reply_chunk", "text": chunk})

            async def feeder():
                while True:
                    task = await fetch_q.get()
                    if task is None:
                        break
                    await out.feed(await task)

            await asyncio.gather(dispatcher(), feeder())

        turn.mark("llm_request_sent")
        try:
            await asyncio.gather(producer(), consumer())
        finally:
            # If one chunk failed, don't leave its siblings downloading in
            # the background after the turn has ended.
            for t in fetches:
                t.cancel()
        turn.mark("playback_start", out.first_send_ts or now())
        turn.mark("playback_done", now())

        reply = "".join(collected).strip()
        self.history.append({"role": "assistant", "content": reply})
        return reply

    # ------------------------------------------------------------------ turn
    async def run_turn(self) -> Turn:
        """Mirrors pipeline.py:261-302: every failure is caught and recorded
        on the turn, so one bad turn cannot kill the connection."""
        turn = Turn(self.p.name, "live_web")
        try:
            await self._ensure_dg()
            pcm = await self._listen(turn)

            user_text = await self._transcribe(turn)
            if not user_text:
                turn.errors.append("empty_transcript")
                # Mirrors pipeline.py:275-285: a dead or wedged websocket
                # returns empty transcripts forever without raising, so tear
                # it down and let the next turn's _ensure_dg() reconnect.
                with contextlib.suppress(Exception):
                    await self.dg.close()
                self.dg = None
                return turn
            turn.set(user_text=user_text, audio_ms=round(len(pcm) / 2 / MIC_SAMPLE_RATE * 1000))
            await self.send_json({"type": "transcript", "text": user_text})

            reply = await self._respond(user_text, turn)
            turn.set(reply=reply, reply_chars=len(reply))
            await self.send_json({"type": "reply", "text": reply})
        except _Disconnected:
            raise
        except Exception as e:  # noqa: BLE001
            turn.errors.append(f"{type(e).__name__}: {e}")
        return turn

    async def run(self):
        """Turn loop for the lifetime of the connection."""
        await self.open()
        backoff = 1.0
        try:
            await self.send_json({"type": "ready", "profile": self.p.name})
            while True:
                try:
                    turn = await self.run_turn()
                except _Disconnected:
                    return
                self._save(turn)
                with contextlib.suppress(Exception):
                    await self.send_json({"type": "audio_end"})
                    await self.send_json({
                        "type": "metrics",
                        "turn_id": turn.id,
                        "metrics": turn.metrics(),
                        "critical_path": turn.critical_path(),
                        "errors": turn.errors,
                        "measurement": MEASUREMENT,
                    })
                if turn.errors and "eot_detected" not in turn.marks:
                    # Failed before the user could even speak (e.g. Deepgram
                    # rejected the handshake). Back off exponentially: a bad
                    # key will never fix itself, and retrying every second
                    # floods the log.
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 10.0)
                else:
                    backoff = 1.0
        finally:
            await self.close()

    def _save(self, turn: Turn):
        rec = turn.to_record()
        rec["measurement"] = MEASUREMENT
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
