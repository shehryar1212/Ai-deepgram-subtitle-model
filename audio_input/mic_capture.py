"""
Microphone capture using sounddevice.
Captures at the device's native sample rate, then resamples to 16000 Hz
(the rate Deepgram expects) before pushing chunks into the queue.
"""

import queue
import sys

import numpy as np
import sounddevice as sd

from config import load

DEEPGRAM_RATE = 16000


def create_stream(audio_queue: queue.Queue, device: int | None = None) -> sd.InputStream:
    """
    Create and return a sounddevice InputStream configured from settings.
    Captures at native device rate and resamples to DEEPGRAM_RATE.
    The stream is NOT started here — use it as a context manager in main.py.

    device: optional device index override (takes priority over settings.json).
    """
    cfg = load()["audio"]
    effective_device = device if device is not None else cfg["device"]
    device_info = sd.query_devices(effective_device, "input")
    capture_rate = int(device_info["default_samplerate"])
    print(f"[INFO] Mic device: [{effective_device}] {device_info['name']}")
    print(f"[INFO] Capturing at {capture_rate} Hz → resample to {DEEPGRAM_RATE} Hz")

    def _resample(data: np.ndarray) -> np.ndarray:
        """Downsample from capture_rate to DEEPGRAM_RATE using linear interpolation."""
        if capture_rate == DEEPGRAM_RATE:
            return data
        ratio = DEEPGRAM_RATE / capture_rate
        old_len = len(data)
        new_len = int(old_len * ratio)
        x_old = np.linspace(0, 1, old_len)
        x_new = np.linspace(0, 1, new_len)
        return np.interp(x_new, x_old, data).astype(np.float32)

    def _callback(indata, frames, time, status):
        if status:
            print(f"[WARN] Audio status: {status}", file=sys.stderr)
        mono = indata[:, 0]
        resampled = _resample(mono)
        audio_queue.put(resampled.reshape(-1, 1))

    return sd.InputStream(
        samplerate=capture_rate,
        channels=cfg["channels"],
        dtype="float32",
        device=effective_device,
        blocksize=cfg["blocksize"],
        latency="high",
        callback=_callback,
    )
