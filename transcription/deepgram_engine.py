"""
Deepgram Nova-3 streaming transcription engine (SDK v6).

Uses deepgram-sdk v6 sync WebSocket API via a background thread.
Only is_final=True results are emitted as "final"; interim results as "partial".
Automatically reconnects if the WebSocket connection drops.

Audio contract: expects float32 numpy arrays at 16000 Hz (mono),
matching the output of audio_input.mic_capture after resampling.
"""

import queue
import re
import sys
import threading
import time

import numpy as np
from deepgram import DeepgramClient
from deepgram.core.events import EventType
from deepgram.listen.v1 import ListenV1Results, ListenV1UtteranceEnd

from config import load

_CONTINUATION_WORDS = {
    "the", "a", "an", "and", "but", "or", "of", "that",
    "which", "who", "with", "in", "on", "at", "to", "for",
    "as", "by", "from", "into", "than", "because", "while",
}
_MERGE_WINDOW_SECONDS = 1.5


class DeepgramTranscriber:
    def __init__(self, transcript_queue: queue.Queue, language: str = None):
        cfg = load()["deepgram"]
        self._queue = transcript_queue
        self._model = cfg.get("model", "nova-3")
        self._language = language or cfg.get("language", "en")
        self._sample_rate = str(cfg.get("sample_rate", 16000))
        self._endpointing = str(cfg.get("endpointing", 1500))
        self._utterance_end_ms = str(cfg.get("utterance_end_ms", 2500))
        # Nova-3 uses "keyterm" (no boost weight). Strip ":N" boost suffixes if present.
        raw_keywords = cfg.get("keywords", [])
        self._keyterms = [k.split(":")[0] for k in raw_keywords] if raw_keywords else []
        self._buffer_max_words: int = cfg.get("buffer_max_words", 15)
        self._buffer_timeout: float = cfg.get("buffer_timeout_seconds", 3.0)
        self._client = DeepgramClient(api_key=cfg["api_key"])
        self._connection = None
        self._ready = threading.Event()
        self._shutdown = False
        self._thread = None
        self._sentence_buffer: str = ""
        self._buffer_last_update: float = 0.0
        self._last_was_merged: bool = False
        self._buffer_lock = threading.Lock()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("Deepgram connection timed out after 10s")
        if self._connection is None:
            raise RuntimeError("Deepgram connection failed — check model name and API key")
        threading.Thread(target=self._watchdog, daemon=True).start()
        print("[INFO] Deepgram connection established.", flush=True)

    def _watchdog(self) -> None:
        """Flush buffer based on age and completeness.
        Complete sentences (ending .?!) flush after buffer_timeout.
        Incomplete fragments wait 3x longer to avoid cutting mid-sentence pauses.
        """
        while not self._shutdown:
            time.sleep(0.5)
            with self._buffer_lock:
                if not self._sentence_buffer:
                    continue
                age = time.time() - self._buffer_last_update
                ends_sentence = self._sentence_buffer[-1] in ".?!"
                threshold = self._buffer_timeout if ends_sentence else self._buffer_timeout * 3
                if age > threshold:
                    self._flush_buffer()

    def _connect_kwargs(self) -> dict:
        kwargs = dict(
            model=self._model,
            language=self._language,
            encoding="linear16",
            sample_rate=self._sample_rate,
            interim_results="true",
            endpointing=self._endpointing,
        )
        if self._keyterms:
            kwargs["keyterm"] = self._keyterms
        kwargs["punctuate"] = "true"
        kwargs["utterance_end_ms"] = self._utterance_end_ms
        return kwargs

    def _run(self) -> None:
        first = True
        while not self._shutdown:
            try:
                with self._client.listen.v1.connect(**self._connect_kwargs()) as connection:
                    self._connection = connection

                    def _on_message(msg=None, result=None, **kwargs):
                        # SDK v6 may pass the result as a keyword arg 'result'
                        # or as the first positional arg; handle both forms.
                        data = result if result is not None else msg
                        # UtteranceEnd: Deepgram detected end of speech — flush buffer immediately
                        if isinstance(data, ListenV1UtteranceEnd):
                            with self._buffer_lock:
                                self._flush_buffer()
                            return
                        if not isinstance(data, ListenV1Results):
                            return
                        transcript = data.channel.alternatives[0].transcript.strip()
                        if not transcript:
                            return
                        if data.is_final:
                            with self._buffer_lock:
                                self._handle_final(transcript)
                        else:
                            self._queue.put(("partial", transcript))

                    connection.on(EventType.MESSAGE, _on_message)
                    connection.on(EventType.ERROR, lambda err=None, **kwargs: print(f"[ERROR] Deepgram: {err}", file=sys.stderr))

                    if first:
                        time.sleep(0.3)  # allow Deepgram ASR to warm up before audio flows
                        self._ready.set()
                        first = False

                    connection.start_listening()  # blocks until connection closes

            except Exception as e:
                if self._shutdown:
                    break
                print(f"[WARN] Deepgram disconnected ({e}), reconnecting in 2s...", file=sys.stderr)
                time.sleep(2)
            finally:
                # Always clear immediately so send_audio stops trying on a dead socket
                self._connection = None

    def _flush_buffer(self) -> None:
        """Must be called with _buffer_lock held, or from finish() after shutdown."""
        if self._sentence_buffer.strip():
            self._queue.put(("final", self._sentence_buffer.strip(), self._last_was_merged))
            self._sentence_buffer = ""
            self._last_was_merged = False

    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        """Split a transcript on sentence-ending punctuation, keeping the punctuation attached."""
        parts = re.split(r'(?<=[.?!])\s+', text.strip())
        return [p.strip() for p in parts if p.strip()]

    def _is_continuation(self, text: str) -> bool:
        first_word = text.strip().split()[0].lower().rstrip(".,")
        return first_word in _CONTINUATION_WORDS

    def _handle_final(self, transcript: str) -> None:
        now = time.time()
        sentences = self._split_sentences(transcript)
        for sentence in sentences:
            self._handle_sentence(sentence, now)
        self._buffer_last_update = now

    def _handle_sentence(self, transcript: str, now: float) -> None:
        buffer_ends_sentence = bool(self._sentence_buffer) and self._sentence_buffer[-1] in ".?!"

        if self._sentence_buffer and not buffer_ends_sentence:
            # Buffer is mid-sentence — always append regardless of continuation word
            self._sentence_buffer = (self._sentence_buffer.rstrip() + " " + transcript).strip()
            self._last_was_merged = True
        elif (self._sentence_buffer
                and buffer_ends_sentence
                and self._is_continuation(transcript)
                and now - self._buffer_last_update < _MERGE_WINDOW_SECONDS):
            # Completed sentence followed quickly by a continuation clause ("which", "that", etc.)
            self._sentence_buffer = (self._sentence_buffer.rstrip() + " " + transcript).strip()
            self._last_was_merged = True
        else:
            if self._sentence_buffer:
                self._flush_buffer()
            self._sentence_buffer = transcript
            self._last_was_merged = False

        word_count = len(self._sentence_buffer.split())
        ends_sentence = bool(self._sentence_buffer) and self._sentence_buffer[-1] in ".?!"

        if (ends_sentence and word_count >= 3) or word_count >= 35:
            self._flush_buffer()

    def send_audio(self, chunk: np.ndarray) -> None:
        if self._connection is None:
            return
        try:
            pcm = (chunk.flatten() * 32767).clip(-32768, 32767).astype(np.int16).tobytes()
            self._connection.send_media(pcm)
        except Exception as e:
            print(f"[WARN] Audio send failed: {e}", file=sys.stderr)

    def finish(self) -> None:
        self._shutdown = True
        if self._connection is not None:
            try:
                self._connection.send_close_stream()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)
        with self._buffer_lock:
            self._flush_buffer()
        print("[INFO] Deepgram connection closed.", flush=True)
