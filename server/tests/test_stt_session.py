"""DictationSession: buffer → VAD endpointing → interim/final events."""
from __future__ import annotations

from server.stt.session import DictationSession, Event

RATE = 16000


def pcm(seconds: float) -> bytes:
    return b"\x01\x00" * int(seconds * RATE)


class FakeEngine:
    """Scriptable engine: `segments` is returned by speech_segments; transcribe
    returns `text` and records the byte lengths it was asked to transcribe."""

    def __init__(self, segments=None, text="hello", track_buffer: bool = False):
        self.segments = segments or []
        self.text = text
        self.transcribed: list[int] = []
        self.track_buffer = track_buffer

    def speech_segments(self, pcm: bytes):
        if self.track_buffer:
            # Speech spans entire buffer
            return [(0.0, len(pcm) / (RATE * 2))]
        return self.segments

    def transcribe(self, pcm: bytes) -> str:
        self.transcribed.append(len(pcm))
        return self.text


def test_tick_without_new_audio_is_a_noop():
    eng = FakeEngine(segments=[(0.0, 1.0)], track_buffer=True)
    s = DictationSession(eng)
    assert s.tick() == []
    s.feed(pcm(1.0))
    assert s.tick() == [Event("interim", "hello")]
    assert s.tick() == []          # nothing new since the last tick
    assert eng.transcribed == [len(pcm(1.0))]


def test_duplicate_interim_is_suppressed_until_text_changes():
    eng = FakeEngine(segments=[(0.0, 1.0)], track_buffer=True)
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


def test_trailing_silence_commits_even_after_an_interim_was_shown():
    eng = FakeEngine(segments=[(0.0, 1.0)], text="hello")
    s = DictationSession(eng)
    s.feed(pcm(1.0)); assert s.tick() == [Event("interim", "hello")]
    s.feed(pcm(1.0))                       # 1 s of silence after the speech
    assert s.tick() == [Event("final", "hello")]
    assert s.buffered_s == 1.0
