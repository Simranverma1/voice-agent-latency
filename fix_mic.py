"""Microphone diagnostic.

    python fix_mic.py

Tries every input device at every plausible sample rate, records a short clip
from each working combination, and reports the signal level. Tells you exactly
what to put in .env.
"""
import os
import sys

import numpy as np
import sounddevice as sd
from dotenv import load_dotenv

load_dotenv()

RATES = [16000, 44100, 48000, 32000, 22050, 8000]


def show_devices():
    print("\n--- ALL DEVICES ---")
    try:
        apis = sd.query_hostapis()
    except Exception:
        apis = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            api = apis[d["hostapi"]]["name"] if apis else "?"
            print(f"  [{i:2d}] IN  {d['max_input_channels']}ch "
                  f"default_sr={int(d['default_samplerate']):6d}  "
                  f"({api})  {d['name']}")
    print(f"\n  sounddevice default device pair: {sd.default.device}")


def probe(idx: int, rate: int) -> bool:
    """Can we open this device at this rate?"""
    try:
        sd.check_input_settings(device=idx, channels=1, samplerate=rate, dtype="int16")
        return True
    except Exception:
        return False


def level_test(idx: int, rate: int, seconds: float = 1.5) -> int | None:
    try:
        print(f"       recording {seconds}s at {rate} Hz - SAY SOMETHING...")
        rec = sd.rec(int(rate * seconds), samplerate=rate, channels=1,
                     dtype="int16", device=idx)
        sd.wait()
        return int(np.sqrt(np.mean(rec.astype(np.float64) ** 2)))
    except Exception as e:
        print(f"       failed: {e}")
        return None


def main():
    show_devices()

    inputs = [(i, d) for i, d in enumerate(sd.query_devices())
              if d["max_input_channels"] > 0]
    if not inputs:
        sys.exit("\nNo input devices at all. Check Windows Settings > Privacy > "
                 "Microphone > 'Let desktop apps access your microphone'.")

    print("\n--- PROBING (which device/rate combinations can open) ---")
    working: list[tuple[int, int, str]] = []
    for idx, d in inputs:
        rates = [r for r in RATES if probe(idx, r)]
        status = ", ".join(str(r) for r in rates) if rates else "NONE"
        print(f"  [{idx:2d}] {d['name'][:45]:45s} -> {status}")
        for r in rates:
            working.append((idx, r, d["name"]))

    if not working:
        print("\nNothing can be opened. Try, in order:")
        print("  1. Windows Settings > System > Sound > Input: pick your mic, "
              "click it, set 'Allow apps to use this device'")
        print("  2. Windows Settings > Privacy & security > Microphone: turn ON "
              "'Microphone access' AND 'Let desktop apps access your microphone'")
        print("  3. Close Zoom/Teams/Discord/OBS - they can hold exclusive access")
        print("  4. Device Manager > Audio inputs > right-click mic > Enable")
        return

    print("\n--- LEVEL TEST (real microphones only) ---")
    best = None
    seen = set()
    for idx, rate, name in working:
        if idx in seen:
            continue
        seen.add(idx)
        if "stereo mix" in name.lower() or "loopback" in name.lower():
            print(f"  [{idx}] {name} - SKIPPED (loopback, records your speakers)")
            continue
        print(f"  [{idx}] {name}")
        rates_for_dev = [r for i, r, _ in working if i == idx]
        rate = 16000 if 16000 in rates_for_dev else rates_for_dev[0]
        lvl = level_test(idx, rate)
        if lvl is None:
            continue
        verdict = "GOOD" if lvl > 500 else ("WEAK - raise mic volume" if lvl > 120
                                            else "SILENT - wrong device or muted")
        print(f"       level = {lvl}  ->  {verdict}\n")
        if lvl > 120 and (best is None or lvl > best[2]):
            best = (idx, rate, lvl)

    print("=" * 62)
    if best:
        idx, rate, lvl = best
        print(f"  USE THIS. Put in your .env file:\n")
        print(f"      MIC_DEVICE={idx}")
        if rate != 16000:
            print(f"      MIC_NATIVE_RATE={rate}")
            print(f"\n  Note: this device cannot do 16000 Hz natively, so audio")
            print(f"  will be captured at {rate} Hz and downsampled.")
        print(f"\n  Then re-run: python check_setup.py")
    else:
        print("  No device produced signal. Devices open fine, but nothing is")
        print("  reaching them - so this is a Windows settings issue:")
        print("    Settings > System > Sound > Input > raise volume to 100")
        print("    Settings > Privacy > Microphone > allow desktop apps")
        print("    Check the mic is not physically muted (headset inline switch)")
    print("=" * 62)


if __name__ == "__main__":
    main()
