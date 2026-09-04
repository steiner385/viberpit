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
    def _load_fake() -> _FakeModel:
        loads.append(1)
        return fake
    monkeypatch.setattr(e, "_load_model", _load_fake)
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
