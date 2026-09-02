"""Latency instrumentation.

Design principle: we do NOT time each stage with its own stopwatch.
We record ONE monotonic timeline of events per turn, then derive every
metric by subtraction. This guarantees the numbers are internally
consistent and lets us reason about overlap in the streaming pipeline.
"""
import json
import time
import uuid
from pathlib import Path
from typing import Optional

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)


def now() -> float:
    """Monotonic clock. Never use time.time() for latency."""
    return time.perf_counter()


class Turn:
    """One user utterance -> one agent reply."""

    def __init__(self, profile_name: str, prompt_id: str = ""):
        self.id = uuid.uuid4().hex[:8]
        self.profile = profile_name
        self.prompt_id = prompt_id
        self.marks: dict[str, float] = {}
        self.meta: dict = {}
        self.errors: list[str] = []

    # ---------- recording ----------
    def mark(self, name: str, ts: Optional[float] = None) -> float:
        """Record an event. First write wins (so 'first_token' stays first)."""
        if name not in self.marks:
            self.marks[name] = ts if ts is not None else now()
        return self.marks[name]

    def set(self, **kwargs):
        self.meta.update(kwargs)

    def _d(self, a: str, b: str) -> Optional[float]:
        """Milliseconds between mark a and mark b."""
        if a in self.marks and b in self.marks:
            return round((self.marks[b] - self.marks[a]) * 1000, 1)
        return None

    # ---------- derived metrics ----------
    def metrics(self) -> dict:
        return {
            # 1. How long after the user actually stopped talking did we notice?
            "eot_detection_ms": self._d("speech_end_true", "eot_detected"),

            # 2. Time from us deciding the turn is over until we hold a final transcript.
            #    Streaming STT makes this near zero because partials were already sent.
            "stt_ms": self._d("eot_detected", "stt_final"),

            # 3. LLM time to first token (network + queue + prefill).
            "llm_ttft_ms": self._d("llm_request_sent", "llm_first_token"),

            # 4. LLM total generation time.
            "llm_total_ms": self._d("llm_request_sent", "llm_last_token"),

            # 5. TTS time to first audio byte received.
            "tts_ttfa_ms": self._d("tts_request_sent", "tts_first_audio"),

            # 6. THE HEADLINE NUMBER: user stops speaking -> user hears sound.
            "e2e_ms": self._d("speech_end_true", "playback_start"),

            # --- supporting diagnostics ---
            "stt_to_llm_gap_ms": self._d("stt_final", "llm_request_sent"),
            "llm_first_token_to_tts_req_ms": self._d("llm_first_token", "tts_request_sent"),
            "tts_audio_to_speaker_ms": self._d("tts_first_audio", "playback_start"),
            "full_reply_spoken_ms": self._d("speech_end_true", "playback_done"),
        }

    def critical_path(self) -> dict:
        """The breakdown that actually sums to e2e (no double counting).

        In a streaming pipeline LLM generation and TTS overlap, so summing the
        raw stage timings overstates total. These segments are contiguous and
        non-overlapping by construction.
        """
        return {
            "1_eot_detection": self._d("speech_end_true", "eot_detected"),
            "2_stt_finalize": self._d("eot_detected", "stt_final"),
            "3_llm_dispatch": self._d("stt_final", "llm_request_sent"),
            "4_llm_ttft": self._d("llm_request_sent", "llm_first_token"),
            "5_text_buffering": self._d("llm_first_token", "tts_request_sent"),
            "6_tts_ttfa": self._d("tts_request_sent", "tts_first_audio"),
            "7_audio_to_speaker": self._d("tts_first_audio", "playback_start"),
        }

    def to_record(self) -> dict:
        t0 = min(self.marks.values()) if self.marks else 0.0
        return {
            "turn_id": self.id,
            "profile": self.profile,
            "prompt_id": self.prompt_id,
            "wall_clock": time.strftime("%Y-%m-%d %H:%M:%S"),
            "metrics": self.metrics(),
            "critical_path": self.critical_path(),
            "marks_rel_ms": {k: round((v - t0) * 1000, 1) for k, v in sorted(self.marks.items(), key=lambda x: x[1])},
            "meta": self.meta,
            "errors": self.errors,
        }

    def save(self, filename: str = "latency.jsonl"):
        with open(LOG_DIR / filename, "a", encoding="utf-8") as f:
            f.write(json.dumps(self.to_record()) + "\n")

    def pretty(self) -> str:
        m = self.metrics()
        def fmt(k):
            v = m.get(k)
            return f"{v:>7.0f}ms" if v is not None else "      --"
        return (
            f"  end-of-turn detect : {fmt('eot_detection_ms')}\n"
            f"  speech-to-text     : {fmt('stt_ms')}\n"
            f"  LLM TTFT           : {fmt('llm_ttft_ms')}\n"
            f"  LLM total          : {fmt('llm_total_ms')}\n"
            f"  TTS TTFA           : {fmt('tts_ttfa_ms')}\n"
            f"  ---------------------------------\n"
            f"  E2E (perceived)    : {fmt('e2e_ms')}"
        )
