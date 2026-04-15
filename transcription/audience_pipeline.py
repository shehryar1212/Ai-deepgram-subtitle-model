"""
Audience pipeline — captures system audio, transcribes in audience language,
translates to English, updates left panel of overlay.
"""

import queue
import threading

import numpy as np

from audio_input.loopback_capture import create_loopback_stream
from config import load
from transcription.corrector import correct
from transcription.deepgram_engine import DeepgramTranscriber
from translation.translator import translate


class AudiencePipeline:
    def __init__(
        self,
        overlay,
        audience_lang_code: str,
        audience_lang_name: str,
        loopback_device: int | None = None,
    ):
        cfg = load()
        audience_cfg = cfg.get("audience", {})
        self._overlay = overlay
        self._lang_code = audience_lang_code
        self._lang_name = audience_lang_name
        self._min_words: int = audience_cfg.get("min_words", 5)
        self._loopback_gain: float = audience_cfg.get("loopback_gain", 2.0)
        self._loopback_device = loopback_device
        self._audio_queue: queue.Queue = queue.Queue()
        self._transcript_queue: queue.Queue = queue.Queue()
        self._transcriber: DeepgramTranscriber | None = None
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        # Open loopback stream first — if this fails, Deepgram is never started
        try:
            stream = create_loopback_stream(self._audio_queue, device_idx=self._loopback_device)
        except Exception as e:
            print(f"[WARN] Audience pipeline — loopback unavailable: {e}", flush=True)
            print("[WARN] Running in single-mic mode only.", flush=True)
            self._running = False
            return

        # Loopback confirmed — now start Deepgram for audience language
        self._transcriber = DeepgramTranscriber(
            self._transcript_queue,
            language=self._lang_code,
        )
        try:
            self._transcriber.start()
        except Exception as e:
            print(f"[WARN] Audience pipeline — Deepgram failed: {e}", flush=True)
            self._running = False
            return

        print(
            f"[INFO] Audience pipeline active — listening for {self._lang_name}",
            flush=True,
        )

        with stream:
            while self._running:
                # Feed audio to Deepgram
                try:
                    chunk = self._audio_queue.get(timeout=0.05)
                    extra = [chunk]
                    while not self._audio_queue.empty():
                        extra.append(self._audio_queue.get_nowait())
                    combined = np.concatenate(extra, axis=0)
                    combined = np.clip(combined * self._loopback_gain, -1.0, 1.0)
                    self._transcriber.send_audio(combined)
                except queue.Empty:
                    pass

                # Process transcripts
                while not self._transcript_queue.empty():
                    item = self._transcript_queue.get_nowait()
                    if item[0] != "final":
                        continue
                    _, text, _ = item
                    if len(text.split()) < self._min_words:
                        continue
                    try:
                        corrected, _ = correct(text)
                        if self._lang_code == "en":
                            translated = corrected
                        else:
                            translated = translate(corrected, "English")
                        print(f"[AUDIENCE] {corrected}  →  {translated}", flush=True)
                        self._overlay.set_english(f"[{self._lang_name}] {corrected}")
                        self._overlay.set_english(f"→ {translated}")
                    except Exception as e:
                        print(f"[WARN] Audience translation failed: {e}", flush=True)

    def stop(self) -> None:
        self._running = False
        if self._transcriber:
            self._transcriber.finish()
