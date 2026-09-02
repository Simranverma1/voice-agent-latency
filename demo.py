"""Live microphone demo. This is the 'working demo' deliverable.

    python demo.py                      # optimized pipeline
    python demo.py --profile v1_baseline    # feel how bad the baseline is
    python demo.py --devices            # list audio devices

Wear headphones, otherwise the mic will hear the agent and talk to itself.
"""
import argparse
import asyncio

from agent.audio_io import MicSource, list_devices
from agent.config import get_profile
from agent.pipeline import VoiceAgent


async def main(profile_name: str, turns: int, device):
    profile = get_profile(profile_name)
    agent = VoiceAgent(profile)
    await agent.open()

    print(f"\n=== profile: {profile.name} ===")
    print(f"{profile.description}\n")
    print("Speak when you see 'listening'. Ctrl+C to quit.\n")

    try:
        for i in range(turns):
            print(f"[turn {i+1}] listening...")
            source = MicSource(device=device)
            turn = await agent.run_turn(source, prompt_id="live")
            if turn.errors:
                print(f"  (skipped: {turn.errors})")
                continue
            print(turn.pretty())
            turn.save("live.jsonl")
            print()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        await agent.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="v5_full")
    ap.add_argument("--turns", type=int, default=20)
    ap.add_argument("--device", default=None)
    ap.add_argument("--devices", action="store_true")
    a = ap.parse_args()
    if a.devices:
        list_devices()
    else:
        dev = int(a.device) if a.device and a.device.isdigit() else a.device
        asyncio.run(main(a.profile, a.turns, dev))
