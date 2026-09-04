"""Speech-to-text engine boundary for server-side dictation.

`SttEngine` is the seam `DictationSession` (server/stt/session.py) is written
against; `WhisperEngine` is the production implementation over faster-whisper.
faster-whisper/numpy are imported lazily inside methods so `server/` imports
without the optional `stt` extra installed — `is_available()` tells callers
whether the extra is present.
"""
from __future__ import annotations

import importlib.util
import logging
import threading
from typing import Any, Protocol

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000  # Hz — whisper's native rate; the browser resamples to this.

# One inference at a time per process: the edge shares its cores with every live
# claude session, so N concurrent dictations must not fan out across all of them.
_INFERENCE_LOCK = threading.Lock()


class SttEngine(Protocol):
    def transcribe(self, pcm: bytes) -> str: ...
    def speech_segments(self, pcm: bytes) -> list[tuple[float, float]]: ...


def is_available() -> bool:
    """True when the `stt` extra (faster-whisper) is importable."""
    return importlib.util.find_spec("faster_whisper") is not None


def _pcm_to_float(pcm: bytes) -> Any:
    import numpy as np
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


class WhisperEngine:
    """faster-whisper (CTranslate2, int8, greedy) over 16 kHz Int16 mono PCM."""

    def __init__(self, model: str = "base.en", *, cpu_threads: int = 4) -> None:
        self.model_name = model
        self.cpu_threads = cpu_threads
        self._model: Any = None
        self._load_lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def warm(self) -> None:
        """Load the model now (first load downloads it and takes seconds)."""
        self._get_model()

    def _get_model(self) -> Any:
        with self._load_lock:
            if self._model is None:
                self._model = self._load_model()
            return self._model

    def _load_model(self) -> Any:  # overridden in tests
        from faster_whisper import WhisperModel
        log.info("stt: loading whisper model %s (int8, %d threads)",
                 self.model_name, self.cpu_threads)
        return WhisperModel(self.model_name, device="cpu", compute_type="int8",
                            cpu_threads=self.cpu_threads)

    def transcribe(self, pcm: bytes) -> str:
        if not pcm:
            return ""
        model = self._get_model()
        audio = _pcm_to_float(pcm)
        with _INFERENCE_LOCK:
            segments, _info = model.transcribe(
                audio, beam_size=1, language="en", vad_filter=False,
                condition_on_previous_text=False,
            )
            return " ".join(s.text.strip() for s in segments).strip()

    def _vad_timestamps(self, audio: Any) -> list[dict]:  # overridden in tests
        from faster_whisper.vad import VadOptions, get_speech_timestamps
        return get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=300))

    def speech_segments(self, pcm: bytes) -> list[tuple[float, float]]:
        if not pcm:
            return []
        audio = _pcm_to_float(pcm)
        with _INFERENCE_LOCK:
            stamps = self._vad_timestamps(audio)
        return [(t["start"] / SAMPLE_RATE, t["end"] / SAMPLE_RATE) for t in stamps]
