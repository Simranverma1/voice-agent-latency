"""Audio input/output.

Two audio SOURCES that look identical to the rest of the pipeline:
  - MicSource : live microphone (for the demo)
  - WavSource : replays a recorded WAV in real time (for reproducible benchmarks)

Notes:
  * Python 3.13+ removed the stdlib `audioop` module, so all PCM maths here is
    done with numpy.
  * Many Windows mics (Realtek via WDM-KS) refuse 16 kHz. We capture at the
    device's native rate and downsample to 16 kHz ourselves, because the STT
    providers expect 16 kHz.

Set in .env:
    MIC_DEVICE=8            device index from fix_mic.py
    MIC_NATIVE_RATE=48000   capture rate; must be a whole multiple of 16000
"""
import asyncio
import os
import queue
import threading
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd

from .config import MIC_BLOCK_BYTES, MIC_BLOCK_MS, MIC_SAMPLE_RATE, TTS_SAMPLE_RATE
from .metrics import now

_dev = os.getenv("MIC_DEVICE")
MIC_DEVICE = int(_dev) if _dev not in (None, "") else None
MIC_NATIVE_RATE = int(os.getenv("MIC_NATIVE_RATE", MIC_SAMPLE_RATE))

if MIC_NATIVE_RATE % MIC_SAMPLE_RATE != 0:
    raise SystemExit(
        f"MIC_NATIVE_RATE={MIC_NATIVE_RATE} must be a whole multiple of "
        f"{MIC_SAMPLE_RATE}. Use 48000, not 44100."
    )
DECIMATION = MIC_NATIVE_RATE // MIC_SAMPLE_RATE


def mic_channels(device=None) -> int:
    """How many input channels this device insists on.

    WDM-KS devices refuse to open with a channel count other than their native
    one, so we must open at the hardware count and downmix to mono ourselves.
    """
    try:
        info = sd.query_devices(device if device is not None else MIC_DEVICE, "input")
        return max(1, int(info["max_input_channels"]))
    except Exception:
        return 1


def to_mono(pcm: bytes, channels: int) -> bytes:
    """Interleaved multi-channel int16 -> mono int16 by averaging channels."""
    if channels <= 1:
        return pcm
    a = np.frombuffer(pcm, dtype=np.int16)
    usable = (a.size // channels) * channels
    if usable == 0:
        return b""
    a = a[:usable].reshape(-1, channels).astype(np.float32).mean(axis=1)
    return np.clip(a, -32768, 32767).astype(np.int16).tobytes()


# ---------------------------------------------------------------- PCM helpers
def rms(pcm: bytes) -> float:
    """RMS energy of int16 PCM bytes (numpy replacement for audioop.rms)."""
    if not pcm:
        return 0.0
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    if a.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(a * a)))


def _lowpass_kernel(factor: int, taps: int = 63) -> np.ndarray:
    """Windowed-sinc anti-aliasing filter, cutoff at nyquist/factor."""
    n = np.arange(taps) - (taps - 1) / 2.0
    h = np.sinc(n / factor) * np.hamming(taps)
    return (h / h.sum()).astype(np.float32)


class Downsampler:
    """Streaming integer-factor decimator with continuous filter state.

    Filtering before decimation is not optional: without it, energy above
    8 kHz folds back into the speech band as aliasing and degrades the
    transcript. State carries across blocks so there is no discontinuity
    at block seams.
    """

    def __init__(self, factor: int, taps: int = 63):
        self.factor = factor
        self.h = _lowpass_kernel(factor, taps)
        self.tail = np.zeros(taps - 1, dtype=np.float32)
        self.phase = 0

    def process(self, pcm: bytes) -> bytes:
        if self.factor == 1:
            return pcm
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        if x.size == 0:
            return b""
        buf = np.concatenate([self.tail, x])
        y = np.convolve(buf, self.h, mode="valid")
        self.tail = buf[-(len(self.h) - 1):]
        idx = np.arange(self.phase, len(y), self.factor)
        out = y[idx]
        self.phase = self.phase + self.factor * len(idx) - len(y)
        return np.clip(out, -32768, 32767).astype(np.int16).tobytes()


def resample_to_16k(pcm: bytes, src_rate: int) -> bytes:
    """One-shot downsample of a complete buffer (used by make_prompts.py)."""
    if src_rate == MIC_SAMPLE_RATE:
        return pcm
    if src_rate % MIC_SAMPLE_RATE == 0:
        return Downsampler(src_rate // MIC_SAMPLE_RATE).process(pcm)
    # Non-integer ratio: band-limit then linearly interpolate.
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    factor = src_rate / MIC_SAMPLE_RATE
    h = _lowpass_kernel(int(np.ceil(factor)))
    x = np.convolve(x, h, mode="same")
    n_out = int(len(x) / factor)
    idx = np.arange(n_out) * factor
    out = np.interp(idx, np.arange(len(x)), x)
    return np.clip(out, -32768, 32767).astype(np.int16).tobytes()


class EnergyVAD:
    """Local voice activity detector, calibrated on ambient noise."""

    def __init__(self, multiplier: float = 3.5, floor_min: float = 180.0, calib_blocks: int = 25):
        self.multiplier = multiplier
        self.floor_min = floor_min
        self.calib_blocks = calib_blocks
        self._samples: list[float] = []
        self.threshold = floor_min * multiplier

    def is_speech(self, pcm: bytes) -> bool:
        e = rms(pcm)
        if len(self._samples) < self.calib_blocks:
            self._samples.append(e)
            if len(self._samples) == self.calib_blocks:
                floor = max(float(np.median(self._samples)), self.floor_min)
                self.threshold = floor * self.multiplier
            return False
        return e > self.threshold


# --------------------------------------------------------------------- input
class MicSource:
    """Live microphone -> async queue of (timestamp, 16 kHz pcm) 20 ms blocks."""

    def __init__(self, samplerate: int = MIC_SAMPLE_RATE, device=None):
        self.out_rate = samplerate
        self.native_rate = MIC_NATIVE_RATE
        self.device = device if device is not None else MIC_DEVICE
        self.blocksize = self.native_rate * MIC_BLOCK_MS // 1000
        self.channels = mic_channels(self.device)
        self.q: asyncio.Queue = asyncio.Queue()
        self._down = Downsampler(DECIMATION)
        self._stream = None
        self._loop = None
        self.is_live = True

    def _cb(self, indata, frames, time_info, status):
        ts = now()
        data = self._down.process(to_mono(bytes(indata), self.channels))
        if not data:
            return
        try:
            self._loop.call_soon_threadsafe(self.q.put_nowait, (ts, data))
        except RuntimeError:
            pass

    async def start(self):
        self._loop = asyncio.get_running_loop()
        self._stream = sd.RawInputStream(
            samplerate=self.native_rate, blocksize=self.blocksize,
            dtype="int16", channels=self.channels, device=self.device,
            callback=self._cb,
        )
        self._stream.start()

    async def blocks(self):
        while True:
            yield await self.q.get()

    def stop(self):
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None


class WavSource:
    """Replays a mono 16 kHz WAV in real time, then feeds digital silence.

    `speech_end_ts` marks the last block containing real signal. That is the
    ground-truth zero point for every latency measurement.
    """

    def __init__(self, path: str | Path, trailing_silence_ms: int = 2500):
        self.path = Path(path)
        self.trailing_silence_ms = trailing_silence_ms
        self.q: asyncio.Queue = asyncio.Queue()
        self.speech_end_ts: float | None = None
        self.is_live = False
        self._task = None

    def _read_blocks(self) -> list[bytes]:
        with wave.open(str(self.path), "rb") as w:
            assert w.getnchannels() == 1, "prompt WAVs must be mono"
            assert w.getframerate() == MIC_SAMPLE_RATE, f"prompt WAVs must be {MIC_SAMPLE_RATE} Hz"
            raw = w.readframes(w.getnframes())
        step = MIC_BLOCK_BYTES
        blocks = [raw[i:i + step] for i in range(0, len(raw), step)]
        return [b.ljust(step, b"\x00") for b in blocks if b]

    async def start(self):
        self._task = asyncio.create_task(self._pump())

    async def _pump(self):
        blocks = self._read_blocks()
        loud = [i for i, b in enumerate(blocks) if rms(b) > 300]
        last_loud = loud[-1] if loud else len(blocks) - 1

        period = MIC_BLOCK_MS / 1000.0
        t_next = now()

        # EnergyVAD calibrates its noise floor from the first ~500ms of audio,
        # assuming that stretch is ambient silence (true for a live mic, where
        # calibration runs before the user starts talking). make_prompts.py
        # trims leading silence from every recording, so without a lead-in the
        # calibration window samples real speech and sets an unreachable
        # threshold - the VAD then never detects speech and _listen() hangs
        # forever once WavSource stops producing blocks.
        lead_in_ms = 600
        silence = b"\x00" * MIC_BLOCK_BYTES
        for _ in range(lead_in_ms // MIC_BLOCK_MS):
            self.q.put_nowait((now(), silence))
            t_next += period
            await asyncio.sleep(max(0.0, t_next - now()))

        for i, b in enumerate(blocks):
            ts = now()
            self.q.put_nowait((ts, b))
            if i == last_loud:
                self.speech_end_ts = ts
            t_next += period
            await asyncio.sleep(max(0.0, t_next - now()))

        silence = b"\x00" * MIC_BLOCK_BYTES
        for _ in range(self.trailing_silence_ms // MIC_BLOCK_MS):
            self.q.put_nowait((now(), silence))
            t_next += period
            await asyncio.sleep(max(0.0, t_next - now()))

    async def blocks(self):
        while True:
            yield await self.q.get()

    def stop(self):
        if self._task:
            self._task.cancel()


# -------------------------------------------------------------------- output
class Player:
    """Streaming speaker output on a background thread."""

    def __init__(self, samplerate: int = TTS_SAMPLE_RATE):
        self.samplerate = samplerate
        self.q: queue.Queue = queue.Queue()
        self._thread = None
        self._stop = threading.Event()
        self.first_write_ts: float | None = None
        self.done_ts: float | None = None

    def start(self):
        self._stop.clear()
        self.first_write_ts = None
        self.done_ts = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            stream = sd.RawOutputStream(samplerate=self.samplerate, channels=1,
                                        dtype="int16", blocksize=0)
            stream.start()
        except Exception as e:
            print(f"[player] cannot open output stream: {e}")
            self.done_ts = now()
            return
        carry = b""  # a chunk boundary from the network can split a 16-bit
                     # sample in half; hold the odd trailing byte over to
                     # prefix onto the next chunk instead of writing an
                     # odd-length buffer (PortAudio rejects that outright).
        try:
            while not self._stop.is_set():
                chunk = self.q.get()
                if chunk is None:
                    break
                if self.first_write_ts is None:
                    self.first_write_ts = now()
                data = carry + chunk
                if len(data) % 2:
                    carry, data = data[-1:], data[:-1]
                else:
                    carry = b""
                if data:
                    stream.write(data)
        finally:
            self.done_ts = now()
            stream.stop()
            stream.close()

    def feed(self, pcm: bytes):
        if pcm:
            self.q.put(pcm)

    def finish(self, wait: bool = True):
        self.q.put(None)
        if wait and self._thread:
            self._thread.join(timeout=30)

    def abort(self):
        self._stop.set()
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass
        self.q.put(None)


def save_wav(path: str | Path, pcm: bytes, samplerate: int = MIC_SAMPLE_RATE):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(samplerate)
        w.writeframes(pcm)


def list_devices():
    print(sd.query_devices())
    print(f"\nMIC_DEVICE={MIC_DEVICE}  MIC_NATIVE_RATE={MIC_NATIVE_RATE}  "
          f"decimation={DECIMATION}x -> {MIC_SAMPLE_RATE} Hz")
