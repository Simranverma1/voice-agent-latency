"""Text-to-speech backends + sentence chunking.

The chunker is the piece that converts "streaming LLM" into "user hears sound
sooner". Streaming tokens is worthless on its own if you still wait for the
full text before calling TTS.

Strategy: flush the FIRST chunk as soon as we have a clause boundary and ~24
characters, then use larger chunks afterwards. Small first chunk minimises
time-to-first-audio; larger later chunks keep prosody natural and reduce the
number of TTS round trips.
"""
import asyncio
import re
from typing import AsyncIterator

import httpx

from .config import DEEPGRAM_API_KEY, OPENAI_API_KEY, TTS_SAMPLE_RATE
from .metrics import now
from .net import TRANSIENT_STATUS

# A per-read timeout doesn't bound total time if a connection dribbles data
# slowly - each individual read can stay under the limit while the whole
# request crawls for minutes. Cap wall-clock time across the whole stream.
OVERALL_DEADLINE_S = 20.0

DG_SPEAK = "https://api.deepgram.com/v1/speak"
OA_SPEECH = "https://api.openai.com/v1/audio/speech"

# Boundaries we are willing to cut on, strongest first.
STRONG = re.compile(r"[.!?]['\")\]]?\s")
WEAK = re.compile(r"[,;:—]\s")


def _split_at_boundary(buf: str, min_chars: int) -> tuple[str | None, str]:
    """Return (chunk_to_speak, remainder) or (None, buf) if not ready yet."""
    if len(buf) < min_chars:
        return None, buf
    for pattern in (STRONG, WEAK):
        matches = list(pattern.finditer(buf))
        for m in matches:
            if m.end() >= min_chars:
                return buf[:m.end()].strip(), buf[m.end():]
    # Fallback: if the buffer has grown far past the target with no punctuation,
    # cut on a word boundary so we are not held hostage by a run-on sentence.
    if len(buf) > min_chars * 3:
        cut = buf.rfind(" ", 0, min_chars * 2)
        if cut > 0:
            return buf[:cut].strip(), buf[cut:]
    return None, buf


async def chunk_stream(token_iter: AsyncIterator[str], first_min: int = 24,
                       rest_min: int = 90) -> AsyncIterator[str]:
    """Turn a token stream into a stream of speakable chunks."""
    buf = ""
    first = True
    async for tok in token_iter:
        buf += tok
        while True:
            chunk, buf = _split_at_boundary(buf, first_min if first else rest_min)
            if chunk is None:
                break
            if chunk:
                yield chunk
                first = False
    if buf.strip():
        yield buf.strip()


# --------------------------------------------------------------------------
# Synthesis
# --------------------------------------------------------------------------
async def speak_deepgram(text: str, client: httpx.AsyncClient,
                         model: str = "aura-2-thalia-en") -> AsyncIterator[bytes]:
    """Deepgram Aura. Returns raw linear16 PCM, streamed as it is generated."""
    params = {"model": model, "encoding": "linear16", "sample_rate": str(TTS_SAMPLE_RATE)}
    attempts = 2
    for attempt in range(attempts):
        yielded_any = False
        start = now()
        try:
            async with client.stream(
                "POST", DG_SPEAK, params=params,
                headers={"Authorization": f"Token {DEEPGRAM_API_KEY}",
                         "Content-Type": "application/json"},
                json={"text": text}, timeout=15.0,
            ) as r:
                r.raise_for_status()
                async for chunk in r.aiter_bytes():
                    if chunk:
                        yielded_any = True
                        yield chunk
                    if now() - start > OVERALL_DEADLINE_S:
                        raise TimeoutError(f"TTS stream exceeded {OVERALL_DEADLINE_S}s overall")
            return
        except httpx.HTTPStatusError as e:
            if yielded_any or attempt == attempts - 1 or e.response.status_code not in TRANSIENT_STATUS:
                raise
            await asyncio.sleep(0.5)
        except (httpx.TimeoutException, httpx.ConnectError, TimeoutError):
            if yielded_any or attempt == attempts - 1:
                raise
            await asyncio.sleep(0.5)


async def speak_openai(text: str, client: httpx.AsyncClient,
                       model: str = "tts-1", voice: str = "alloy") -> AsyncIterator[bytes]:
    """OpenAI TTS. `pcm` format = raw 24 kHz mono s16le, no container to parse."""
    async with client.stream(
        "POST", OA_SPEECH,
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                 "Content-Type": "application/json"},
        json={"model": model, "voice": voice, "input": text, "response_format": "pcm"},
        timeout=15.0,
    ) as r:
        r.raise_for_status()
        async for chunk in r.aiter_bytes():
            if chunk:
                yield chunk


def synth(provider: str):
    return speak_deepgram if provider == "deepgram" else speak_openai
