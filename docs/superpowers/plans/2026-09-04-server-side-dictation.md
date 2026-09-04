# Server-side Streaming Dictation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the laggy browser Web Speech dictation with a self-hosted streaming
Whisper engine on the edge, keeping Web Speech as the fallback when the server engine is off.

**Architecture:** The browser captures 16 kHz Int16 PCM through an AudioWorklet and streams it
over an identity-gated WebSocket (`/ws/stt`). The edge buffers audio in a `DictationSession`,
ticks once a second, uses Silero VAD to endpoint, and runs `faster-whisper` (`base.en`, int8) to
emit `interim`/`final` text. `useDictationField` picks the server engine when `/api/config`
reports `sttEnabled`, else the existing Web Speech hook.

**Tech Stack:** FastAPI WebSocket + `faster-whisper` 1.2 (CTranslate2, Silero VAD via onnxruntime)
on the backend; React hooks + Web Audio `AudioWorklet` + native `WebSocket` on the frontend;
pytest / vitest.

**Spec:** `docs/superpowers/specs/2026-09-04-server-side-dictation-design.md`

## Global Constraints

- New Python dependency lives in an optional extra `stt = ["faster-whisper>=1.2,<2"]`; `server/`
  must import without it (lazy imports inside functions/classes only).
- Feature is OFF by default: `CCSWITCH_ENABLE_STT` defaults to `false`.
- Defaults: model `base.en`, `cpu_threads` 4, tick 1.0 s, endpoint silence 0.7 s, max window 15 s,
  max concurrent 3, max session 600 s, sample rate 16000 Hz, Int16 little-endian mono.
- WebSocket close codes: 4004 `stt disabled`, 4005 `stt busy`.
- Audio is never persisted or logged.
- Quality gate before pushing: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`,
  `pytest tests/`, `pytest server/tests/`, and in `web/`: `vitest run`, `tsc --noEmit`, `eslint .`.
- Run Python tests with the verification venv that has both extras installed:
  `env -u CCSWITCH_SESSIOND_ENDPOINT -u CCSWITCH_SESSIOND_CLIENT_CERT -u CCSWITCH_SESSIOND_TLS_KEY -u CCSWITCH_SESSIOND_TLS_CERT -u CCSWITCH_SESSIOND_TLS_CA -u CCSWITCH_SESSIOND_CLIENT_ALLOWLIST -u CCSWITCH_DAEMON_ROLE -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_DB_URL CCSWITCH_DEV=1 /tmp/stt-spike/bin/python -m pytest <paths> -q`
  (the shared `/home/tony/GitHub/ccswitch/.venv` lacks `faster-whisper`; both work for tasks 1–4).
- Web tests: `web/node_modules` may need `ln -s /home/tony/GitHub/ccswitch/web/node_modules web/node_modules`;
  always run vitest from a subshell `(cd web && npx vitest run …)` — never `cd` the session into `web/`.
- Commits end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Write commit
  messages to a file and use `git commit -F` (a PreToolUse hook rejects inline messages that
  contain deploy-ish words).

---

### Task 1: `SttEngine` protocol + `WhisperEngine` + `stt` extra

**Files:**
- Create: `server/stt/__init__.py` (empty docstring module)
- Create: `server/stt/engine.py`
- Test: `server/tests/test_stt_engine.py`
- Modify: `pyproject.toml` (`[project.optional-dependencies]`)
- Modify: `server/deploy/steps.py:255` (`.[server]` → `.[server,stt]`)
- Modify: `server/tests/test_deploy_steps_deps_build.py` (assert the new extras string)

**Interfaces:**
- Produces:
  - `SAMPLE_RATE: int = 16000`
  - `class SttEngine(Protocol)`: `transcribe(self, pcm: bytes) -> str`,
    `speech_segments(self, pcm: bytes) -> list[tuple[float, float]]` (seconds)
  - `def is_available() -> bool`
  - `class WhisperEngine(model: str = "base.en", *, cpu_threads: int = 4)` with `warm()`,
    `loaded` property, `transcribe`, `speech_segments`, and an overridable `_load_model()`.

- [ ] **Step 1: Write the failing tests**

`server/tests/test_stt_engine.py`:

```python
"""WhisperEngine: lazy model load, serialized inference, VAD segment mapping."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.stt import engine as eng


class _FakeModel:
    def __init__(self):
        self.calls: list[dict] = []

    def transcribe(self, audio, **kw):
        self.calls.append({"n": len(audio), **kw})
        return iter([SimpleNamespace(text="  hello "), SimpleNamespace(text="world ")]), None


def _engine(monkeypatch) -> tuple[eng.WhisperEngine, _FakeModel, list[int]]:
    fake = _FakeModel()
    loads: list[int] = []
    e = eng.WhisperEngine("base.en", cpu_threads=2)
    monkeypatch.setattr(e, "_load_model", lambda: (loads.append(1), fake)[1])
    return e, fake, loads


def test_is_available_reflects_import(monkeypatch):
    monkeypatch.setattr(eng.importlib.util, "find_spec", lambda name: None)
    assert eng.is_available() is False
    monkeypatch.setattr(eng.importlib.util, "find_spec", lambda name: object())
    assert eng.is_available() is True


def test_model_loads_once_and_lazily(monkeypatch):
    pytest.importorskip("numpy")
    e, _fake, loads = _engine(monkeypatch)
    assert e.loaded is False and loads == []
    e.warm()
    e.transcribe(b"\x00\x00" * 160)
    assert e.loaded is True and loads == [1]


def test_transcribe_joins_stripped_segments_with_greedy_settings(monkeypatch):
    pytest.importorskip("numpy")
    e, fake, _ = _engine(monkeypatch)
    assert e.transcribe(b"\x00\x00" * 160) == "hello world"
    call = fake.calls[0]
    assert call["n"] == 160
    assert call["beam_size"] == 1
    assert call["condition_on_previous_text"] is False
    assert call["vad_filter"] is False


def test_transcribe_empty_pcm_short_circuits(monkeypatch):
    e, fake, loads = _engine(monkeypatch)
    assert e.transcribe(b"") == ""
    assert fake.calls == [] and loads == []


def test_speech_segments_maps_samples_to_seconds(monkeypatch):
    pytest.importorskip("numpy")
    e, _, _ = _engine(monkeypatch)
    monkeypatch.setattr(
        e, "_vad_timestamps",
        lambda audio: [{"start": 0, "end": 16000}, {"start": 24000, "end": 40000}],
    )
    assert e.speech_segments(b"\x00\x00" * 48000) == [(0.0, 1.0), (1.5, 2.5)]
    assert e.speech_segments(b"") == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `<venv-python> -m pytest server/tests/test_stt_engine.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'server.stt'`.

- [ ] **Step 3: Implement the engine**

`server/stt/__init__.py`:

```python
"""Server-side speech-to-text for dictation (optional `stt` extra)."""
```

`server/stt/engine.py`:

```python
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
```

- [ ] **Step 4: Add the extra and the edge deploy install**

`pyproject.toml`, after the `server = [...]` list inside `[project.optional-dependencies]`:

```toml
# Server-side dictation engine (server/stt). Installed on the edge by the deploy
# controller (`.[server,stt]`); CI and runner sync keep `[server]` — tests use a fake engine.
stt = ["faster-whisper>=1.2,<2"]
```

`server/deploy/steps.py:255`: change `".[server]"` to `".[server,stt]"`. In
`server/tests/test_deploy_steps_deps_build.py`, find the test that asserts the pip
command (the one returning `["pip"]`) and add/adjust an assertion that the recorded
argv contains `".[server,stt]"` (read the file first; it records `run` calls).

- [ ] **Step 5: Run tests to verify they pass**

Run: `<venv-python> -m pytest server/tests/test_stt_engine.py server/tests/test_deploy_steps_deps_build.py -q`
Expected: PASS (engine tests needing numpy skip under the shared venv, pass under `/tmp/stt-spike`).

- [ ] **Step 6: Commit**

```bash
git add server/stt pyproject.toml server/deploy/steps.py server/tests/test_stt_engine.py server/tests/test_deploy_steps_deps_build.py
git commit -F /tmp/msg-task1.txt   # "feat(stt): WhisperEngine over faster-whisper behind an optional extra"
```

---

### Task 2: `DictationSession` endpointing policy

**Files:**
- Create: `server/stt/session.py`
- Test: `server/tests/test_stt_session.py`

**Interfaces:**
- Consumes: `SttEngine`, `SAMPLE_RATE` from `server/stt/engine.py`.
- Produces:
  - `@dataclass(frozen=True) class Event: kind: Literal["interim", "final"]; text: str`
  - `class DictationSession(engine, *, endpoint_silence_s=0.7, max_window_s=15.0, silence_trim_s=2.0, keep_tail_s=0.5, sample_rate=SAMPLE_RATE)`
    with `feed(pcm: bytes) -> None`, `tick() -> list[Event]`, `flush() -> list[Event]`,
    `buffered_s` property (float seconds).

- [ ] **Step 1: Write the failing tests**

`server/tests/test_stt_session.py`:

```python
"""DictationSession: buffer → VAD endpointing → interim/final events."""
from __future__ import annotations

from server.stt.session import DictationSession, Event

RATE = 16000


def pcm(seconds: float) -> bytes:
    return b"\x01\x00" * int(seconds * RATE)


class FakeEngine:
    """Scriptable engine: `segments` is returned by speech_segments; transcribe
    returns `text` and records the byte lengths it was asked to transcribe."""

    def __init__(self, segments=None, text="hello"):
        self.segments = segments or []
        self.text = text
        self.transcribed: list[int] = []

    def speech_segments(self, pcm: bytes):
        return self.segments

    def transcribe(self, pcm: bytes) -> str:
        self.transcribed.append(len(pcm))
        return self.text


def test_tick_without_new_audio_is_a_noop():
    eng = FakeEngine(segments=[(0.0, 1.0)])
    s = DictationSession(eng)
    assert s.tick() == []
    s.feed(pcm(1.0))
    assert s.tick() == [Event("interim", "hello")]
    assert s.tick() == []          # nothing new since the last tick
    assert eng.transcribed == [len(pcm(1.0))]


def test_duplicate_interim_is_suppressed_until_text_changes():
    eng = FakeEngine(segments=[(0.0, 1.0)])
    s = DictationSession(eng)
    s.feed(pcm(1.0)); assert s.tick() == [Event("interim", "hello")]
    s.feed(pcm(0.5)); assert s.tick() == []
    eng.text = "hello there"
    s.feed(pcm(0.5)); assert s.tick() == [Event("interim", "hello there")]


def test_trailing_silence_commits_a_final_and_drops_the_prefix():
    # 3 s buffered, speech 0.2–2.0 s, silence 1.0 s ≥ endpoint (0.7 s).
    eng = FakeEngine(segments=[(0.2, 2.0)], text="ask not")
    s = DictationSession(eng)
    s.feed(pcm(3.0))
    assert s.tick() == [Event("final", "ask not")]
    assert eng.transcribed == [int(2.0 * RATE) * 2]   # only up to speech end
    assert s.buffered_s == 1.0                        # the silent tail stays


def test_silence_shorter_than_endpoint_stays_interim():
    eng = FakeEngine(segments=[(0.0, 1.5)])
    s = DictationSession(eng)
    s.feed(pcm(2.0))                                   # 0.5 s of silence
    assert s.tick() == [Event("interim", "hello")]


def test_max_window_forces_a_final_and_clears():
    eng = FakeEngine(segments=[(0.0, 15.0)], text="long")
    s = DictationSession(eng, max_window_s=15.0)
    s.feed(pcm(15.0))
    assert s.tick() == [Event("final", "long")]
    assert s.buffered_s == 0.0


def test_pure_silence_is_trimmed_and_emits_nothing():
    eng = FakeEngine(segments=[])
    s = DictationSession(eng, silence_trim_s=2.0, keep_tail_s=0.5)
    s.feed(pcm(3.0))
    assert s.tick() == []
    assert s.buffered_s == 0.5
    assert eng.transcribed == []


def test_empty_transcripts_are_never_emitted():
    eng = FakeEngine(segments=[(0.0, 1.0)], text="   ")
    s = DictationSession(eng)
    s.feed(pcm(1.0))
    assert s.tick() == []


def test_flush_emits_remaining_audio_as_final():
    eng = FakeEngine(segments=[(0.0, 1.0)], text="tail")
    s = DictationSession(eng)
    s.feed(pcm(1.0))
    assert s.flush() == [Event("final", "tail")]
    assert s.buffered_s == 0.0
    assert s.flush() == []


def test_final_resets_interim_dedupe():
    eng = FakeEngine(segments=[(0.0, 1.0)], text="hello")
    s = DictationSession(eng)
    s.feed(pcm(1.0)); assert s.tick() == [Event("interim", "hello")]
    assert s.flush() == [Event("final", "hello")]
    s.feed(pcm(1.0)); assert s.tick() == [Event("interim", "hello")]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `<venv-python> -m pytest server/tests/test_stt_session.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'server.stt.session'`.

- [ ] **Step 3: Implement the session**

`server/stt/session.py`:

```python
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
        if total_s - last_end_s >= self.endpoint_silence_s:
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `<venv-python> -m pytest server/tests/test_stt_session.py -q`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add server/stt/session.py server/tests/test_stt_session.py
git commit -F /tmp/msg-task2.txt   # "feat(stt): DictationSession endpointing policy"
```

---

### Task 3: Settings + `GET /api/config` `sttEnabled`

**Files:**
- Modify: `server/config.py` (both the pydantic `Settings` and the dataclass fallback)
- Modify: `server/api/config.py`
- Test: `server/tests/test_config_api.py` (append)

**Interfaces:**
- Produces on `Settings`: `enable_stt: bool`, `stt_model: str`, `stt_cpu_threads: int`,
  `stt_tick_s: float`, `stt_max_concurrent: int`, `stt_max_session_s: float`.
- `GET /api/config` → `{..., "sttEnabled": bool}`.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_config_api.py`:

```python
def test_config_stt_disabled_by_default():
    client = _client()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("CCSWITCH_ENABLE_STT", None)
        r = client.get("/api/config", headers=H)
    assert r.json()["sttEnabled"] is False


def test_config_stt_enabled_requires_flag_and_installed_engine():
    client = _client()
    with patch.dict(os.environ, {"CCSWITCH_ENABLE_STT": "1"}), \
            patch("server.api.config.stt_available", return_value=True):
        assert client.get("/api/config", headers=H).json()["sttEnabled"] is True
    with patch.dict(os.environ, {"CCSWITCH_ENABLE_STT": "1"}), \
            patch("server.api.config.stt_available", return_value=False):
        assert client.get("/api/config", headers=H).json()["sttEnabled"] is False


def test_stt_settings_defaults_and_env(monkeypatch):
    from server.config import get_settings
    for k in ("CCSWITCH_ENABLE_STT", "CCSWITCH_STT_MODEL", "CCSWITCH_STT_CPU_THREADS",
              "CCSWITCH_STT_TICK_S", "CCSWITCH_STT_MAX_CONCURRENT", "CCSWITCH_STT_MAX_SESSION_S"):
        monkeypatch.delenv(k, raising=False)
    s = get_settings()
    assert (s.enable_stt, s.stt_model, s.stt_cpu_threads) == (False, "base.en", 4)
    assert (s.stt_tick_s, s.stt_max_concurrent, s.stt_max_session_s) == (1.0, 3, 600.0)
    monkeypatch.setenv("CCSWITCH_ENABLE_STT", "true")
    monkeypatch.setenv("CCSWITCH_STT_MODEL", "small.en")
    monkeypatch.setenv("CCSWITCH_STT_TICK_S", "0.5")
    s = get_settings()
    assert (s.enable_stt, s.stt_model, s.stt_tick_s) == (True, "small.en", 0.5)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `<venv-python> -m pytest server/tests/test_config_api.py -q`
Expected: the three new tests FAIL (`KeyError: 'sttEnabled'`, `AttributeError: enable_stt`).

- [ ] **Step 3: Add the settings**

In the pydantic `Settings` (next to the `enable_copilot` block, ~line 93):

```python
        # Server-side dictation (server/stt): off by default; needs the `stt` extra.
        enable_stt: bool = Field(default=False, alias="CCSWITCH_ENABLE_STT")
        stt_model: str = Field(default="base.en", alias="CCSWITCH_STT_MODEL")
        stt_cpu_threads: int = Field(default=4, alias="CCSWITCH_STT_CPU_THREADS")
        stt_tick_s: float = Field(default=1.0, alias="CCSWITCH_STT_TICK_S")
        stt_max_concurrent: int = Field(default=3, alias="CCSWITCH_STT_MAX_CONCURRENT")
        stt_max_session_s: float = Field(default=600.0, alias="CCSWITCH_STT_MAX_SESSION_S")
```

In the dataclass fallback (next to the `enable_copilot` field, ~line 263), following its style:

```python
        enable_stt: bool = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_ENABLE_STT", "false").lower() in ("1","true","yes","on")
        )
        stt_model: str = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_STT_MODEL", "base.en")
        )
        stt_cpu_threads: int = dataclasses.field(
            default_factory=lambda: int(os.environ.get("CCSWITCH_STT_CPU_THREADS", "4"))
        )
        stt_tick_s: float = dataclasses.field(
            default_factory=lambda: float(os.environ.get("CCSWITCH_STT_TICK_S", "1.0"))
        )
        stt_max_concurrent: int = dataclasses.field(
            default_factory=lambda: int(os.environ.get("CCSWITCH_STT_MAX_CONCURRENT", "3"))
        )
        stt_max_session_s: float = dataclasses.field(
            default_factory=lambda: float(os.environ.get("CCSWITCH_STT_MAX_SESSION_S", "600.0"))
        )
```

`server/api/config.py`:

```python
"""Config API — exposes server-side config that the frontend needs."""
from fastapi import APIRouter, Depends
from server.auth.middleware import current_identity
from server.config import get_settings
from server.stt.engine import is_available as stt_available

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("")
async def get_config(_identity: str = Depends(current_identity)) -> dict:
    settings = get_settings()
    return {
        "repoRoot": settings.repo_root,
        "adoptionEnabled": settings.enable_adoption,
        # True only when the flag is on AND the `stt` extra is importable, so the UI
        # never offers a mic engine the edge cannot actually run.
        "sttEnabled": bool(settings.enable_stt and stt_available()),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `<venv-python> -m pytest server/tests/test_config_api.py server/tests/test_config_env_float.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/config.py server/api/config.py server/tests/test_config_api.py
git commit -F /tmp/msg-task3.txt   # "feat(stt): CCSWITCH_ENABLE_STT settings and sttEnabled in /api/config"
```

---

### Task 4: `/ws/stt` WebSocket route + app wiring + lifespan warm

**Files:**
- Create: `server/stt/ws.py`
- Modify: `server/app.py` (create_app: engine on `app.state`, include router next to the VNC
  bridge at ~line 798; `_lifespan`: warm thread)
- Test: `server/tests/test_stt_ws.py`, `server/tests/test_app_stt_wiring.py`

**Interfaces:**
- Consumes: `SttEngine`, `WhisperEngine`, `is_available`, `SAMPLE_RATE` (Task 1);
  `DictationSession`, `Event` (Task 2); settings fields (Task 3); `require_ws_identity`.
- Produces: `build_stt_ws_router(get_engine: Callable[[], SttEngine | None], *, tick_s=1.0, max_concurrent=3, max_session_s=600.0, session_factory: Callable[[SttEngine], DictationSession] = DictationSession) -> APIRouter`,
  constants `CLOSE_NOT_ENABLED = 4004`, `CLOSE_BUSY = 4005`; `app.state.stt_engine`.
- Wire protocol (client→server): text `{"type":"start","sampleRate":16000}`, binary Int16 LE
  PCM frames, text `{"type":"stop"}`. (server→client): `{"type":"ready"}`,
  `{"type":"interim","text":…}`, `{"type":"final","text":…}`, `{"type":"error","message":…}`,
  `{"type":"done"}`.

- [ ] **Step 1: Write the failing route tests**

`server/tests/test_stt_ws.py`:

```python
"""/ws/stt: identity-gated PCM stream → interim/final transcript events."""
from __future__ import annotations

import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server.auth.middleware import IdentityMiddleware
from server.stt.ws import CLOSE_BUSY, CLOSE_NOT_ENABLED, build_stt_ws_router

H = {"Tailscale-User-Login": "alice@example.com"}


class FakeEngine:
    def __init__(self):
        self.warmed = 0
        self.fed_total = 0

    def warm(self):
        self.warmed += 1

    def speech_segments(self, pcm: bytes):
        return [(0.0, len(pcm) / 32000)]

    def transcribe(self, pcm: bytes) -> str:
        self.fed_total = max(self.fed_total, len(pcm))
        return f"words:{len(pcm)}"


@pytest.fixture(autouse=True)
def _identity_env(monkeypatch):
    monkeypatch.setenv("CCSWITCH_TAILNET_ALLOWLIST", "alice@example.com")
    monkeypatch.delenv("CCSWITCH_ALLOW_UNAUTHENTICATED", raising=False)


def _app(engine, **kw) -> TestClient:
    app = FastAPI()
    app.add_middleware(IdentityMiddleware)
    app.include_router(build_stt_ws_router(lambda: engine, tick_s=kw.pop("tick_s", 0.05), **kw))
    return TestClient(app, raise_server_exceptions=False)


def _collect_until(ws, kind: str, limit: int = 50) -> list[dict]:
    got = []
    for _ in range(limit):
        m = ws.receive_json()
        got.append(m)
        if m["type"] == kind:
            return got
    raise AssertionError(f"no {kind!r} in {got}")


def test_disabled_engine_closes_4004():
    client = _app(None)
    with client.websocket_connect("/ws/stt", headers=H) as ws:
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_text()
    assert ei.value.code == CLOSE_NOT_ENABLED


def test_happy_path_streams_interim_then_final_then_done():
    eng = FakeEngine()
    client = _app(eng)
    with client.websocket_connect("/ws/stt", headers=H) as ws:
        ws.send_json({"type": "start", "sampleRate": 16000})
        assert ws.receive_json() == {"type": "ready"}
        ws.send_bytes(b"\x00\x00" * 8000)          # 0.5 s
        got = _collect_until(ws, "interim")
        assert got[-1]["text"].startswith("words:")
        ws.send_bytes(b"\x00\x00" * 8000)
        ws.send_json({"type": "stop"})
        got = _collect_until(ws, "done")
        kinds = [m["type"] for m in got]
        assert "final" in kinds and kinds[-1] == "done"
    assert eng.warmed == 1
    assert eng.fed_total == 32000                     # both frames reached the session


def test_bad_sample_rate_is_rejected_with_error():
    client = _app(FakeEngine())
    with client.websocket_connect("/ws/stt", headers=H) as ws:
        ws.send_json({"type": "start", "sampleRate": 44100})
        assert ws.receive_json()["type"] == "error"
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_text()
    assert ei.value.code == 1003


def test_missing_start_frame_is_rejected():
    client = _app(FakeEngine())
    with client.websocket_connect("/ws/stt", headers=H) as ws:
        ws.send_bytes(b"\x00\x00" * 10)
        assert ws.receive_json()["type"] == "error"


def test_over_capacity_closes_4005():
    client = _app(FakeEngine(), max_concurrent=1)
    with client.websocket_connect("/ws/stt", headers=H) as first:
        first.send_json({"type": "start", "sampleRate": 16000})
        assert first.receive_json() == {"type": "ready"}
        with client.websocket_connect("/ws/stt", headers=H) as second:
            with pytest.raises(WebSocketDisconnect) as ei:
                second.receive_text()
        assert ei.value.code == CLOSE_BUSY
        first.send_json({"type": "stop"})
        _collect_until(first, "done")


def test_engine_failure_reports_error_and_closes_1011():
    class Boom(FakeEngine):
        def warm(self):
            raise RuntimeError("model download failed")
    client = _app(Boom())
    with client.websocket_connect("/ws/stt", headers=H) as ws:
        ws.send_json({"type": "start", "sampleRate": 16000})
        m = ws.receive_json()
        assert m["type"] == "error" and "model download failed" in m["message"]
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_text()
    assert ei.value.code == 1011


def test_session_deadline_flushes_and_closes():
    eng = FakeEngine()
    client = _app(eng, max_session_s=0.2)
    with client.websocket_connect("/ws/stt", headers=H) as ws:
        ws.send_json({"type": "start", "sampleRate": 16000})
        assert ws.receive_json() == {"type": "ready"}
        ws.send_bytes(b"\x00\x00" * 1600)
        t0 = time.monotonic()
        got = _collect_until(ws, "done")
        assert time.monotonic() - t0 < 5
        assert got[-1] == {"type": "done"}


def test_unauthenticated_upgrade_is_rejected():
    client = _app(FakeEngine())
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/stt") as ws:
            ws.receive_text()
```

`server/tests/test_app_stt_wiring.py`:

```python
"""create_app mounts /ws/stt and only builds an engine when enabled + installed."""
from __future__ import annotations

import os
from unittest.mock import patch


def _app(**env):
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory
    base = {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:", "CCSWITCH_DEV": "1",
            "CCSWITCH_TAILNET_ALLOWLIST": ""}
    with patch.dict(os.environ, {**base, **env}):
        os.environ.pop("CCSWITCH_ENABLE_STT", None) if "CCSWITCH_ENABLE_STT" not in env else None
        return create_app(orchestrator=SessionOrchestrator(sdk_factory=fake_sdk_factory()))


def test_route_mounted_and_engine_absent_by_default():
    app = _app()
    assert any(getattr(r, "path", None) == "/ws/stt" for r in app.routes)
    assert app.state.stt_engine is None


def test_engine_built_when_enabled_and_available():
    with patch("server.app.stt_available", return_value=True):
        app = _app(CCSWITCH_ENABLE_STT="1", CCSWITCH_STT_MODEL="tiny.en", CCSWITCH_STT_CPU_THREADS="2")
    eng = app.state.stt_engine
    assert eng is not None and eng.model_name == "tiny.en" and eng.cpu_threads == 2
    assert eng.loaded is False       # create_app never loads; the lifespan warms in a thread


def test_engine_absent_when_enabled_but_not_installed():
    with patch("server.app.stt_available", return_value=False):
        app = _app(CCSWITCH_ENABLE_STT="1")
    assert app.state.stt_engine is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `<venv-python> -m pytest server/tests/test_stt_ws.py server/tests/test_app_stt_wiring.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'server.stt.ws'` / `AttributeError: stt_engine`.

- [ ] **Step 3: Implement the route**

`server/stt/ws.py`:

```python
"""WebSocket `/ws/stt`: browser PCM stream in, transcript events out.

Protocol (client → server): text `{"type":"start","sampleRate":16000}`, then binary
Int16 little-endian mono frames, then text `{"type":"stop"}`.
Protocol (server → client): `{"type":"ready"}` once the model is loaded,
`{"type":"interim"|"final","text":…}`, `{"type":"error","message":…}`, and
`{"type":"done"}` after the flush that `stop` (or the session deadline) triggers.

Gated by the same Tailscale identity as `/ws` and `/ws/vnc`. Audio lives only in
the DictationSession buffer (≤ max_window_s) and is never persisted or logged.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from server.auth.middleware import require_ws_identity
from server.stt.engine import SAMPLE_RATE, SttEngine
from server.stt.session import DictationSession

log = logging.getLogger(__name__)

CLOSE_NOT_ENABLED = 4004
CLOSE_BUSY = 4005
CLOSE_BAD_PROTOCOL = 1003
CLOSE_INTERNAL = 1011


def build_stt_ws_router(
    get_engine: Callable[[], SttEngine | None],
    *,
    tick_s: float = 1.0,
    max_concurrent: int = 3,
    max_session_s: float = 600.0,
    session_factory: Callable[[SttEngine], DictationSession] = DictationSession,
) -> APIRouter:
    router = APIRouter()
    active = 0

    @router.websocket("/ws/stt")
    async def stt_ws(websocket: WebSocket, identity: str = Depends(require_ws_identity)) -> None:
        nonlocal active
        engine = get_engine()
        await websocket.accept()
        if engine is None:
            await websocket.close(CLOSE_NOT_ENABLED, "stt disabled")
            return
        if active >= max_concurrent:
            await websocket.close(CLOSE_BUSY, "stt busy")
            return
        active += 1
        try:
            await _serve(websocket, engine, session_factory(engine),
                         tick_s=tick_s, max_session_s=max_session_s)
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # engine/model failures surface to the UI, never crash the app
            log.warning("stt: session for %s failed: %s", identity, exc)
            await _send(websocket, {"type": "error", "message": str(exc)})
            await _close(websocket, CLOSE_INTERNAL)
        finally:
            active -= 1

    return router


async def _send(ws: WebSocket, msg: dict) -> None:
    if ws.client_state == WebSocketState.CONNECTED and ws.application_state == WebSocketState.CONNECTED:
        await ws.send_json(msg)


async def _close(ws: WebSocket, code: int, reason: str = "") -> None:
    if ws.application_state == WebSocketState.CONNECTED:
        await ws.close(code, reason)


async def _handshake(ws: WebSocket) -> bool:
    """Consume the `start` frame; on a protocol violation send `error`, close 1003, return False."""
    msg = await ws.receive()
    if msg["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(msg.get("code", 1000))
    try:
        data = json.loads(msg["text"]) if msg.get("text") is not None else {}
    except ValueError:
        data = {}
    if data.get("type") != "start":
        await _send(ws, {"type": "error", "message": "expected a start frame"})
        await _close(ws, CLOSE_BAD_PROTOCOL, "expected start")
        return False
    if data.get("sampleRate") != SAMPLE_RATE:
        await _send(ws, {"type": "error", "message": f"sampleRate must be {SAMPLE_RATE}"})
        await _close(ws, CLOSE_BAD_PROTOCOL, "bad sampleRate")
        return False
    return True


async def _serve(ws: WebSocket, engine: SttEngine, session: DictationSession,
                 *, tick_s: float, max_session_s: float) -> None:
    if not await _handshake(ws):
        return
    warm = getattr(engine, "warm", None)
    if warm is not None:
        await asyncio.to_thread(warm)          # first use may download/load the model
    await _send(ws, {"type": "ready"})

    stop = asyncio.Event()

    async def ticker() -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=tick_s)
                return
            except asyncio.TimeoutError:
                pass
            for ev in await asyncio.to_thread(session.tick):
                await _send(ws, {"type": ev.kind, "text": ev.text})

    tick_task = asyncio.create_task(ticker())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_session_s
    disconnected = False
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            if msg["type"] == "websocket.disconnect":
                disconnected = True
                break
            if (pcm := msg.get("bytes")) is not None:
                session.feed(pcm)
                continue
            text = msg.get("text")
            if text is not None:
                try:
                    data = json.loads(text)
                except ValueError:
                    data = {}
                if data.get("type") == "stop":
                    break
    finally:
        stop.set()
        # Wait for an in-flight tick to finish rather than cancelling mid-send.
        await asyncio.gather(tick_task, return_exceptions=True)

    if disconnected:
        return
    for ev in await asyncio.to_thread(session.flush):
        await _send(ws, {"type": ev.kind, "text": ev.text})
    await _send(ws, {"type": "done"})
    await _close(ws, 1000)
```

- [ ] **Step 4: Wire the app**

In `server/app.py` imports (top of file, alongside `from server.bridge.ws import build_ws_router`):

```python
from server.stt.engine import WhisperEngine, is_available as stt_available
from server.stt.ws import build_stt_ws_router
```

In `create_app`, immediately after the VNC bridge `app.include_router(build_vnc_ws_router(...))`
(~line 798). Read the surrounding code to find the local name for settings in `create_app`
(it calls `get_settings()`; reuse that variable, do not call it twice if one exists):

```python
    # Server-side dictation — engine only exists when enabled AND the `stt` extra is
    # installed; the config endpoint mirrors that predicate so the UI never offers it otherwise.
    _stt_settings = get_settings()
    app.state.stt_engine = (
        WhisperEngine(_stt_settings.stt_model, cpu_threads=_stt_settings.stt_cpu_threads)
        if (_stt_settings.enable_stt and stt_available()) else None
    )
    app.include_router(build_stt_ws_router(
        lambda: getattr(app.state, "stt_engine", None),
        tick_s=_stt_settings.stt_tick_s,
        max_concurrent=_stt_settings.stt_max_concurrent,
        max_session_s=_stt_settings.stt_max_session_s,
    ))
```

In `_lifespan`, in the startup section (after `app.state.loop_lag_task = …` is a fine spot),
add a background warm so the first dictation does not pay the model load:

```python
    _stt = getattr(app.state, "stt_engine", None)
    if _stt is not None:
        def _warm_stt() -> None:
            try:
                _stt.warm()
                log.info("stt: model %s ready", _stt.model_name)
            except Exception as exc:  # the WS path retries and reports the error to the UI
                log.warning("stt: warm failed: %s", exc)
        threading.Thread(target=_warm_stt, name="stt-warm", daemon=True).start()
```

(`import threading` if `server/app.py` does not already import it; check `log` is the module logger name.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `<venv-python> -m pytest server/tests/test_stt_ws.py server/tests/test_app_stt_wiring.py server/tests/test_config_api.py -q`
Expected: PASS. Then `ruff check src/ server/` and `mypy server/ --ignore-missing-imports` clean.

- [ ] **Step 6: Commit**

```bash
git add server/stt/ws.py server/app.py server/tests/test_stt_ws.py server/tests/test_app_stt_wiring.py
git commit -F /tmp/msg-task4.txt   # "feat(stt): /ws/stt streaming dictation route wired into the edge app"
```

---

### Task 5: Real-engine integration test (gated)

**Files:**
- Create: `server/tests/test_stt_whisper_integration.py`
- Create: `server/tests/fixtures/jfk.flac` — copy from `/tmp/jfk.flac` (the 11 s clip from the
  openai/whisper repo test suite, 1.1 MB). If adding a binary fixture is undesirable, download
  it inside the test from `https://github.com/openai/whisper/raw/main/tests/jfk.flac` to
  `tmp_path` (it only runs when `CCSWITCH_TEST_STT=1`).

- [ ] **Step 1: Write the test**

```python
"""Real faster-whisper through DictationSession. Skipped unless the `stt` extra is
installed AND CCSWITCH_TEST_STT=1 (downloads the base.en model on first run)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("faster_whisper")
pytestmark = pytest.mark.skipif(os.environ.get("CCSWITCH_TEST_STT") != "1",
                                reason="set CCSWITCH_TEST_STT=1 to run the real whisper engine")

CLIP = Path(__file__).parent / "fixtures" / "jfk.flac"


def _pcm16(path: Path) -> bytes:
    import numpy as np
    from faster_whisper import decode_audio
    audio = decode_audio(str(path), sampling_rate=16000)
    return (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()


def test_streaming_jfk_clip_yields_interims_and_a_sane_final():
    from server.stt.engine import WhisperEngine
    from server.stt.session import DictationSession

    pcm = _pcm16(CLIP)
    session = DictationSession(WhisperEngine("base.en", cpu_threads=4))
    chunk = 16000 * 2  # 1 s
    kinds, finals = [], []
    for i in range(0, len(pcm), chunk):
        session.feed(pcm[i:i + chunk])
        for ev in session.tick():
            kinds.append(ev.kind)
            if ev.kind == "final":
                finals.append(ev.text)
    for ev in session.flush():
        kinds.append(ev.kind); finals.append(ev.text)

    assert "interim" in kinds
    text = " ".join(finals).lower()
    assert "ask not what your country can do for you" in text
    assert "what you can do for your country" in text
```

- [ ] **Step 2: Run it for real**

Run: `CCSWITCH_TEST_STT=1 <spike-venv-python> -m pytest server/tests/test_stt_whisper_integration.py -q -s`
Expected: PASS (first run downloads `Systran/faster-whisper-base.en`). Also confirm it SKIPS
under the shared venv: `/home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_stt_whisper_integration.py -q` → 1 skipped.

- [ ] **Step 3: Commit**

```bash
git add server/tests/test_stt_whisper_integration.py server/tests/fixtures/jfk.flac
git commit -F /tmp/msg-task5.txt   # "test(stt): gated real-engine integration test on the JFK clip"
```

---

### Task 6: `pcmCapture.ts` — mic → 16 kHz Int16 chunks

**Files:**
- Create: `web/src/lib/pcmCapture.ts`
- Test: `web/src/lib/__tests__/pcmCapture.test.ts`

**Interfaces:**
- Produces: `PCM_SAMPLE_RATE = 16000`, `CHUNK_SAMPLES = 1600`, `WORKLET_SOURCE: string`,
  `pcmCaptureSupported(): boolean`,
  `startPcmCapture(onChunk: (chunk: Int16Array) => void): Promise<PcmCapture>` where
  `interface PcmCapture { stop(): Promise<void> }`.

- [ ] **Step 1: Write the failing tests**

`web/src/lib/__tests__/pcmCapture.test.ts`:

```ts
import { describe, it, expect, vi, afterEach } from 'vitest'
import {
  CHUNK_SAMPLES, PCM_SAMPLE_RATE, WORKLET_SOURCE, pcmCaptureSupported, startPcmCapture,
} from '../pcmCapture'

// Evaluate the worklet source in a fake AudioWorklet realm so the conversion code
// that actually ships is what gets tested.
function loadProcessor() {
  let registered: (new () => { process(inputs: Float32Array[][]): boolean; port: { postMessage: ReturnType<typeof vi.fn> } }) | null = null
  const realm = {
    AudioWorkletProcessor: class { port = { postMessage: vi.fn() } },
    registerProcessor: (_name: string, cls: never) => { registered = cls },
  }
  new Function('AudioWorkletProcessor', 'registerProcessor', WORKLET_SOURCE)(realm.AudioWorkletProcessor, realm.registerProcessor)
  return registered!
}

afterEach(() => { vi.unstubAllGlobals() })

describe('worklet processor', () => {
  it('converts float samples to clamped Int16 and posts CHUNK_SAMPLES at a time', () => {
    const Proc = loadProcessor()
    const p = new Proc()
    const frame = new Float32Array(128)
    frame[0] = 1; frame[1] = -1; frame[2] = 0.5; frame[3] = 2 /* clipped */
    for (let i = 0; i < CHUNK_SAMPLES / 128; i++) expect(p.process([[frame]])).toBe(true)
    expect(p.port.postMessage).toHaveBeenCalledTimes(1)
    const chunk = p.port.postMessage.mock.calls[0][0] as Int16Array
    expect(chunk).toBeInstanceOf(Int16Array)
    expect(chunk.length).toBe(CHUNK_SAMPLES)
    expect([chunk[0], chunk[1], chunk[2], chunk[3]]).toEqual([32767, -32768, 16383, 32767])
  })

  it('keeps running with no input connected', () => {
    const Proc = loadProcessor()
    expect(new Proc().process([[]])).toBe(true)
  })
})

describe('startPcmCapture', () => {
  function stubAudio() {
    const tracks = [{ stop: vi.fn() }]
    const stream = { getTracks: () => tracks }
    const getUserMedia = vi.fn(async () => stream)
    vi.stubGlobal('navigator', { mediaDevices: { getUserMedia } })
    const port = { onmessage: null as null | ((e: { data: unknown }) => void) }
    const node = { port, connect: vi.fn(), disconnect: vi.fn() }
    const source = { connect: vi.fn(), disconnect: vi.fn() }
    const gain = { gain: { value: 1 }, connect: vi.fn(), disconnect: vi.fn() }
    const ctx = {
      sampleRate: PCM_SAMPLE_RATE, destination: {},
      audioWorklet: { addModule: vi.fn(async () => {}) },
      createMediaStreamSource: vi.fn(() => source),
      createGain: vi.fn(() => gain),
      resume: vi.fn(async () => {}), close: vi.fn(async () => {}),
    }
    const AudioContext = vi.fn(() => ctx)
    vi.stubGlobal('AudioContext', AudioContext)
    vi.stubGlobal('AudioWorkletNode', vi.fn(() => node))
    vi.stubGlobal('URL', { createObjectURL: vi.fn(() => 'blob:x'), revokeObjectURL: vi.fn() })
    return { tracks, getUserMedia, ctx, AudioContext, node, source, gain, port }
  }

  it('reports unsupported without getUserMedia or AudioWorkletNode', () => {
    vi.stubGlobal('navigator', {})
    expect(pcmCaptureSupported()).toBe(false)
  })

  it('opens a 16 kHz mono context, loads the worklet, and forwards chunks', async () => {
    const s = stubAudio()
    const onChunk = vi.fn()
    const cap = await startPcmCapture(onChunk)
    expect(s.getUserMedia).toHaveBeenCalledWith({
      audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
    })
    expect(s.AudioContext).toHaveBeenCalledWith({ sampleRate: PCM_SAMPLE_RATE })
    expect(s.ctx.audioWorklet.addModule).toHaveBeenCalledWith('blob:x')
    expect(s.source.connect).toHaveBeenCalledWith(s.node)
    expect(s.gain.gain.value).toBe(0)
    const chunk = new Int16Array([1, 2, 3])
    s.port.onmessage!({ data: chunk })
    expect(onChunk).toHaveBeenCalledWith(chunk)
    await cap.stop()
    expect(s.tracks[0].stop).toHaveBeenCalled()
    expect(s.ctx.close).toHaveBeenCalled()
    expect(s.source.disconnect).toHaveBeenCalled()
  })

  it('releases the mic if the worklet fails to load', async () => {
    const s = stubAudio()
    s.ctx.audioWorklet.addModule.mockRejectedValueOnce(new Error('nope'))
    await expect(startPcmCapture(vi.fn())).rejects.toThrow('nope')
    expect(s.tracks[0].stop).toHaveBeenCalled()
    expect(s.ctx.close).toHaveBeenCalled()
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `(cd web && npx vitest run src/lib/__tests__/pcmCapture.test.ts)`
Expected: FAIL — cannot resolve `../pcmCapture`.

- [ ] **Step 3: Implement**

`web/src/lib/pcmCapture.ts`:

```ts
/**
 * Microphone → 16 kHz mono Int16 PCM chunks, for the server-side dictation engine.
 *
 * The AudioContext is opened at 16 kHz so the browser resamples the mic for us; an
 * AudioWorklet (loaded from a Blob URL — no build config) converts Float32 frames to
 * Int16 and posts fixed-size chunks (~100 ms) to the main thread.
 */
export const PCM_SAMPLE_RATE = 16000
export const CHUNK_SAMPLES = 1600 // 100 ms at 16 kHz

export const WORKLET_SOURCE = `
class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super()
    this._buf = new Int16Array(${CHUNK_SAMPLES})
    this._n = 0
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0]
    if (!ch) return true
    for (let i = 0; i < ch.length; i++) {
      const s = Math.max(-1, Math.min(1, ch[i]))
      this._buf[this._n++] = s < 0 ? s * 0x8000 : s * 0x7fff
      if (this._n === this._buf.length) {
        this.port.postMessage(this._buf, [this._buf.buffer])
        this._buf = new Int16Array(${CHUNK_SAMPLES})
        this._n = 0
      }
    }
    return true
  }
}
registerProcessor('pcm-capture', PcmCaptureProcessor)
`

export interface PcmCapture {
  stop(): Promise<void>
}

export function pcmCaptureSupported(): boolean {
  return typeof navigator !== 'undefined'
    && typeof navigator.mediaDevices?.getUserMedia === 'function'
    && typeof AudioWorkletNode !== 'undefined'
}

export async function startPcmCapture(onChunk: (chunk: Int16Array) => void): Promise<PcmCapture> {
  if (!pcmCaptureSupported()) throw new Error('audio-capture-unsupported')
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
  })
  const ctx = new AudioContext({ sampleRate: PCM_SAMPLE_RATE })
  const release = async () => {
    stream.getTracks().forEach((t) => t.stop())
    await ctx.close()
  }
  try {
    const url = URL.createObjectURL(new Blob([WORKLET_SOURCE], { type: 'application/javascript' }))
    try { await ctx.audioWorklet.addModule(url) } finally { URL.revokeObjectURL(url) }
    await ctx.resume()
  } catch (e) {
    await release()
    throw e
  }
  const source = ctx.createMediaStreamSource(stream)
  const node = new AudioWorkletNode(ctx, 'pcm-capture')
  // A worklet with no downstream connection may be culled; route through a muted gain.
  const sink = ctx.createGain()
  sink.gain.value = 0
  node.port.onmessage = (e: MessageEvent) => onChunk(e.data as Int16Array)
  source.connect(node)
  node.connect(sink)
  sink.connect(ctx.destination)
  return {
    async stop() {
      node.port.onmessage = null
      source.disconnect()
      node.disconnect()
      sink.disconnect()
      await release()
    },
  }
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `(cd web && npx vitest run src/lib/__tests__/pcmCapture.test.ts && npx tsc --noEmit && npx eslint src/lib/pcmCapture.ts src/lib/__tests__/pcmCapture.test.ts)`
Expected: PASS, no type or lint errors.

- [ ] **Step 5: Commit**

```bash
git add web/src/lib/pcmCapture.ts web/src/lib/__tests__/pcmCapture.test.ts
git commit -F /tmp/msg-task6.txt   # "feat(voice): AudioWorklet PCM capture at 16 kHz"
```

---

### Task 7: `useServerDictation` hook

**Files:**
- Create: `web/src/lib/useServerDictation.ts`
- Test: `web/src/lib/__tests__/useServerDictation.test.tsx`

**Interfaces:**
- Consumes: `startPcmCapture`, `pcmCaptureSupported`, `PCM_SAMPLE_RATE` (Task 6);
  `UseDictation`, `UseDictationOptions` from `web/src/lib/useDictation.ts` (existing).
- Produces: `useServerDictation({ onResult }: UseDictationOptions): UseDictation`,
  `STT_SOCKET_PATH = '/ws/stt'`.

- [ ] **Step 1: Write the failing tests**

`web/src/lib/__tests__/useServerDictation.test.tsx`:

```tsx
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'

vi.mock('../pcmCapture', () => ({
  PCM_SAMPLE_RATE: 16000,
  pcmCaptureSupported: vi.fn(() => true),
  startPcmCapture: vi.fn(),
}))
import { pcmCaptureSupported, startPcmCapture } from '../pcmCapture'
import { useServerDictation, STT_SOCKET_PATH } from '../useServerDictation'

class MockSocket {
  static instances: MockSocket[] = []
  static OPEN = 1; static CLOSED = 3
  readyState = 0
  binaryType = ''
  sent: unknown[] = []
  onopen: (() => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  onerror: (() => void) | null = null
  onclose: ((e: { code: number; reason: string }) => void) | null = null
  constructor(public url: string) { MockSocket.instances.push(this) }
  send(d: unknown) { this.sent.push(d) }
  close(code = 1000, reason = '') { this.readyState = 3; this.onclose?.({ code, reason }) }
  open() { this.readyState = 1; this.onopen?.() }
  message(m: object) { this.onmessage?.({ data: JSON.stringify(m) }) }
}

let capture: { stop: ReturnType<typeof vi.fn>; onChunk: (c: Int16Array) => void }
beforeEach(() => {
  MockSocket.instances = []
  vi.stubGlobal('WebSocket', MockSocket)
  ;(startPcmCapture as unknown as ReturnType<typeof vi.fn>).mockImplementation(async (onChunk: (c: Int16Array) => void) => {
    capture = { stop: vi.fn(async () => {}), onChunk }
    return capture
  })
})
afterEach(() => { vi.unstubAllGlobals(); vi.clearAllMocks() })

const flush = () => act(async () => { await Promise.resolve(); await Promise.resolve() })

describe('useServerDictation', () => {
  it('reports unsupported when capture is unsupported', () => {
    ;(pcmCaptureSupported as unknown as ReturnType<typeof vi.fn>).mockReturnValueOnce(false)
    const { result } = renderHook(() => useServerDictation({ onResult: vi.fn() }))
    expect(result.current.supported).toBe(false)
  })

  it('opens the socket, sends start, streams chunks as binary', async () => {
    const { result } = renderHook(() => useServerDictation({ onResult: vi.fn() }))
    act(() => { result.current.start() })
    expect(result.current.listening).toBe(true)
    const ws = MockSocket.instances[0]
    expect(ws.url).toBe(STT_SOCKET_PATH)
    expect(ws.binaryType).toBe('arraybuffer')
    act(() => { ws.open() })
    expect(ws.sent[0]).toBe(JSON.stringify({ type: 'start', sampleRate: 16000 }))
    await flush()
    expect(startPcmCapture).toHaveBeenCalledTimes(1)
    const chunk = new Int16Array([1, 2])
    act(() => { capture.onChunk(chunk) })
    expect(ws.sent[1]).toBe(chunk)
  })

  it('maps interim and final messages to onResult', () => {
    const onResult = vi.fn()
    const { result } = renderHook(() => useServerDictation({ onResult }))
    act(() => { result.current.start() })
    const ws = MockSocket.instances[0]
    act(() => { ws.open(); ws.message({ type: 'ready' }); ws.message({ type: 'interim', text: 'hel' }) })
    expect(onResult).toHaveBeenCalledWith('hel', false)
    act(() => { ws.message({ type: 'final', text: 'hello' }) })
    expect(onResult).toHaveBeenCalledWith('hello', true)
  })

  it('stop releases the mic, sends stop, and closes after done', async () => {
    const { result } = renderHook(() => useServerDictation({ onResult: vi.fn() }))
    act(() => { result.current.start() })
    const ws = MockSocket.instances[0]
    act(() => { ws.open() })
    await flush()
    act(() => { result.current.stop() })
    expect(capture.stop).toHaveBeenCalled()
    expect(ws.sent.at(-1)).toBe(JSON.stringify({ type: 'stop' }))
    expect(result.current.listening).toBe(false)
    act(() => { ws.message({ type: 'done' }) })
    expect(ws.readyState).toBe(MockSocket.CLOSED)
    // a fresh start after done opens a new socket
    act(() => { result.current.start() })
    expect(MockSocket.instances).toHaveLength(2)
  })

  it('toggle is start when idle, stop when listening, ignored while finishing', async () => {
    const { result } = renderHook(() => useServerDictation({ onResult: vi.fn() }))
    act(() => { result.current.toggle() })
    expect(MockSocket.instances).toHaveLength(1)
    act(() => { MockSocket.instances[0].open() })
    await flush()
    act(() => { result.current.toggle() })       // stop
    act(() => { result.current.toggle() })       // finishing: ignored
    expect(MockSocket.instances).toHaveLength(1)
    expect(MockSocket.instances[0].sent.filter((s) => s === JSON.stringify({ type: 'stop' }))).toHaveLength(1)
  })

  it('surfaces server errors and 4xxx close reasons', () => {
    const { result } = renderHook(() => useServerDictation({ onResult: vi.fn() }))
    act(() => { result.current.start() })
    const ws = MockSocket.instances[0]
    act(() => { ws.open(); ws.message({ type: 'error', message: 'model download failed' }) })
    expect(result.current.error).toBe('model download failed')
    act(() => { ws.close(4004, 'stt disabled') })
    expect(result.current.listening).toBe(false)
    expect(result.current.error).toBe('stt disabled')
  })

  it('surfaces a denied microphone and tears down', async () => {
    ;(startPcmCapture as unknown as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      Object.assign(new Error('denied'), { name: 'NotAllowedError' }))
    const { result } = renderHook(() => useServerDictation({ onResult: vi.fn() }))
    act(() => { result.current.start() })
    act(() => { MockSocket.instances[0].open() })
    await flush()
    expect(result.current.error).toBe('NotAllowedError')
    expect(result.current.listening).toBe(false)
    expect(MockSocket.instances[0].readyState).toBe(MockSocket.CLOSED)
  })

  it('tears down on unmount', async () => {
    const { result, unmount } = renderHook(() => useServerDictation({ onResult: vi.fn() }))
    act(() => { result.current.start() })
    act(() => { MockSocket.instances[0].open() })
    await flush()
    unmount()
    expect(capture.stop).toHaveBeenCalled()
    expect(MockSocket.instances[0].readyState).toBe(MockSocket.CLOSED)
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `(cd web && npx vitest run src/lib/__tests__/useServerDictation.test.tsx)`
Expected: FAIL — cannot resolve `../useServerDictation`.

- [ ] **Step 3: Implement**

`web/src/lib/useServerDictation.ts`:

```ts
import { useCallback, useEffect, useRef, useState } from 'react'
import type { UseDictation, UseDictationOptions } from './useDictation'
import { PCM_SAMPLE_RATE, pcmCaptureSupported, startPcmCapture, type PcmCapture } from './pcmCapture'

/** Same-origin path; the existing session socket uses a relative `/ws` the same way. */
export const STT_SOCKET_PATH = '/ws/stt'
const STOP_GRACE_MS = 3000

type ServerMsg =
  | { type: 'ready' }
  | { type: 'interim' | 'final'; text: string }
  | { type: 'error'; message: string }
  | { type: 'done' }

/**
 * Dictation via the edge's streaming Whisper engine (`/ws/stt`): mic PCM goes up as
 * binary frames, `interim`/`final` transcript events come back. Same shape as the
 * Web Speech `useDictation` so `useDictationField` can drive either.
 */
export function useServerDictation({ onResult }: UseDictationOptions): UseDictation {
  const [supported] = useState(() => pcmCaptureSupported())
  const [listening, setListening] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const captureRef = useRef<PcmCapture | null>(null)
  const finishingRef = useRef(false)
  const onResultRef = useRef(onResult)
  useEffect(() => { onResultRef.current = onResult }, [onResult])

  const releaseMic = useCallback(() => {
    const cap = captureRef.current
    captureRef.current = null
    void cap?.stop()
  }, [])

  const teardown = useCallback((ws: WebSocket) => {
    if (wsRef.current !== ws) return
    wsRef.current = null
    finishingRef.current = false
    releaseMic()
    setListening(false)
  }, [releaseMic])

  const stop = useCallback(() => {
    const ws = wsRef.current
    if (!ws || finishingRef.current) return
    finishingRef.current = true
    releaseMic()
    setListening(false)
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'stop' }))
      // The server closes after `done`; if it never arrives, do not hang forever.
      setTimeout(() => { if (ws.readyState !== WebSocket.CLOSED) ws.close() }, STOP_GRACE_MS)
    } else {
      ws.close()
    }
  }, [releaseMic])

  const start = useCallback(() => {
    if (wsRef.current) return
    setError(null)
    const ws = new WebSocket(STT_SOCKET_PATH)
    ws.binaryType = 'arraybuffer'
    wsRef.current = ws
    finishingRef.current = false
    setListening(true)

    ws.onopen = () => {
      ws.send(JSON.stringify({ type: 'start', sampleRate: PCM_SAMPLE_RATE }))
      startPcmCapture((chunk) => { if (ws.readyState === WebSocket.OPEN) ws.send(chunk) })
        .then((cap) => {
          if (wsRef.current !== ws || finishingRef.current) { void cap.stop(); return }
          captureRef.current = cap
        })
        .catch((e: unknown) => {
          setError(e instanceof Error && e.name ? e.name : 'capture-error')
          ws.close()
        })
    }
    ws.onmessage = (ev: MessageEvent) => {
      let msg: ServerMsg
      try { msg = JSON.parse(String(ev.data)) as ServerMsg } catch { return }
      switch (msg.type) {
        case 'interim': onResultRef.current(msg.text, false); break
        case 'final': onResultRef.current(msg.text, true); break
        case 'error': setError(msg.message || 'stt-error'); break
        case 'done': ws.close(); break
        case 'ready': break
      }
    }
    ws.onerror = () => { setError('socket-error') }
    ws.onclose = (ev: CloseEvent) => {
      if (ev.code >= 4000) setError(ev.reason || `stt-${ev.code}`)
      teardown(ws)
    }
  }, [teardown])

  const toggle = useCallback(() => {
    if (wsRef.current) stop()
    else start()
  }, [start, stop])

  useEffect(() => () => {
    const ws = wsRef.current
    wsRef.current = null
    releaseMic()
    ws?.close()
  }, [releaseMic])

  return { supported, listening, error, start, stop, toggle }
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `(cd web && npx vitest run src/lib/__tests__/useServerDictation.test.tsx && npx tsc --noEmit && npx eslint src/lib/useServerDictation.ts src/lib/__tests__/useServerDictation.test.tsx)`
Expected: PASS, clean.

- [ ] **Step 5: Commit**

```bash
git add web/src/lib/useServerDictation.ts web/src/lib/__tests__/useServerDictation.test.tsx
git commit -F /tmp/msg-task7.txt   # "feat(voice): useServerDictation streams PCM to /ws/stt"
```

---

### Task 8: Engine selection in `useDictationField` + capability store fed from config

**Files:**
- Create: `web/src/lib/sttCapability.ts`
- Modify: `web/src/lib/useDictationField.ts`
- Modify: `web/src/api/hooks.ts:150-153` (`ConfigInfo` gains `sttEnabled?: boolean`)
- Modify: `web/src/shell/AppShell.tsx` (~line 359, where `useConfig()` is already called)
- Test: `web/src/lib/__tests__/sttCapability.test.tsx`, `web/src/lib/__tests__/useDictationField.test.tsx` (append), `web/src/shell/__tests__/…` (see step 1; add a small test that AppShell syncs the store)

**Why a store and not `useConfig()` inside the hook:** five Composer test files render the
component with no QueryClientProvider and without mocking `useDictationField`; a react-query
hook inside it would break them all. The store is a derived, subscribe-able cache of one field
of `/api/config`, written from the one place that already reads config.

**Interfaces:**
- Produces: `setServerSttEnabled(v: boolean): void`, `useServerSttEnabled(): boolean`,
  `getServerSttEnabled(): boolean` in `sttCapability.ts`.

- [ ] **Step 1: Write the failing tests**

`web/src/lib/__tests__/sttCapability.test.tsx`:

```tsx
import { describe, it, expect, afterEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { getServerSttEnabled, setServerSttEnabled, useServerSttEnabled } from '../sttCapability'

afterEach(() => { setServerSttEnabled(false) })

describe('sttCapability', () => {
  it('defaults to false and notifies subscribers on change', () => {
    expect(getServerSttEnabled()).toBe(false)
    const { result } = renderHook(() => useServerSttEnabled())
    expect(result.current).toBe(false)
    act(() => { setServerSttEnabled(true) })
    expect(result.current).toBe(true)
  })
})
```

Append to `web/src/lib/__tests__/useDictationField.test.tsx` (add these imports at the top:
`import { setServerSttEnabled } from '../sttCapability'` and
`vi.mock('../useServerDictation', () => ({ useServerDictation: vi.fn() }))` plus
`import { useServerDictation } from '../useServerDictation'`; in `afterEach` also call
`setServerSttEnabled(false)`; and give the mock a default implementation in a `beforeEach`:
`(useServerDictation as unknown as Mock).mockReturnValue({ supported: false, listening: false, error: null, start: vi.fn(), stop: vi.fn(), toggle: vi.fn() })`):

```tsx
describe('engine selection', () => {
  it('uses the server engine when the edge reports sttEnabled and capture is supported', () => {
    installMock()
    const server = { supported: true, listening: false, error: null, start: vi.fn(), stop: vi.fn(), toggle: vi.fn() }
    ;(useServerDictation as unknown as Mock).mockReturnValue(server)
    setServerSttEnabled(true)
    render(<Harness initial="" />)
    act(() => { api.start() })
    expect(server.start).toHaveBeenCalled()
    expect(instance).toBeUndefined()          // Web Speech never constructed
  })

  it('falls back to Web Speech when the server engine is unavailable in this browser', () => {
    installMock()
    ;(useServerDictation as unknown as Mock).mockReturnValue({ supported: false, listening: false, error: null, start: vi.fn(), stop: vi.fn(), toggle: vi.fn() })
    setServerSttEnabled(true)
    render(<Harness initial="" />)
    act(() => { api.start() })
    expect(instance.start).toHaveBeenCalled()
  })

  it('uses Web Speech when the edge has STT disabled', () => {
    installMock()
    ;(useServerDictation as unknown as Mock).mockReturnValue({ supported: true, listening: false, error: null, start: vi.fn(), stop: vi.fn(), toggle: vi.fn() })
    render(<Harness initial="" />)
    act(() => { api.start() })
    expect(instance.start).toHaveBeenCalled()
  })

  it('inserts server results at the caret exactly like Web Speech results', () => {
    installMock()
    let captured: ((t: string, f: boolean) => void) | null = null
    ;(useServerDictation as unknown as Mock).mockImplementation(({ onResult }: { onResult: (t: string, f: boolean) => void }) => {
      captured = onResult
      return { supported: true, listening: true, error: null, start: vi.fn(), stop: vi.fn(), toggle: vi.fn() }
    })
    setServerSttEnabled(true)
    render(<Harness initial="" />)
    act(() => { api.start() })
    act(() => { captured!('hel', false) })
    expect(currentValue).toBe('hel')
    act(() => { captured!('hello', true) })
    expect(currentValue).toBe('hello ')
  })
})
```

Note: `instance` is `let instance: MockRecognition` — for the first test to assert it is
undefined, reset it in `beforeEach` (`instance = undefined as unknown as MockRecognition`)
and check with `expect(instance).toBeUndefined()`.

AppShell sync test — find the existing AppShell test file(s) under `web/src/shell/` (grep
`useConfig` mocks there). Add one test that mocks `useConfig` to return
`{ data: { repoRoot: '/', adoptionEnabled: false, sttEnabled: true } }`, renders AppShell with
the file's existing helper, and asserts `getServerSttEnabled() === true` after render. If
AppShell tests mock `@/api/hooks` wholesale, extend that mock's `useConfig` return.

- [ ] **Step 2: Run to verify they fail**

Run: `(cd web && npx vitest run src/lib/__tests__/sttCapability.test.tsx src/lib/__tests__/useDictationField.test.tsx)`
Expected: FAIL — cannot resolve `../sttCapability` / `../useServerDictation` not used.

- [ ] **Step 3: Implement**

`web/src/lib/sttCapability.ts`:

```ts
import { useSyncExternalStore } from 'react'

/**
 * Whether the edge offers the server-side dictation engine (`/api/config.sttEnabled`).
 *
 * A tiny external store rather than `useConfig()` inside `useDictationField`: that hook
 * is rendered by Composer/DispatchBar in many tests (and code paths) with no
 * QueryClientProvider. AppShell — which already reads config — writes it once per change.
 */
let enabled = false
const listeners = new Set<() => void>()

export function getServerSttEnabled(): boolean { return enabled }

export function setServerSttEnabled(v: boolean): void {
  if (enabled === v) return
  enabled = v
  listeners.forEach((l) => l())
}

function subscribe(l: () => void): () => void {
  listeners.add(l)
  return () => { listeners.delete(l) }
}

export function useServerSttEnabled(): boolean {
  return useSyncExternalStore(subscribe, getServerSttEnabled, getServerSttEnabled)
}
```

`web/src/lib/useDictationField.ts` — replace the `const dictation = useDictation({ onResult })`
line with:

```ts
  // Both hooks are called unconditionally (rules of hooks); only the selected one is driven.
  const browser = useDictation({ onResult })
  const server = useServerDictation({ onResult })
  const dictation = useServerSttEnabled() && server.supported ? server : browser
```

and add imports `import { useServerDictation } from './useServerDictation'` and
`import { useServerSttEnabled } from './sttCapability'`.

`web/src/api/hooks.ts` `ConfigInfo`:

```ts
export interface ConfigInfo {
  repoRoot: string
  adoptionEnabled: boolean
  /** Edge runs the server-side dictation engine (`CCSWITCH_ENABLE_STT` + `stt` extra). */
  sttEnabled?: boolean
}
```

`web/src/shell/AppShell.tsx`, after `const { data: config } = useConfig()`:

```ts
  // Publish the dictation-engine capability for useDictationField (see lib/sttCapability.ts).
  useEffect(() => { setServerSttEnabled(!!config?.sttEnabled) }, [config?.sttEnabled])
```

with `import { setServerSttEnabled } from '@/lib/sttCapability'` (and `useEffect` if not imported).

- [ ] **Step 4: Run the FULL web suite**

Run: `(cd web && npx vitest run && npx tsc --noEmit && npx eslint .)`
Expected: all green — in particular every `Composer*.test.tsx`, `DispatchBar*.test.tsx`, and
`shell/` test still passes.

- [ ] **Step 5: Commit**

```bash
git add web/src/lib/sttCapability.ts web/src/lib/useDictationField.ts web/src/api/hooks.ts web/src/shell/AppShell.tsx web/src/lib/__tests__/
git add web/src/shell   # the AppShell test you touched
git commit -F /tmp/msg-task8.txt   # "feat(voice): pick the server dictation engine when the edge offers it"
```

---

### Task 9: Docs, full gates, PR

**Files:**
- Modify: `CLAUDE.md` (new short section "Server-side dictation" after "Copilot account type")
- Modify: `deploy/README.md` (env + first-run model download note, after the HTTPS section)

- [ ] **Step 1: Write the docs**

`CLAUDE.md` section:

```markdown
## Server-side dictation (STT)

The mic button streams 16 kHz PCM to `/ws/stt` (`server/stt/`) where `faster-whisper`
(`base.en`, int8, CPU) transcribes it with Silero VAD endpointing; Web Speech is the fallback
when the edge does not advertise `sttEnabled` in `GET /api/config`.

- Enable with `CCSWITCH_ENABLE_STT=true` on the **edge** (needs the `stt` extra: the deploy
  controller installs `.[server,stt]`; CI/runners stay on `[server]`). First start downloads
  `Systran/faster-whisper-base.en` into the HuggingFace cache; the lifespan warms the model
  in a background thread.
- Knobs: `CCSWITCH_STT_MODEL` (`base.en`; `small.en` is ~1 s/tick on CPU — too slow),
  `CCSWITCH_STT_CPU_THREADS` (4), `CCSWITCH_STT_TICK_S` (1.0), `CCSWITCH_STT_MAX_CONCURRENT`
  (3), `CCSWITCH_STT_MAX_SESSION_S` (600).
- Policy lives in `server/stt/session.py` (pure, fake-engine tests); the real engine test is
  `server/tests/test_stt_whisper_integration.py`, gated on `CCSWITCH_TEST_STT=1`.
- Audio is never persisted or logged.
```

`deploy/README.md`, a new subsection after "HTTPS via `tailscale serve`":

```markdown
## Server-side dictation (optional)

Set `CCSWITCH_ENABLE_STT=true` in `/etc/ccswitch/ccswitch.env` and restart the edge
(`ccswitch.service` only — the daemon is untouched). The venv needs the `stt` extra
(`pip install -e '.[server,stt]'`, ~440 MB; the deploy controller does this automatically
when `pyproject.toml` changes). The first dictation after enabling downloads the
`base.en` model (~150 MB) into `~/.cache/huggingface`; the lifespan pre-warms it so the mic is
ready within ~10 s of startup. `GET /api/config` shows `sttEnabled: true` when it is live.
```

- [ ] **Step 2: Run every gate**

```bash
ruff check src/ server/
<spike-venv-python> -m mypy server/ --ignore-missing-imports
<venv-python> -m pytest tests/ -q
<venv-python> -m pytest server/tests/ -q -n auto --dist loadfile --timeout=300 2>&1 | tee /tmp/stt-server-suite.log
(cd web && npx vitest run && npx tsc --noEmit && npx eslint .)
```

Expected: all green (the server suite takes ~20+ min; run it in the background and poll the log).

- [ ] **Step 3: Commit, push, PR**

```bash
git add CLAUDE.md deploy/README.md
git commit -F /tmp/msg-task9.txt   # "docs(stt): enable/operate server-side dictation"
git push -u origin feat/server-side-dictation
gh pr create --title "feat(voice): server-side streaming dictation (faster-whisper) replaces laggy Web Speech" --body-file /tmp/pr-body.md
gh pr ready <#>; gh pr merge <#> --squash --auto
```

PR body: problem (Web Speech lag, both clients Chrome → Google cloud shim), options table,
spike numbers, what changed (backend/frontend), how to enable, test evidence, and the Claude
Code trailer.

---

## Self-review

- **Spec coverage:** engine (T1), session policy incl. every rule (T2), settings + config
  endpoint (T3), WS protocol/guards/warm/wiring (T4), real-engine test (T5), capture (T6),
  server hook (T7), engine selection + `ConfigInfo` (T8), ops docs + deploy extra (T1, T9). ✔
- **Placeholders:** none; every step has code.
- **Type consistency:** `Event.kind` ∈ {"interim","final"} is what `ws.py` forwards as
  `type`; `UseDictation` shape shared by both hooks; `SAMPLE_RATE`/`PCM_SAMPLE_RATE` both 16000;
  `sttEnabled` name identical in `server/api/config.py`, `ConfigInfo`, and AppShell.
