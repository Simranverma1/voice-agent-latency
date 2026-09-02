"""Build the fixed set of 20 test utterances used for every benchmark run.

  python make_prompts.py --mode record    (recommended - your real voice)
  python make_prompts.py --mode tts       (fast fallback - synthetic voice)
  python make_prompts.py --mode verify

Using the SAME audio for every profile is what makes the before/after
comparison valid. Recording at the mic's native rate and downsampling to
16 kHz here means the prompt files are always correct regardless of what
your sound card supports.
"""
import argparse
import asyncio
import sys
import wave
from pathlib import Path

import httpx
import numpy as np

from agent.audio_io import (MIC_DEVICE, MIC_NATIVE_RATE, mic_channels,
                            resample_to_16k, save_wav, to_mono)
from agent.config import DEEPGRAM_API_KEY, MIC_SAMPLE_RATE

DATA = Path(__file__).parent / "data"
DATA.mkdir(exist_ok=True)

PROMPTS = [
    "What is the capital of Japan?",
    "Can you tell me a fun fact about octopuses?",
    "How do I make a really good cup of coffee at home?",
    "What's the difference between a virus and a bacteria?",
    "Give me a quick tip for sleeping better at night.",
    "Why is the sky blue?",
    "What should I cook for dinner if I only have eggs and rice?",
    "How far away is the moon from the earth?",
    "Explain what an API is in one sentence.",
    "What's a good beginner exercise routine?",
    "Who wrote the book Pride and Prejudice?",
    "How long does it take to fly from London to New York?",
    "What's the best way to remember someone's name?",
    "Tell me something interesting about the Great Wall of China.",
    "How do noise cancelling headphones actually work?",
    "What is machine learning in simple terms?",
    "Suggest a good movie for a rainy afternoon.",
    "Why do cats purr?",
    "What's the tallest mountain in the world?",
    "How can I make my morning routine less chaotic?",
]


def _rms(block: np.ndarray) -> float:
    if block.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(block.astype(np.float64) ** 2)))


def trim_silence(pcm: bytes, threshold: int | None = None, block: int = 160) -> bytes:
    """Trim leading/trailing silence using an ADAPTIVE noise floor.

    A fixed threshold breaks in any room whose ambient noise happens to sit
    above it - nothing gets trimmed and the clip keeps seconds of dead air.
    That corrupts measurements, because WavSource treats the last block with
    signal as the ground-truth end of speech. So the threshold is derived from
    each recording's own quiet portion instead.
    """
    a = np.frombuffer(pcm, dtype=np.int16)
    if a.size == 0:
        return pcm
    n = a.size // block
    if n == 0:
        return pcm
    frames = a[: n * block].reshape(n, block).astype(np.float64)
    energy = np.sqrt(np.mean(frames * frames, axis=1))

    if threshold is None:
        floor = float(np.percentile(energy, 20))
        peak = float(np.percentile(energy, 95))
        thresh = max(floor * 3.0, peak * 0.15, 120.0)
    else:
        thresh = float(threshold)

    loud = np.flatnonzero(energy > thresh)
    if loud.size == 0:
        return pcm
    pad = 12  # 120 ms either side, so nothing is clipped
    start = max(0, int(loud[0]) - pad)
    end = min(n - 1, int(loud[-1]) + pad)
    return a[start * block:(end + 1) * block].tobytes()


def record_mode():
    import sounddevice as sd

    ch = mic_channels(MIC_DEVICE)
    print(f"\nRecording at {MIC_NATIVE_RATE} Hz, {ch}ch, on device {MIC_DEVICE}, "
          f"downmixing to mono and downsampling to {MIC_SAMPLE_RATE} Hz.")
    print("Press ENTER, speak the line, then press ENTER again.\n")

    for i, text in enumerate(PROMPTS, 1):
        path = DATA / f"prompt_{i:02d}.wav"
        if path.exists():
            print(f"[{i:02d}/20] exists, skipping: {path.name}")
            continue
        input(f'[{i:02d}/20] ENTER to start >> "{text}"')
        frames: list[bytes] = []
        try:
            stream = sd.RawInputStream(
                samplerate=MIC_NATIVE_RATE, channels=ch, dtype="int16",
                blocksize=MIC_NATIVE_RATE // 50, device=MIC_DEVICE,
                callback=lambda d, f, t, s: frames.append(bytes(d)),
            )
            stream.start()
        except Exception as e:
            sys.exit(f"\nCannot open microphone: {e}\nRun: python fix_mic.py")
        input("        speaking... ENTER when done ")
        stream.stop(); stream.close()

        mono = to_mono(b"".join(frames), ch)
        pcm16k = resample_to_16k(mono, MIC_NATIVE_RATE)
        pcm16k = trim_silence(pcm16k)
        level = _rms(np.frombuffer(pcm16k, dtype=np.int16))
        save_wav(path, pcm16k, MIC_SAMPLE_RATE)
        dur = len(pcm16k) / 2 / MIC_SAMPLE_RATE
        warn = "  <-- VERY QUIET, consider redoing" if level < 400 else ""
        print(f"        saved {path.name}  {dur:.1f}s  level={level:.0f}{warn}\n")


async def tts_mode():
    if not DEEPGRAM_API_KEY:
        sys.exit("DEEPGRAM_API_KEY is required for --mode tts")
    voices = ["aura-2-thalia-en", "aura-2-orion-en", "aura-2-luna-en", "aura-2-arcas-en"]
    async with httpx.AsyncClient() as client:
        for i, text in enumerate(PROMPTS, 1):
            path = DATA / f"prompt_{i:02d}.wav"
            if path.exists():
                print(f"[{i:02d}/20] exists, skipping")
                continue
            r = await client.post(
                "https://api.deepgram.com/v1/speak",
                params={"model": voices[i % len(voices)], "encoding": "linear16",
                        "sample_rate": str(MIC_SAMPLE_RATE)},
                headers={"Authorization": f"Token {DEEPGRAM_API_KEY}",
                         "Content-Type": "application/json"},
                json={"text": text}, timeout=60.0,
            )
            r.raise_for_status()
            pcm = trim_silence(r.content)
            save_wav(path, pcm, MIC_SAMPLE_RATE)
            print(f"[{i:02d}/20] {path.name}  ({len(pcm)/2/MIC_SAMPLE_RATE:.1f}s)  {text}")


def verify():
    files = sorted(DATA.glob("prompt_*.wav"))
    print(f"\n{len(files)} prompt files in {DATA}")
    bad = 0
    for f in files:
        with wave.open(str(f), "rb") as w:
            dur = w.getnframes() / w.getframerate()
            pcm = w.readframes(w.getnframes())
            ok = w.getnchannels() == 1 and w.getframerate() == MIC_SAMPLE_RATE
        level = _rms(np.frombuffer(pcm, dtype=np.int16))
        flag = "OK" if ok and level > 250 else "CHECK"
        bad += flag == "CHECK"
        print(f"  {f.name}  {dur:5.2f}s  level={level:6.0f}  {flag}")
    if len(files) < 20:
        print(f"\n  Only {len(files)}/20 recorded. Re-run to continue where you left off.")
    elif bad:
        print(f"\n  {bad} file(s) look too quiet. Delete them and re-record those.")
    else:
        print("\n  All 20 good. Next: python demo.py --profile v5_full")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["record", "tts", "verify"], default="tts")
    a = ap.parse_args()
    if a.mode == "record":
        record_mode()
    elif a.mode == "tts":
        asyncio.run(tts_mode())
    verify()
