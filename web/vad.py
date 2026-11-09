"""Energy VAD for the web server.

A deliberate copy of `rms` and `EnergyVAD` from agent/audio_io.py. Importing
that module would import sounddevice, which needs PortAudio and crashes on a
headless Linux server. Keep the two in sync: same thresholds, same
calibration rule, so live speech_end_true is detected the same way demo.py
detects it.

The live server uses AdaptiveVAD (below), which differs on purpose.
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


class AdaptiveVAD(EnergyVAD):
    """EnergyVAD whose noise floor keeps tracking the room after calibration.

    EnergyVAD calibrates once on the first 500 ms and never again. That holds
    for WAV replays, but a browser mic changes under it: Chrome's noise
    suppression and auto-gain are still settling during those first blocks,
    so the floor can lock in far too high and every later word stays below
    the threshold (seen live: vad_missed_speech on 7 of 7 turns). Here the
    floor follows quieter blocks quickly and louder non-speech blocks slowly,
    so a bad first estimate corrects itself within about a second, while
    speech (above threshold) never raises it. floor_min is lower than
    EnergyVAD's because noise-suppressed browser audio sits near zero
    between words; 180 assumed a raw laptop mic.
    """

    def __init__(self, multiplier: float = 3.5, floor_min: float = 50.0, calib_blocks: int = 25):
        super().__init__(multiplier, floor_min, calib_blocks)
        self.floor: float | None = None
        self.peak = 0.0   # loudest block since reset_peak(), for diagnosis

    def is_speech(self, pcm: bytes) -> bool:
        e = rms(pcm)
        self.peak = max(self.peak, e)
        if self.floor is None:
            self._samples.append(e)
            if len(self._samples) == self.calib_blocks:
                self.floor = float(np.median(self._samples))
                self._update_threshold()
            return False
        speech = e > self.threshold
        if e < self.floor:
            self.floor += 0.2 * (e - self.floor)      # fall fast
        elif e < 2 * self.floor:
            # Rise slowly, and only on blocks near the floor: quiet syllables
            # sit below the threshold but well above the floor, and letting
            # them pull it up raised the threshold mid-sentence (noisy-room
            # simulation: speech end detected 1.4 s early).
            self.floor += 0.01 * (e - self.floor)
        self._update_threshold()
        return speech

    def _update_threshold(self):
        self.threshold = max(self.floor, self.floor_min) * self.multiplier

    def reset_peak(self):
        self.peak = 0.0
