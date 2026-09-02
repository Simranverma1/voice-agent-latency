"""Configuration + optimization profiles.

The whole point of this file: every optimization is a FLAG, not a rewrite.
That lets us run the same 20 audio prompts through different profiles and
attribute latency savings to individual changes.
"""
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")

# Audio format used everywhere on the input side
MIC_SAMPLE_RATE = 16000
MIC_BLOCK_MS = 20
MIC_BLOCK_SAMPLES = MIC_SAMPLE_RATE * MIC_BLOCK_MS // 1000
MIC_BLOCK_BYTES = MIC_BLOCK_SAMPLES * 2  # int16 mono

# Both OpenAI PCM and Deepgram Aura linear16 are requested at 24 kHz
TTS_SAMPLE_RATE = 24000

SYSTEM_PROMPT = (
    "You are a voice assistant. Reply in ONE or TWO short spoken sentences. "
    "No lists, no markdown, no emoji. Be direct and conversational."
)


@dataclass
class Profile:
    name: str
    description: str

    # --- End of turn detection ---
    # "fixed_silence"        : local RMS energy VAD, wait N ms of silence
    # "deepgram_endpointing" : Deepgram's server-side endpointer (speech_final)
    eot: str = "fixed_silence"
    silence_ms: int = 900

    # --- Speech to text ---
    # "openai_whisper" | "deepgram_batch" | "deepgram_stream"
    stt: str = "openai_whisper"

    # --- LLM ---
    llm_provider: str = "openai"        # "openai" | "groq"
    llm_model: str = "gpt-4o"
    llm_stream: bool = False
    max_tokens: int = 150

    # --- TTS ---
    tts_provider: str = "openai"        # "openai" | "deepgram"
    tts_model: str = "tts-1"
    tts_chunked: bool = False           # sentence-chunk the LLM stream
    first_chunk_min_chars: int = 24     # flush first chunk aggressively
    rest_chunk_min_chars: int = 90      # larger later chunks = better prosody


PROFILES = {
    # ---------------- BEFORE ----------------
    # No OpenAI usage anywhere in these profiles: OpenAI's paid endpoints
    # (Whisper, GPT-4o, tts-1) require a funded billing account with no free
    # tier, which isn't available here. Every profile below runs entirely on
    # Groq + Deepgram, both usable on free-tier keys. The LLM axis that used
    # to be "OpenAI gpt-4o -> Groq gpt-oss-20b" is reframed as "large/slower
    # gpt-oss-120b -> small/faster gpt-oss-20b", both free on Groq - the same
    # non-streaming-vs-streaming, model-size-vs-latency story, without a bill.
    "v1_baseline": Profile(
        name="v1_baseline",
        description="Fully sequential. Nothing streams. Larger, slower LLM. This is the 'before'.",
        eot="fixed_silence", silence_ms=1500,
        stt="deepgram_batch",
        llm_provider="groq", llm_model="openai/gpt-oss-120b", llm_stream=False,
        tts_provider="deepgram", tts_model="aura-2-thalia-en", tts_chunked=False,
    ),

    # ---------------- ABLATIONS (one change at a time) ----------------
    "v2_fast_llm": Profile(
        name="v2_fast_llm",
        description="v1 + swap the 120b LLM for the smaller/faster 20b one (still non-streaming).",
        eot="fixed_silence", silence_ms=1500,
        stt="deepgram_batch",
        llm_provider="groq", llm_model="openai/gpt-oss-20b", llm_stream=False,
        tts_provider="deepgram", tts_model="aura-2-thalia-en", tts_chunked=False,
    ),

    "v3_stream_llm_tts": Profile(
        name="v3_stream_llm_tts",
        description="v2 + stream LLM tokens, sentence-chunk into streaming TTS.",
        eot="fixed_silence", silence_ms=1500,
        stt="deepgram_batch",
        llm_provider="groq", llm_model="openai/gpt-oss-20b", llm_stream=True,
        tts_provider="deepgram", tts_model="aura-2-thalia-en", tts_chunked=True,
    ),

    "v4_stream_stt": Profile(
        name="v4_stream_stt",
        description="v3 + streaming STT (transcript ready the instant speech ends).",
        eot="fixed_silence", silence_ms=1500,
        stt="deepgram_stream",
        llm_provider="groq", llm_model="openai/gpt-oss-20b", llm_stream=True,
        tts_provider="deepgram", tts_model="aura-2-thalia-en", tts_chunked=True,
    ),

    # ---------------- AFTER ----------------
    "v5_full": Profile(
        name="v5_full",
        description="v4 + server-side endpointing (300ms) instead of 900ms silence timer.",
        eot="deepgram_endpointing", silence_ms=300,
        stt="deepgram_stream",
        llm_provider="groq", llm_model="openai/gpt-oss-20b", llm_stream=True,
        tts_provider="deepgram", tts_model="aura-2-thalia-en", tts_chunked=True,
    ),
}


def get_profile(name: str) -> Profile:
    if name not in PROFILES:
        raise SystemExit(f"Unknown profile '{name}'. Choose from: {', '.join(PROFILES)}")
    return PROFILES[name]
