import asyncio, base64
from server.sessions.driver import SessionDriver
from server.sessions.permissions import PermissionRegistry
from server.sessions.events import UserMessage, Status
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.fakes import fake_sdk as f
from claude_agent_sdk import TextBlock

PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
           "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")

class RecordingSDK:
    def __init__(self):
        self.queried = []
    async def __aenter__(self): return self
    async def __aexit__(self, *_): return False
    async def query(self, prompt):
        # prompt is an async-iterable of message dicts when images are present
        if isinstance(prompt, str):
            self.queried.append(("str", prompt)); return
        async for m in prompt:
            self.queried.append(("blocks", m))
    async def receive_response(self):
        yield f.result(uuid="t1")
    async def interrupt(self): pass
    async def set_permission_mode(self, mode): pass

def test_images_sent_as_content_blocks(tmp_path):
    sdk = RecordingSDK()
    def factory(cwd, cfg, can_use_tool, mode, resume): return sdk
    img_path = tmp_path / "abc.png"
    img_path.write_bytes(base64.b64decode(PNG_B64))
    images = [{"sha": "abc", "mime": "image/png", "name": "shot.png",
               "path": str(img_path), "url": "/api/sessions/s/images/abc"}]
    async def run():
        d = SessionDriver("s", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=factory, registry=PermissionRegistry(), identity="me")
        await d.start()
        seen = []
        await d.send_prompt("look at this", images)
        async for e in d.events():
            seen.append(e)
            if isinstance(e, Status) and e.state == "idle":
                break
        await d.aclose()
        return seen
    seen = asyncio.run(run())
    # The query got a content-block message, not a bare string.
    kind, msg = sdk.queried[0]
    assert kind == "blocks"
    blocks = msg["message"]["content"]
    assert blocks[0] == {"type": "text", "text": "look at this"}
    assert blocks[1]["type"] == "image"
    assert blocks[1]["source"]["data"] == PNG_B64
    # The live UserMessage echoes the image url for rendering.
    um = next(e for e in seen if isinstance(e, UserMessage))
    assert um.images == [{"url": "/api/sessions/s/images/abc",
                          "mime": "image/png", "name": "shot.png"}]

def test_text_only_unchanged(tmp_path):
    msgs = [f.stream_start(uuid="t1"), f.assistant(TextBlock(text="hi"), uuid="t1"), f.result(uuid="t1")]
    def factory(cwd, cfg, cut, mode, resume): return FakeSDKClient(msgs, on_can_use_tool=cut)
    async def run():
        d = SessionDriver("s2", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=factory, registry=PermissionRegistry(), identity="me")
        await d.start(); await d.send_prompt("hello")  # no images arg
        async for e in d.events():
            if isinstance(e, Status) and e.state == "idle":
                break
        await d.aclose()
    asyncio.run(run())  # must not raise
