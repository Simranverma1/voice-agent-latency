"""The orchestrator.

One class runs every profile. Each optimization is a branch, not a fork, so
the measurement code is identical across all runs.
"""
import asyncio
import contextlib

import httpx

from . import llm as llm_mod
from . import stt as stt_mod
from . import tts as tts_mod
from .audio_io import EnergyVAD, Player, MicSource, WavSource
from .config import MIC_BLOCK_MS, TTS_SAMPLE_RATE, Profile
from .metrics import Turn, now


class VoiceAgent:
    def __init__(self, profile: Profile, verbose: bool = True):
        self.p = profile
        self.verbose = verbose
        self.history: list[dict] = []
        self.client: httpx.AsyncClient | None = None
        self.dg: stt_mod.DeepgramStream | None = None

    # ----------------------------------------------------------------- setup
    async def open(self):
        # Persistent connection pool with keep-alive. Re-establishing TLS on
        # every turn costs 100-300 ms; this removes it from the hot path.
        limits = httpx.Limits(max_keepalive_connections=10, max_connections=20,
                              keepalive_expiry=300.0)
        self.client = httpx.AsyncClient(limits=limits, http2=False)
        await self._warm()

    async def _warm(self):
        """Pre-open TLS connections so turn #1 is not an outlier."""
        targets = []
        if self.p.llm_provider == "groq":
            targets.append("https://api.groq.com/openai/v1/models")
        else:
            targets.append("https://api.openai.com/v1/models")
        for url in targets:
            with contextlib.suppress(Exception):
                await self.client.get(url, timeout=10.0)

    async def close(self):
        if self.dg:
            await self.dg.close()
            self.dg = None
        if self.client:
            await self.client.aclose()

    async def _ensure_dg(self):
        """Open the streaming STT socket BEFORE the user speaks.

        Connection setup is ~150-400 ms. Doing it lazily would charge that to
        the user's turn; doing it ahead of time makes it free.
        """
        if self.p.stt != "deepgram_stream":
            return
        if self.dg is None:
            dg = stt_mod.DeepgramStream(endpointing_ms=self.p.silence_ms)
            await dg.connect()
            # Only assign after connect() succeeds - if it raises, self.dg
            # stays None so the next turn retries instead of being stuck
            # with a half-initialized connection forever.
            self.dg = dg

    # ------------------------------------------------------- listen + detect
    async def _listen(self, source, turn: Turn) -> bytes:
        """Consume audio until end-of-turn. Returns the captured PCM."""
        vad = EnergyVAD()
        pcm = bytearray()
        speech_started = False
        silent_blocks = 0
        last_speech_ts = None
        needed = max(1, self.p.silence_ms // MIC_BLOCK_MS)
        use_dg_endpoint = self.p.eot == "deepgram_endpointing" and self.dg is not None

        async for ts, block in source.blocks():
            pcm.extend(block)
            if self.dg is not None:
                await self.dg.send(block)

            speaking = vad.is_speech(block)
            if speaking:
                speech_started = True
                silent_blocks = 0
                last_speech_ts = ts
                turn.mark("speech_started", ts)
            elif speech_started:
                silent_blocks += 1

            if use_dg_endpoint:
                # Server-side endpointer decides. Drain any events it produced.
                ev = await self._poll_dg_event()
                if ev and ev["type"] in ("speech_final", "utterance_end") and self.dg.text():
                    turn.mark("eot_detected", ev["ts"])
                    turn.mark("stt_final", ev["ts"])  # transcript arrives with the signal
                    break
                # Safety net so a missed endpoint event cannot hang the turn.
                if speech_started and silent_blocks > (2500 // MIC_BLOCK_MS):
                    turn.mark("eot_detected", ts)
                    break
            else:
                if speech_started and silent_blocks >= needed:
                    turn.mark("eot_detected", ts)
                    break

            # Nothing said at all after a long wait -> give up (live mode only).
            if not speech_started and len(pcm) > 2 * 16000 * 15:
                return b""

        # Ground truth for "user stopped speaking".
        truth = getattr(source, "speech_end_ts", None) or last_speech_ts or now()
        turn.mark("speech_end_true", truth)
        return bytes(pcm)

    async def _poll_dg_event(self):
        try:
            return self.dg.events.get_nowait()
        except asyncio.QueueEmpty:
            return None

    # ------------------------------------------------------------------ STT
    async def _transcribe(self, pcm: bytes, turn: Turn) -> str:
        if self.p.stt == "deepgram_stream":
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
            # Reset for the next utterance and keep the socket warm.
            self.dg.transcript_parts = []
            return text

        if self.p.stt == "deepgram_batch":
            text = await stt_mod.transcribe_deepgram(pcm, self.client)
        else:
            text = await stt_mod.transcribe_openai(pcm, self.client)
        turn.mark("stt_final")
        return text

    # -------------------------------------------------------------- speaking
    async def _respond(self, user_text: str, turn: Turn, player: Player) -> str:
        self.history.append({"role": "user", "content": user_text})
        synth = tts_mod.synth(self.p.tts_provider)

        # ---------- Path A: fully sequential (the baseline) ----------
        if not self.p.llm_stream or not self.p.tts_chunked:
            turn.mark("llm_request_sent")
            if self.p.llm_stream:
                parts = []
                async for tok in llm_mod.stream(self.p.llm_provider, self.p.llm_model,
                                                self.history, self.client, self.p.max_tokens):
                    turn.mark("llm_first_token")
                    parts.append(tok)
                reply = "".join(parts).strip()
            else:
                reply = await llm_mod.complete(self.p.llm_provider, self.p.llm_model,
                                               self.history, self.client, self.p.max_tokens)
                turn.mark("llm_first_token")
            turn.mark("llm_last_token")

            turn.mark("tts_request_sent")
            audio = bytearray()
            async for b in synth(reply, self.client, self.p.tts_model):
                turn.mark("tts_first_audio")
                audio.extend(b)
            # Baseline behaviour: nothing plays until the whole file exists.
            player.start()
            player.feed(bytes(audio))
            player.finish(wait=True)
            turn.mark("playback_start", player.first_write_ts or now())
            turn.mark("playback_done", player.done_ts or now())
            self.history.append({"role": "assistant", "content": reply})
            return reply

        # ---------- Path B: streamed + chunked (the optimization) ----------
        player.start()
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

        async def consumer():
            while True:
                chunk = await text_q.get()
                if chunk is None:
                    break
                turn.mark("tts_request_sent")
                async for b in synth(chunk, self.client, self.p.tts_model):
                    turn.mark("tts_first_audio")
                    player.feed(b)

        turn.mark("llm_request_sent")
        await asyncio.gather(producer(), consumer())
        player.finish(wait=True)
        turn.mark("playback_start", player.first_write_ts or now())
        turn.mark("playback_done", player.done_ts or now())

        reply = "".join(collected).strip()
        self.history.append({"role": "assistant", "content": reply})
        return reply

    # ------------------------------------------------------------------ turn
    async def run_turn(self, source, prompt_id: str = "") -> Turn:
        turn = Turn(self.p.name, prompt_id)
        player = Player(samplerate=TTS_SAMPLE_RATE)
        try:
            # A slow/failed connect here (e.g. a websocket handshake timeout)
            # must fail only this turn, not escape and kill the whole run.
            await self._ensure_dg()
            await source.start()
            pcm = await self._listen(source, turn)
            if not pcm:
                turn.errors.append("no_speech_detected")
                return turn

            user_text = await self._transcribe(pcm, turn)
            if not user_text:
                turn.errors.append("empty_transcript")
                if self.p.stt == "deepgram_stream" and self.dg is not None:
                    # A dead or wedged websocket produces empty transcripts
                    # forever with no exception to catch. Tear it down so the
                    # next turn's _ensure_dg() opens a fresh connection rather
                    # than every remaining turn in the run failing the same way.
                    with contextlib.suppress(Exception):
                        await self.dg.close()
                    self.dg = None
                return turn
            turn.set(user_text=user_text, audio_ms=round(len(pcm) / 2 / 16000 * 1000))
            if self.verbose:
                print(f"  you : {user_text}")

            reply = await self._respond(user_text, turn, player)
            turn.set(reply=reply, reply_chars=len(reply))
            if self.verbose:
                print(f"  bot : {reply}")
        except Exception as e:  # noqa: BLE001
            turn.errors.append(f"{type(e).__name__}: {e}")
            if self.verbose:
                print(f"  !! {type(e).__name__}: {e}")
        finally:
            source.stop()
            with contextlib.suppress(Exception):
                player.abort()
        return turn
