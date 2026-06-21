import asyncio, base64, os
from unittest.mock import patch
import pytest
from server.tests.fakes.fake_factory import fake_sdk_factory

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")

@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path))

def test_send_prompt_associates_and_resolves(tmp_path):
    from server.sessions.orchestrator import SessionOrchestrator
    from server.sessions import uploads

    captured = {}
    class FakeDriver:
        async def send_prompt(self, text, images=None):
            captured["text"] = text
            captured["images"] = images
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)
    orch._drivers["s1"] = FakeDriver()
    ref = uploads.stage_image("image/png", PNG)  # stage it first
    asyncio.run(orch.send_prompt("s1", "hi", [dict(ref, name="x.png")]))
    assert captured["text"] == "hi"
    img = captured["images"][0]
    assert img["sha"] == ref["sha"] and img["url"] == f"/api/sessions/s1/images/{ref['sha']}"
    assert os.path.exists(img["path"])

def test_send_prompt_skips_missing_sha(tmp_path):
    from server.sessions.orchestrator import SessionOrchestrator
    captured = {}
    class FakeDriver:
        async def send_prompt(self, text, images=None):
            captured["images"] = images
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)
    orch._drivers["s1"] = FakeDriver()
    asyncio.run(orch.send_prompt("s1", "hi",
                [{"sha": "ghost", "mime": "image/png", "name": "x"}]))
    assert captured["images"] == []  # missing staged file → dropped, text still sent


def test_send_prompt_truncates_images_to_max(tmp_path):
    """send_prompt must cap the images list to MAX_IMAGES (8) even if 10 refs are passed."""
    from server.sessions.orchestrator import SessionOrchestrator
    from server.sessions import uploads
    # Stage 10 images (all same bytes — stage_image deduplicates by SHA, so use distinct data)
    refs = []
    for i in range(10):
        raw = PNG + bytes([i])  # make each image unique so SHA differs
        ref = uploads.stage_image("image/png", raw)
        refs.append(dict(ref, name=f"img{i}.png"))

    captured = {}
    class FakeDriver:
        async def send_prompt(self, text, images=None):
            captured["images"] = images

    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)
    orch._drivers["s1"] = FakeDriver()
    asyncio.run(orch.send_prompt("s1", "many images", refs))
    # The driver must receive exactly MAX_IMAGES (8) items, not 10
    assert len(captured["images"]) == uploads.MAX_IMAGES
