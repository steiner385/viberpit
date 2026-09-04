"""Endpointing policy for one dictation: PCM buffer → interim/final events.

Pure and synchronous so it is unit-testable with a fake engine; the WebSocket
route (server/stt/ws.py) calls `tick()`/`flush()` from a worker thread and
`feed()` from the event loop, hence the lock around buffer mutation.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Literal

from server.stt.engine import SAMPLE_RATE, SttEngine

BYTES_PER_SAMPLE = 2  # Int16


@dataclass(frozen=True)
class Event:
    kind: Literal["interim", "final"]
    text: str


class DictationSession:
    def __init__(
        self,
        engine: SttEngine,
        *,
        endpoint_silence_s: float = 0.7,
        max_window_s: float = 15.0,
        silence_trim_s: float = 2.0,
        keep_tail_s: float = 0.5,
        sample_rate: int = SAMPLE_RATE,
    ) -> None:
        self.engine = engine
        self.endpoint_silence_s = endpoint_silence_s
        self.max_window_s = max_window_s
        self.silence_trim_s = silence_trim_s
        self.keep_tail_s = keep_tail_s
        self.sample_rate = sample_rate
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._fed_since_tick = False
        self._last_interim = ""

    # -- units -------------------------------------------------------------
    def _secs(self, n_bytes: int) -> float:
        return n_bytes / (self.sample_rate * BYTES_PER_SAMPLE)

    def _nbytes(self, secs: float) -> int:
        return int(secs * self.sample_rate) * BYTES_PER_SAMPLE

    @property
    def buffered_s(self) -> float:
        with self._lock:
            return self._secs(len(self._buf))

    # -- input --------------------------------------------------------------
    def feed(self, pcm: bytes) -> None:
        with self._lock:
            self._buf += pcm
            self._fed_since_tick = True

    # -- policy -------------------------------------------------------------
    def tick(self) -> list[Event]:
        with self._lock:
            if not self._fed_since_tick:
                return []
            self._fed_since_tick = False
            buf = bytes(self._buf)

        segments = self.engine.speech_segments(buf)
        total_s = self._secs(len(buf))
        if not segments:
            if total_s > self.silence_trim_s:
                self._drop_prefix(len(buf) - self._nbytes(self.keep_tail_s))
            return []

        last_end_s = segments[-1][1]
        if total_s - last_end_s >= self.endpoint_silence_s and not self._last_interim:
            cut = self._nbytes(last_end_s)
            text = self.engine.transcribe(buf[:cut])
            self._drop_prefix(cut)
            return self._final(text)

        if total_s >= self.max_window_s:
            text = self.engine.transcribe(buf)
            self._drop_prefix(len(buf))
            return self._final(text)

        text = self.engine.transcribe(buf).strip()
        if not text or text == self._last_interim:
            return []
        self._last_interim = text
        return [Event("interim", text)]

    def flush(self) -> list[Event]:
        with self._lock:
            buf = bytes(self._buf)
            self._buf.clear()
            self._fed_since_tick = False
        text = self.engine.transcribe(buf) if buf else ""
        return self._final(text)

    # -- helpers ------------------------------------------------------------
    def _drop_prefix(self, n_bytes: int) -> None:
        with self._lock:
            del self._buf[:max(0, n_bytes)]

    def _final(self, text: str) -> list[Event]:
        self._last_interim = ""
        text = text.strip()
        return [Event("final", text)] if text else []
