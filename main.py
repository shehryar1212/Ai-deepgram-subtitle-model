import concurrent.futures
import json
import queue
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from audio_input.mic_capture import create_stream
from config import load
from output.writer import emit, emit_partial
from overlay.device_selector import DeviceSelector
from overlay.subtitle_bar import SubtitleBar
from transcription.audience_pipeline import AudiencePipeline
from transcription.corrector import correct
from transcription.deepgram_engine import DeepgramTranscriber
from translation.translator import translate

_LOG_PATH: Path | None = None
_log_entries: list[dict] = []


def _init_log() -> None:
    global _LOG_PATH
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    _LOG_PATH = Path("logs") / f"session_{timestamp}.json"


def log_segment(raw: str, corrected: str, translated: str, latency_ms: int, target_lang: str = "", merged: bool = False, correction_rule: str = "none") -> None:
    entry = {
        "timestamp": datetime.now().strftime("%H:%M:%S"),
        "target_lang": target_lang,
        "raw": raw,
        "corrected": corrected,
        "translated": translated,
        "merged": merged,
        "correction_rule": correction_rule,
        "latency_ms": latency_ms,
    }
    _log_entries.append(entry)
    if _LOG_PATH is not None:
        try:
            _LOG_PATH.write_text(
                json.dumps(_log_entries, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

running = True

_last_transcript: str = ""
_last_transcript_time: float = 0.0

# Single-worker executor: translations run in background, never block audio loop.
# max_workers=1 keeps output sequential (no interleaved subtitles).
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)


def _is_duplicate(text: str, dedup_window: float) -> bool:
    global _last_transcript, _last_transcript_time
    now = time.time()
    if now - _last_transcript_time > dedup_window:
        return False
    last_words = _last_transcript.strip().split()
    new_words = text.strip().split()
    # Match on first 8 words, or full text if shorter
    n = min(8, len(last_words), len(new_words))
    return last_words[:n] == new_words[:n]


def _signal_handler(sig, frame):
    global running
    print("\n[INFO] Stopping transcription...")
    running = False
    sys.exit(0)


signal.signal(signal.SIGINT, _signal_handler)


def _translate_and_emit(text: str, subtitle_bar: SubtitleBar, debug: bool, was_merged: bool = False) -> None:
    """Runs in background thread. Reads active language from subtitle_bar at execution
    time so mid-session language switches take effect on the next queued segment.
    Pipeline: correct → translate → update overlay + emit to file.
    English text is already shown on overlay by caller before this runs.
    """
    try:
        target_lang, target_name = subtitle_bar.get_selected_language()  # (code, name)
        raw = text
        corrected, correction_rule = correct(raw)

        t0 = time.perf_counter()
        try:
            translated = translate(corrected, target_name)
        except Exception as e:
            print(f"\n[WARN] Translation failed ({e}) — showing original English", file=sys.stderr, flush=True)
            translated = corrected
        latency_ms = int((time.perf_counter() - t0) * 1000)

        if debug:
            print(f"\r{' ' * 120}\r[RAW]: {raw}", flush=True)
            print(f"[CORRECTED]: {corrected}", flush=True)
            print(f"[TRANSLATED]: {translated}", flush=True)
            print(f"[MERGED]: {'yes' if was_merged else 'no'}", flush=True)
            print(f"[CORRECTION_RULE]: {correction_rule}", flush=True)
            print(f"[LATENCY]: {latency_ms}ms", flush=True)
        else:
            print(f"\r{' ' * 120}\r[TRANSCRIPT]: {corrected}", flush=True)
            print(f"[TRANSLATED]: {translated}", flush=True)

        log_segment(raw, corrected, translated, latency_ms, target_lang, was_merged, correction_rule)
        subtitle_bar.set_translated(translated)
        emit(translated)  # keep atomic file write for OBS compatibility
    except Exception as exc:
        import traceback
        print(f"\n[ERROR] Translation worker crashed: {exc}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)


def main():
    global _last_transcript, _last_transcript_time

    _init_log()
    cfg = load()
    mic_gain:    float = cfg["audio"]["mic_gain"]
    debug:       bool  = cfg.get("debug", False)
    dedup_window: float = cfg["deepgram"].get("dedup_window_seconds", 1.5)
    languages:   dict  = cfg.get("languages", {"fr": "French", "it": "Italian", "pt": "Portuguese"})
    overlay_cfg: dict  = cfg.get("overlay", {})

    # ── Device selection — shown before anything starts ──────────────────
    # Input languages = English + all configured output languages
    input_languages = {"en": "English", **languages}
    selector = DeviceSelector(
        current_mic=cfg["audio"].get("device"),
        current_loopback=None,
        input_languages=input_languages,
    )
    mic_device, loopback_device, input_lang, loopback_lang = selector.show()  # raises SystemExit if user exits
    print(f"[INFO] Transcription language: {input_languages.get(input_lang, input_lang)}", flush=True)
    print(f"[INFO] Loopback language: {input_languages.get(loopback_lang, loopback_lang)}", flush=True)

    # ── Launch overlay — starts with default language pre-selected ───────
    default_lang = overlay_cfg.get("default_language", next(iter(languages), "fr"))
    subtitle_bar = SubtitleBar(languages, overlay_cfg, default_lang=default_lang)
    subtitle_bar.start()
    target_lang, target_name = subtitle_bar.wait_for_language()
    print(f"[INFO] Translating to {target_name} ({target_lang})", flush=True)

    audio_queue:      queue.Queue = queue.Queue()
    transcript_queue: queue.Queue = queue.Queue()

    transcriber = DeepgramTranscriber(transcript_queue, language=input_lang)
    transcriber.start()
    subtitle_bar.set_status(True)

    # ── Audience pipeline (system audio loopback) ──────────────────────────
    audience_cfg = cfg.get("audience", {})
    audience_pipeline: AudiencePipeline | None = None
    if audience_cfg.get("enabled", False) and loopback_device is not None:
        # Use language selected in device selector UI (not hardcoded config)
        audience_lang_code = loopback_lang
        audience_lang_name = input_languages.get(loopback_lang, loopback_lang)
        audience_pipeline = AudiencePipeline(
            overlay=subtitle_bar,
            audience_lang_code=audience_lang_code,
            audience_lang_name=audience_lang_name,
            loopback_device=loopback_device,
        )
        try:
            audience_pipeline.start()
            print("[INFO] Dual audio active — mic + system audio", flush=True)
        except Exception as e:
            print(f"[WARN] System audio capture unavailable: {e}", flush=True)
            print("[WARN] Running in single-mic mode only", flush=True)
            audience_pipeline = None

    print("[INFO] Listening... (Ctrl+C to stop)\n")

    with create_stream(audio_queue, device=mic_device):
        while running and not subtitle_bar.stop_requested():
            try:
                chunk = audio_queue.get(timeout=0.05)
                extra = [chunk]
                while not audio_queue.empty():
                    extra.append(audio_queue.get_nowait())
                combined = np.concatenate(extra, axis=0)
                combined = np.clip(combined * mic_gain, -1.0, 1.0)
                transcriber.send_audio(combined)
            except queue.Empty:
                pass

            while not transcript_queue.empty():
                item = transcript_queue.get_nowait()
                kind = item[0]
                if kind == "final":
                    _, text, was_merged = item
                    if len(text.split()) < 2:
                        continue
                    if _is_duplicate(text, dedup_window):
                        continue
                    _last_transcript = text
                    _last_transcript_time = time.time()
                    # Show English immediately so overlay updates before GPT returns
                    corrected_preview, _ = correct(text)
                    subtitle_bar.set_english(corrected_preview)
                    _executor.submit(_translate_and_emit, text, subtitle_bar, debug, was_merged)
                elif kind == "partial":
                    text = item[1]
                    if debug:
                        print(f"\r[PARTIAL]: {text}   ", end="", flush=True)
                    emit_partial(text)

    transcriber.finish()
    if audience_pipeline is not None:
        audience_pipeline.stop()
    subtitle_bar.set_status(False)
    # Drain any remaining finals, wait for in-flight translations to finish
    while not transcript_queue.empty():
        item = transcript_queue.get_nowait()
        if item[0] == "final":
            _, text, was_merged = item
            if len(text.split()) >= 2 and not _is_duplicate(text, dedup_window):
                _executor.submit(_translate_and_emit, text, subtitle_bar, debug, was_merged)
    _executor.shutdown(wait=True)
    subtitle_bar.stop()


if __name__ == "__main__":
    main()
