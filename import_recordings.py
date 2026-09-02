"""Import recordings made on a phone (or any other device) as the 20 prompts.

Two ways to use it:

  A) TWENTY SEPARATE FILES  (simplest, recommended)
     Record 20 voice memos in order, copy them into a folder, then:
         python import_recordings.py --dir "C:\\Users\\user\\Desktop\\memos"

  B) ONE LONG RECORDING     (fastest to record)
     Say all 20 lines in one memo, pausing ~1.5 s between each, then:
         python import_recordings.py --split "C:\\Users\\user\\Desktop\\all.m4a"

Everything is converted to the required format: mono, 16 kHz, 16-bit WAV,
with leading/trailing silence trimmed so end-of-speech is unambiguous.

Requires ffmpeg for anything that is not already a WAV file:
    winget install --id Gyan.FFmpeg -e
(then close and reopen PowerShell)
"""
import argparse
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

from agent.audio_io import save_wav
from agent.config import MIC_SAMPLE_RATE
from make_prompts import PROMPTS, trim_silence

DATA = Path(__file__).parent / "data"
DATA.mkdir(exist_ok=True)

AUDIO_EXT = {".m4a", ".mp3", ".wav", ".aac", ".ogg", ".opus", ".flac",
             ".mp4", ".caf", ".wma", ".amr", ".3gp"}


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def decode_to_16k_mono(src: Path) -> bytes:
    """Any audio file -> raw 16 kHz mono int16 PCM bytes."""
    if src.suffix.lower() == ".wav":
        try:
            with wave.open(str(src), "rb") as w:
                if w.getnchannels() == 1 and w.getframerate() == MIC_SAMPLE_RATE:
                    return w.readframes(w.getnframes())
        except Exception:
            pass
    if not have_ffmpeg():
        sys.exit(
            f"\n{src.name} needs conversion but ffmpeg is not installed.\n\n"
            "  winget install --id Gyan.FFmpeg -e\n\n"
            "Then CLOSE and REOPEN PowerShell, reactivate the venv, and retry.\n"
            "Alternative: re-export your recordings as WAV and use those."
        )
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "out.wav"
        r = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
             "-ac", "1", "-ar", str(MIC_SAMPLE_RATE), "-sample_fmt", "s16",
             "-af", "highpass=f=80,dynaudnorm=p=0.9:s=5", str(out)],
            capture_output=True, text=True,
        )
        if r.returncode != 0 or not out.exists():
            sys.exit(f"ffmpeg failed on {src.name}:\n{r.stderr[:500]}")
        with wave.open(str(out), "rb") as w:
            return w.readframes(w.getnframes())


def level(pcm: bytes) -> float:
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    return float(np.sqrt(np.mean(a * a))) if a.size else 0.0


def write_prompt(i: int, pcm: bytes) -> None:
    pcm = trim_silence(pcm)
    path = DATA / f"prompt_{i:02d}.wav"
    save_wav(path, pcm, MIC_SAMPLE_RATE)
    dur = len(pcm) / 2 / MIC_SAMPLE_RATE
    lvl = level(pcm)
    flag = ""
    if dur < 0.6:
        flag = "  <-- SUSPICIOUSLY SHORT"
    elif dur > 8:
        flag = "  <-- SUSPICIOUSLY LONG"
    elif lvl < 300:
        flag = "  <-- VERY QUIET"
    print(f"  prompt_{i:02d}.wav  {dur:5.2f}s  level={lvl:6.0f}{flag}")
    print(f"       expected: \"{PROMPTS[i-1]}\"")


# ------------------------------------------------------------------ mode A
def import_dir(folder: Path, order: str):
    files = sorted([f for f in folder.iterdir()
                    if f.is_file() and f.suffix.lower() in AUDIO_EXT],
                   key=(lambda f: f.stat().st_mtime) if order == "time" else (lambda f: f.name.lower()))
    if not files:
        sys.exit(f"No audio files found in {folder}")

    print(f"\nFound {len(files)} audio file(s), ordered by {order}:")
    for i, f in enumerate(files[:25], 1):
        print(f"  {i:2d}. {f.name}")
    if len(files) != 20:
        print(f"\nWARNING: expected 20 files, found {len(files)}.")
    if input("\nIs this the right order? [y/N] ").strip().lower() != "y":
        sys.exit("Aborted. Rename your files 01.m4a, 02.m4a ... and use --order name")

    print()
    for i, f in enumerate(files[:20], 1):
        write_prompt(i, decode_to_16k_mono(f))


# ------------------------------------------------------------------ mode B
def split_one(src: Path, min_gap_ms: int, thresh: int, expected: int = 20):
    pcm = decode_to_16k_mono(src)
    a = np.frombuffer(pcm, dtype=np.int16)
    block = 160  # 10 ms
    n = a.size // block
    frames = a[: n * block].reshape(n, block)
    energy = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))

    floor = float(np.percentile(energy, 20))
    cut = max(float(thresh), floor * 4)
    voiced = energy > cut
    gap_blocks = min_gap_ms // 10

    segments, start, silence = [], None, 0
    for i, v in enumerate(voiced):
        if v:
            if start is None:
                start = i
            silence = 0
        elif start is not None:
            silence += 1
            if silence >= gap_blocks:
                segments.append((start, i - silence))
                start, silence = None, 0
    if start is not None:
        segments.append((start, len(voiced) - 1))

    segments = [(s, e) for s, e in segments if (e - s) * 10 >= 400]
    total = len(a) / MIC_SAMPLE_RATE
    print(f"\n{src.name}: {total:.1f}s, threshold={cut:.0f}, "
          f"found {len(segments)} segment(s)")

    if len(segments) != expected:
        print(f"\nExpected {expected} segments but found {len(segments)}.")
        print("Adjust and retry:")
        print(f"  too many  -> increase the gap:  --gap {min_gap_ms + 400}")
        print(f"  too few   -> decrease the gap:  --gap {max(300, min_gap_ms - 300)}")
        print(f"  noisy room-> raise the floor:   --thresh {int(cut * 1.6)}")
        for i, (s, e) in enumerate(segments[:30], 1):
            print(f"    {i:2d}. {s*10/1000:6.2f}s - {e*10/1000:6.2f}s")
        if input("\nProceed anyway with what was found? [y/N] ").strip().lower() != "y":
            return

    print()
    for i, (s, e) in enumerate(segments[:expected], 1):
        pad = 15  # keep 150 ms either side so nothing is clipped
        s = max(0, s - pad); e = min(len(voiced) - 1, e + pad)
        write_prompt(i, a[s * block:(e + 1) * block].tobytes())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", help="folder containing 20 separate recordings")
    ap.add_argument("--split", help="one long recording to auto-split")
    ap.add_argument("--order", choices=["time", "name"], default="time",
                    help="how to order files in --dir mode")
    ap.add_argument("--gap", type=int, default=900,
                    help="silence in ms that separates utterances (--split mode)")
    ap.add_argument("--thresh", type=int, default=250,
                    help="energy threshold for speech (--split mode)")
    a = ap.parse_args()

    if a.dir:
        import_dir(Path(a.dir), a.order)
    elif a.split:
        split_one(Path(a.split), a.gap, a.thresh)
    else:
        ap.error("give either --dir or --split")

    print("\nNow verify:  python make_prompts.py --mode verify")
