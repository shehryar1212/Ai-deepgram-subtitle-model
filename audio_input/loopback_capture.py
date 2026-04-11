"""
WASAPI loopback capture — captures system audio output (Teams/Meet).

On most Windows systems, Stereo Mix appears as a regular input device and
needs no special WASAPI flags. WasapiSettings(loopback=True) is only used
as a fallback for output-only devices on versions of sounddevice that support it.
"""

import queue
import sys

import numpy as np
import sounddevice as sd

from config import load

WHISPER_RATE = 16000

# Names that identify a software loopback / stereo-mix input device
_LOOPBACK_KEYWORDS = ("stereo mix", "loopback", "what u hear", "wave out mix")


def get_loopback_device() -> int | None:
    """Return the best device index for capturing system audio.

    Priority:
    1. Input device whose name matches a known loopback keyword (Stereo Mix, etc.)
    2. Default output device (for WasapiSettings loopback fallback)
    3. First device with output channels
    """
    devices = sd.query_devices()

    # 1. Prefer an explicit software-loopback input device
    for i, dev in enumerate(devices):
        if dev["max_input_channels"] > 0:
            if any(kw in dev["name"].lower() for kw in _LOOPBACK_KEYWORDS):
                return i

    # 2. Default output device (WasapiSettings fallback)
    try:
        out_idx = sd.default.device[1]
        if isinstance(out_idx, int) and out_idx >= 0:
            return out_idx
    except Exception:
        pass

    # 3. First device with output channels
    for i, dev in enumerate(devices):
        if dev["max_output_channels"] > 0:
            return i

    return None


def create_loopback_stream(
    audio_queue: queue.Queue, device_idx: int | None = None
) -> sd.InputStream:
    """Create a loopback input stream capturing system audio.

    device_idx: optional device index override from device selector.
                Falls back to auto-detection if None.
    """
    cfg = load()["audio"]

    if device_idx is None:
        device_idx = get_loopback_device()
    if device_idx is None:
        raise RuntimeError("No loopback device found for system audio capture.")

    device_info = sd.query_devices(device_idx)
    capture_rate = int(device_info["default_samplerate"])

    # Decide whether this is a native input device or an output we need to tap
    is_input_device = device_info["max_input_channels"] > 0
    num_channels = (
        max(1, min(int(device_info["max_input_channels"]), 2))
        if is_input_device
        else max(1, min(int(device_info["max_output_channels"]), 2))
    )

    print(
        f"[INFO] Loopback device [{device_idx}]: {device_info['name']} "
        f"({'input' if is_input_device else 'output-tap'}, "
        f"{num_channels}ch @ {capture_rate} Hz)",
        flush=True,
    )

    def _resample(data: np.ndarray) -> np.ndarray:
        if capture_rate == WHISPER_RATE:
            return data
        old_len = len(data)
        new_len = int(old_len * WHISPER_RATE / capture_rate)
        x_old = np.linspace(0, 1, old_len)
        x_new = np.linspace(0, 1, new_len)
        return np.interp(x_new, x_old, data).astype(np.float32)

    _level_frames = [0]
    _level_peak   = [0.0]

    def _callback(indata, frames, time, status):
        if status:
            print(f"[WARN] Loopback status: {status}", file=sys.stderr)
        mono = indata.mean(axis=1) if indata.shape[1] > 1 else indata[:, 0]
        resampled = _resample(mono)
        audio_queue.put(resampled.reshape(-1, 1))

        # Periodic level report — helps confirm audio is actually flowing
        peak = float(np.abs(mono).max())
        if peak > _level_peak[0]:
            _level_peak[0] = peak
        _level_frames[0] += 1
        if _level_frames[0] >= int(capture_rate / cfg.get("blocksize", 4096) * 5):
            lvl = _level_peak[0]
            bar = int(lvl * 20)
            label = "OK" if lvl > 0.001 else "SILENT — no audio detected on this device"
            print(f"[LOOPBACK] level: {'█' * bar}{'░' * (20 - bar)} {lvl:.3f}  {label}", flush=True)
            _level_frames[0] = 0
            _level_peak[0] = 0.0

    stream_kwargs = dict(
        samplerate=capture_rate,
        channels=num_channels,
        dtype="float32",
        device=device_idx,
        blocksize=cfg.get("blocksize", 4096),
        latency="high",
        callback=_callback,
    )

    if not is_input_device:
        # Output-only device: attempt WASAPI loopback tap
        try:
            stream_kwargs["extra_settings"] = sd.WasapiSettings(loopback=True)
        except (AttributeError, TypeError):
            raise RuntimeError(
                f"Device [{device_idx}] '{device_info['name']}' is output-only and "
                "this version of sounddevice does not support WasapiSettings(loopback=True). "
                "Enable Stereo Mix in Windows Sound settings instead."
            )

    return sd.InputStream(**stream_kwargs)
