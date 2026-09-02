"""Reproducible benchmark harness.

Replays the same 20 recorded prompts through one or more profiles and writes
every turn to logs/bench.jsonl.

    python benchmark.py --profiles v1_baseline v5_full
    python benchmark.py --profiles all --repeat 1
    python benchmark.py --profiles v5_full --mute      # no speaker output

--mute routes audio to a null sink so you can run 100 turns without listening
to all of them. Playback timing is still measured (the sound card is still
opened), so the numbers stay comparable.
"""
import argparse
import asyncio
import time
from pathlib import Path

from agent.audio_io import WavSource
from agent.config import PROFILES, get_profile
from agent.pipeline import VoiceAgent

DATA = Path(__file__).parent / "data"
LOGS = Path(__file__).parent / "logs"


async def run_profile(name: str, prompts: list[Path], repeat: int, logfile: str):
    profile = get_profile(name)
    agent = VoiceAgent(profile, verbose=False)
    await agent.open()
    ok = fail = 0
    print(f"\n=== {profile.name} ===  {profile.description}")
    try:
        for r in range(repeat):
            for i, wav in enumerate(prompts, 1):
                agent.history = []  # each turn is independent -> comparable
                source = WavSource(wav)
                turn = await agent.run_turn(source, prompt_id=wav.stem)
                turn.set(repeat=r)
                turn.save(logfile)
                m = turn.metrics()
                if turn.errors or m.get("e2e_ms") is None:
                    fail += 1
                    print(f"  {i:02d}/{len(prompts)}  FAILED  {turn.errors}")
                else:
                    ok += 1
                    print(f"  {i:02d}/{len(prompts)}  e2e={m['e2e_ms']:7.0f}ms  "
                          f"eot={m['eot_detection_ms'] or 0:5.0f}  "
                          f"stt={m['stt_ms'] or 0:5.0f}  "
                          f"ttft={m['llm_ttft_ms'] or 0:5.0f}  "
                          f"ttfa={m['tts_ttfa_ms'] or 0:5.0f}")
                await asyncio.sleep(0.4)  # let sockets settle between turns
    finally:
        await agent.close()
    print(f"  -> {ok} ok, {fail} failed")


async def main(profile_names: list[str], repeat: int, logfile: str):
    prompts = sorted(DATA.glob("prompt_*.wav"))
    if not prompts:
        raise SystemExit("No prompts found. Run: python make_prompts.py --mode tts")
    print(f"{len(prompts)} prompts x {repeat} repeat(s) x {len(profile_names)} profile(s) "
          f"= {len(prompts)*repeat*len(profile_names)} turns")
    t0 = time.time()
    for name in profile_names:
        await run_profile(name, prompts, repeat, logfile)
    print(f"\nDone in {time.time()-t0:.0f}s. Log: {LOGS/logfile}")
    print("Next:  python analyze.py")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--profiles", nargs="+", default=["v1_baseline", "v5_full"])
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--log", default="bench.jsonl")
    a = ap.parse_args()
    names = list(PROFILES) if a.profiles == ["all"] else a.profiles
    asyncio.run(main(names, a.repeat, a.log))
