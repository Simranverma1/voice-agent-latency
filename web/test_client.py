"""Browser stand-in: stream recorded prompts to the web server and check
that every turn comes back complete.

    uvicorn web.server:app --port 8000            # terminal 1
    python -m web.test_client 1 2 3               # terminal 2

Each prompt is streamed in real time (20 ms blocks, like WavSource), all on
one connection, so multi-turn conversation is exercised too. Every turn
costs real Deepgram + Groq credits; keep the list short.

Two independent e2e numbers per turn:
  server e2e  from the server's own timeline (metrics.e2e_ms): last speech
              block *arrived* -> first audio frame *sent*
  client e2e  from this script's clock: last loud block *sent* (rms > 300,
              WavSource's rule) -> first audio byte *received*
The difference is network transit plus the two sides' slightly different
definitions of "last speech block".
"""
import argparse
import asyncio
import json
import sys
import wave
from pathlib import Path

import websockets

from agent.config import MIC_BLOCK_BYTES, MIC_BLOCK_MS, MIC_SAMPLE_RATE, TTS_SAMPLE_RATE
from agent.metrics import now

from .vad import rms

DATA = Path(__file__).resolve().parent.parent / "data"
LEAD_IN_MS = 600       # same as WavSource: lets the server VAD calibrate on silence
TURN_TIMEOUT_S = 45.0
LOUD_RMS = 300         # WavSource's speech_end rule


def load_blocks(path: Path) -> list[bytes]:
    with wave.open(str(path), "rb") as w:
        assert w.getnchannels() == 1 and w.getframerate() == MIC_SAMPLE_RATE, path
        raw = w.readframes(w.getnframes())
    blocks = [raw[i:i + MIC_BLOCK_BYTES] for i in range(0, len(raw), MIC_BLOCK_BYTES)]
    return [b.ljust(MIC_BLOCK_BYTES, b"\x00") for b in blocks if b]


class Receiver:
    """Collects everything the server sends, per turn."""

    def __init__(self):
        self.events: asyncio.Queue = asyncio.Queue()
        self.reset()

    def reset(self):
        self.audio = bytearray()
        self.first_audio_ts: float | None = None
        self.audio_end = asyncio.Event()

    async def run(self, ws):
        async for msg in ws:
            if isinstance(msg, bytes):
                if self.first_audio_ts is None:
                    self.first_audio_ts = now()
                self.audio.extend(msg)
                continue
            ev = json.loads(msg)
            if ev["type"] == "audio_end":
                self.audio_end.set()
            await self.events.put(ev)


async def wait_for(rx: Receiver, etype: str, sink: dict) -> dict:
    """Wait for an event of `etype`, stashing the others in `sink` by type."""
    while True:
        ev = await rx.events.get()
        sink.setdefault(ev["type"], []).append(ev)
        if ev["type"] == etype:
            return ev
        if etype == "listening" and ev["type"] == "metrics" and ev["errors"]:
            # The server finished a turn without ever listening: it failed
            # during setup (e.g. Deepgram rejected the key) and will keep
            # failing. Stop instead of waiting forever.
            raise RuntimeError(f"server failed before listening: {ev['errors']}")


async def run_prompt(ws, rx: Receiver, prompt: int, save_dir: Path | None) -> dict:
    path = DATA / f"prompt_{prompt:02d}.wav"
    blocks = load_blocks(path)
    loud = [i for i, b in enumerate(blocks) if rms(b) > LOUD_RMS]
    last_loud = loud[-1] if loud else len(blocks) - 1
    seen: dict = {}

    await asyncio.wait_for(wait_for(rx, "listening", seen), timeout=TURN_TIMEOUT_S)
    rx.reset()
    silence = b"\x00" * MIC_BLOCK_BYTES
    period = MIC_BLOCK_MS / 1000.0
    t_next = now()
    speech_end_ts = None

    async def pace():
        nonlocal t_next
        t_next += period
        await asyncio.sleep(max(0.0, t_next - now()))

    for _ in range(LEAD_IN_MS // MIC_BLOCK_MS):
        await ws.send(silence)
        await pace()
    for i, b in enumerate(blocks):
        await ws.send(b)
        if i == last_loud:
            speech_end_ts = now()
        await pace()
    # A real mic keeps sending after the user stops; the server drops audio
    # once it starts replying, so this is harmless.
    deadline = now() + TURN_TIMEOUT_S
    while not rx.audio_end.is_set() and now() < deadline:
        await ws.send(silence)
        await pace()

    metrics_ev = await asyncio.wait_for(wait_for(rx, "metrics", seen), timeout=TURN_TIMEOUT_S)

    if save_dir and rx.audio:
        save_dir.mkdir(parents=True, exist_ok=True)
        with wave.open(str(save_dir / f"reply_{prompt:02d}.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(TTS_SAMPLE_RATE)
            w.writeframes(bytes(rx.audio))

    client_e2e = None
    if rx.first_audio_ts is not None and speech_end_ts is not None:
        client_e2e = round((rx.first_audio_ts - speech_end_ts) * 1000, 1)
    return {
        "prompt": prompt,
        "transcript": " ".join(e["text"] for e in seen.get("transcript", [])),
        "reply": " ".join(e["text"] for e in seen.get("reply", [])),
        "chunks": len(seen.get("reply_chunk", [])),
        "audio_bytes": len(rx.audio),
        "metrics": metrics_ev["metrics"],
        "critical_path": metrics_ev["critical_path"],
        "errors": metrics_ev["errors"],
        "measurement": metrics_ev.get("measurement"),
        "client_e2e": client_e2e,
    }


def check(r: dict) -> list[str]:
    fails = []
    if not r["transcript"]:
        fails.append("no transcript")
    if not r["reply"]:
        fails.append("no reply")
    if r["audio_bytes"] == 0:
        fails.append("no audio")
    if r["metrics"].get("e2e_ms") is None:
        fails.append("no e2e metric")
    if r["errors"]:
        fails.append(f"errors: {r['errors']}")
    return fails


def report(results: list[dict]) -> bool:
    ok = True
    for r in results:
        m, cp = r["metrics"], r["critical_path"]
        fails = check(r)
        ok &= not fails
        print(f"\n=== prompt_{r['prompt']:02d}  [{'PASS' if not fails else 'FAIL: ' + '; '.join(fails)}]")
        print(f"  you  : {r['transcript']}")
        print(f"  bot  : {r['reply']}")
        print(f"  audio: {r['audio_bytes']} bytes = {r['audio_bytes'] / 2 / TTS_SAMPLE_RATE:.2f} s "
              f"in {r['chunks']} TTS chunk(s)")
        print("  stages   : " + "  ".join(
            f"{k.removesuffix('_ms')}={v:.0f}" if v is not None else f"{k.removesuffix('_ms')}=--"
            for k, v in m.items() if k in ("eot_detection_ms", "stt_ms", "llm_ttft_ms",
                                           "llm_total_ms", "tts_ttfa_ms", "e2e_ms")))
        print("  critical : " + "  ".join(
            f"{k}={v:.0f}" if v is not None else f"{k}=--" for k, v in cp.items()))
        eot = m.get("eot_detection_ms")
        if eot is not None and eot < 0:
            print(f"  NOTE: eot_detection is negative ({eot:.0f} ms): Deepgram's endpoint "
                  f"event arrived before the local VAD's last speech block.")

    print("\n  server e2e = last speech block arrived -> first audio frame sent (server clock)")
    print("  client e2e = last loud block sent      -> first audio byte received (client clock)\n")
    print(f"  {'prompt':<10}{'server e2e':>12}{'client e2e':>12}{'client-server':>15}")
    for r in results:
        s, c = r["metrics"].get("e2e_ms"), r["client_e2e"]
        diff = f"{c - s:+.0f} ms" if s is not None and c is not None else "--"
        fmt = lambda v: f"{v:.0f} ms" if v is not None else "--"
        print(f"  prompt_{r['prompt']:02d} {fmt(s):>12}{fmt(c):>12}{diff:>15}")
    if results and results[0].get("measurement"):
        print(f"\n  measurement: {results[0]['measurement']}")
    return ok


async def main(url: str, prompts: list[int], save_dir: Path | None) -> int:
    async with websockets.connect(url, max_size=None) as ws:
        rx = Receiver()
        rx_task = asyncio.create_task(rx.run(ws))
        ready = await asyncio.wait_for(rx.events.get(), timeout=30)
        print(f"connected: {ready}")
        results = []
        for p in prompts:
            print(f"streaming prompt_{p:02d} ...", flush=True)
            results.append(await run_prompt(ws, rx, p, save_dir))
        rx_task.cancel()
    return 0 if report(results) else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("prompts", nargs="+", type=int, help="prompt numbers, e.g. 1 2 3")
    ap.add_argument("--url", default="ws://127.0.0.1:8000/ws")
    ap.add_argument("--save-audio", type=Path, default=None,
                    help="directory to write each reply as a WAV you can listen to")
    args = ap.parse_args()
    # The Windows console defaults to cp1252, which can't print characters
    # LLM replies often contain (e.g. U+2011 non-breaking hyphen).
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(asyncio.run(main(args.url, args.prompts, args.save_audio)))
