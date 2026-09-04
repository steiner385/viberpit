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
            print(f"event: {ev.kind!r} {ev.text!r}")
            if ev.kind == "final":
                finals.append(ev.text)
    for ev in session.flush():
        kinds.append(ev.kind)
        print(f"event: {ev.kind!r} {ev.text!r}")
        finals.append(ev.text)

    assert "interim" in kinds
    text = " ".join(finals).lower()
    print(f"final transcript: {text!r}")
    assert "ask not what your country can do for you" in text
    assert "what you can do for your country" in text
