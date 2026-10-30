"""Energy VAD for the web server.

A deliberate copy of `rms` and `EnergyVAD` from agent/audio_io.py. Importing
that module would import sounddevice, which needs PortAudio and crashes on a
headless Linux server. Keep the two in sync: same thresholds, same
calibration rule, so live speech_end_true is detected the same way demo.py
detects it.
"""
import numpy as np


def rms(pcm: bytes) -> float:
    """RMS energy of int16 PCM bytes. Same as agent.audio_io.rms."""
    if not pcm:
        return 0.0
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    if a.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(a * a)))


class EnergyVAD:
    """Same as agent.audio_io.EnergyVAD: calibrate on the first 25 blocks
    (500 ms), then speech = energy above 3.5x the ambient floor."""

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
