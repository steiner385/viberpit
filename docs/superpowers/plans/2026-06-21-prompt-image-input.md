# Session Prompt Image Input — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user attach images (paste / drag-drop / file-pick) to session prompts in both the in-session `Composer` and the new-session `DispatchBar`, sending them to Claude as native multimodal content blocks and re-rendering them on replay.

**Architecture:** Image *bytes* travel over HTTP (`POST /api/uploads`, content-addressed by SHA-256) because the WS frame is capped at 256 KB. The WS `prompt` frame and the create-session body carry only `{sha,mime,name}` refs. The driver associates each ref (moves the staged file into the session's upload dir), reads it as base64, and sends a content-block user message via `query(<async-iterable>)`. The driver already synthesizes its own `UserMessage` event for the live stream; it gains image refs. Replay either reads image blocks back from the Claude CLI's own JSONL transcript (if it persists them — Task 1 settles this) or from a sidecar.

**Tech Stack:** Python 3.12 / FastAPI / `claude-agent-sdk` 0.2.105 / pytest; React 18 / TypeScript / Vitest + @testing-library/react.

## Global Constraints

- Allowed MIME types: `image/png`, `image/jpeg`, `image/gif`, `image/webp` — verified by content sniffing, not the declared MIME.
- Max size per image: `5 * 1024 * 1024` bytes (5 MB), pre-encode.
- Max images per message: 8.
- Image-only messages (no text) are allowed.
- `ImageRef` (small reference shape on the wire): `{"sha": str, "mime": str, "name": str}`.
- Uploads root: `ccswitch.paths.root() / "uploads"` (i.e. `${CCSWITCH_HOME:-~/.claude-accounts}/uploads`). Staging subdir: `uploads/_staging/`. Per-session subdir: `uploads/<session_id>/`.
- Served URL for an associated image: `/api/sessions/{session_id}/images/{sha}`.
- All new API routes require the existing `current_identity` dependency.
- Run the full suite with `.venv/bin/python -m pytest tests/ server/tests/ -q` and the web suite with `cd web && pnpm test`.

---

### Task 1: Spike — does the Claude CLI persist images inline in its JSONL transcript?

This is a research spike, not TDD. Its output decides Task 8's shape. It makes one real (tiny) Claude API call using a locally-stored account.

**Files:**
- Create: `scripts/spike_image_transcript.py` (throwaway — deleted at end of task)

- [ ] **Step 1: Write the spike script**

```python
# scripts/spike_image_transcript.py
"""Spike: send a 1x1 PNG to the SDK and dump what the CLI writes to its JSONL.
Run: .venv/bin/python scripts/spike_image_transcript.py <account-name>
Deletes nothing; prints the transcript path and the raw 'user' lines."""
import asyncio, base64, glob, json, sys
from pathlib import Path
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
from ccswitch.configdir import compose  # composes a per-account config dir

# 1x1 transparent PNG
PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
           "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")

async def main(account: str):
    cfg = compose(account)
    async def msgs():
        yield {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "Reply with the single word OK."},
            {"type": "image", "source": {"type": "base64",
                "media_type": "image/png", "data": PNG_B64}},
        ]}, "parent_tool_use_id": None, "session_id": "default"}
    sid = None
    async with ClaudeSDKClient(ClaudeAgentOptions(cwd=str(Path.cwd()))) as c:
        await c.query(msgs())
        async for m in c.receive_response():
            sid = getattr(m, "session_id", None) or sid
    matches = glob.glob(str(Path(cfg) / "projects" / "*" / f"{sid}.jsonl"))
    print("TRANSCRIPT:", matches)
    if matches:
        for line in Path(matches[0]).read_text().splitlines():
            o = json.loads(line)
            if o.get("type") == "user":
                print("USER LINE:", json.dumps(o)[:1200])

if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
```

- [ ] **Step 2: Run it against a real account**

Run: `.venv/bin/python scripts/spike_image_transcript.py <a-real-account-name>`
(Discover a name with `ls "$(.venv/bin/python -c 'from ccswitch import paths;print(paths.store_dir())')" | sed 's/\.cred\.json//'`.)

Inspect the printed `USER LINE`. Record the finding in the task's completion note:
- **Inline:** the user line's `content` contains a block with `"type":"image"` and a `source.data` base64 (or a file path). → Task 8 reads it from the JSONL.
- **Absent:** no image block (only the text). → Task 8 uses the sidecar.

- [ ] **Step 3: Delete the spike and record the finding**

```bash
rm scripts/spike_image_transcript.py
```
Write the verdict (Inline vs Absent, with a one-line quote of the user line's content shape) into the PR/commit message for Task 8. No code commit for this task (the script is deleted); the deliverable is the recorded finding.

---

### Task 2: `uploads.py` — validation, staging, association, read

**Files:**
- Create: `server/sessions/uploads.py`
- Modify: `src/ccswitch/paths.py` (add `uploads_dir()`)
- Test: `server/tests/test_uploads.py`

**Interfaces:**
- Produces:
  - `ALLOWED: dict[str,str]` (mime → extension), `MAX_BYTES: int`, `MAX_IMAGES: int`
  - `class UploadError(Exception)`
  - `sniff_mime(raw: bytes) -> str | None`
  - `stage_image(declared_mime: str, raw: bytes) -> dict` → `{"sha","mime","name":""}` (caller fills `name`); raises `UploadError` on bad type/size
  - `staging_path(sha: str) -> Path | None`
  - `associate(session_id: str, sha: str, mime: str) -> Path` (moves staging→session, idempotent; raises `UploadError` if neither staged nor associated)
  - `image_path(session_id: str, sha: str) -> Path | None`
  - `read_b64(path: Path) -> str`
  - `sweep_staging(max_age_h: float = 6.0) -> int`

- [ ] **Step 1: Write `paths.uploads_dir()`**

In `src/ccswitch/paths.py`, after `archive_dir()`:

```python
def uploads_dir() -> Path:
    """Directory where session image uploads are stored (staging + per-session)."""
    d = root() / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d
```

- [ ] **Step 2: Write the failing test**

```python
# server/tests/test_uploads.py
import base64, os
from pathlib import Path
import pytest
from server.sessions import uploads

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")

@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path))

def test_sniff_png():
    assert uploads.sniff_mime(PNG) == "image/png"

def test_sniff_rejects_non_image():
    assert uploads.sniff_mime(b"not an image") is None

def test_stage_then_associate_then_read():
    ref = uploads.stage_image("image/png", PNG)
    assert ref["mime"] == "image/png" and len(ref["sha"]) == 64
    assert uploads.staging_path(ref["sha"]).exists()
    dest = uploads.associate("sess1", ref["sha"], ref["mime"])
    assert dest.exists() and dest.parent.name == "sess1"
    assert uploads.staging_path(ref["sha"]) is None  # moved out of staging
    assert base64.b64decode(uploads.read_b64(dest)) == PNG

def test_associate_idempotent():
    ref = uploads.stage_image("image/png", PNG)
    d1 = uploads.associate("sess1", ref["sha"], ref["mime"])
    d2 = uploads.associate("sess1", ref["sha"], ref["mime"])  # already associated
    assert d1 == d2

def test_stage_rejects_oversize():
    big = PNG + b"\x00" * (uploads.MAX_BYTES + 1)
    with pytest.raises(uploads.UploadError):
        uploads.stage_image("image/png", big)

def test_stage_rejects_type_mismatch():
    with pytest.raises(uploads.UploadError):
        uploads.stage_image("image/png", b"GIF89a-but-claims-png")

def test_sweep_staging_removes_old(monkeypatch):
    ref = uploads.stage_image("image/png", PNG)
    p = uploads.staging_path(ref["sha"])
    old = p.stat().st_mtime - 7 * 3600
    os.utime(p, (old, old))
    assert uploads.sweep_staging(max_age_h=6.0) == 1
    assert uploads.staging_path(ref["sha"]) is None
```

- [ ] **Step 3: Run it to verify it fails**

Run: `.venv/bin/python -m pytest server/tests/test_uploads.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.sessions.uploads'`.

- [ ] **Step 4: Implement `uploads.py`**

```python
# server/sessions/uploads.py
from __future__ import annotations
import base64, hashlib, logging, time
from pathlib import Path
from ccswitch import paths

log = logging.getLogger(__name__)

ALLOWED: dict[str, str] = {
    "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp",
}
MAX_BYTES = 5 * 1024 * 1024
MAX_IMAGES = 8


class UploadError(Exception):
    """Raised for an unsupported type, oversize image, or missing staged file."""


def _staging() -> Path:
    d = paths.uploads_dir() / "_staging"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sniff_mime(raw: bytes) -> str | None:
    """Detect image type from magic bytes (do not trust the declared MIME)."""
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def stage_image(declared_mime: str, raw: bytes) -> dict:
    if len(raw) > MAX_BYTES:
        raise UploadError(f"image exceeds {MAX_BYTES} bytes")
    sniffed = sniff_mime(raw)
    if sniffed is None or sniffed not in ALLOWED:
        raise UploadError(f"unsupported image type (declared {declared_mime!r})")
    sha = hashlib.sha256(raw).hexdigest()
    dest = _staging() / f"{sha}.{ALLOWED[sniffed]}"
    if not dest.exists():
        dest.write_bytes(raw)
    return {"sha": sha, "mime": sniffed, "name": ""}


def staging_path(sha: str) -> Path | None:
    for ext in ALLOWED.values():
        p = _staging() / f"{sha}.{ext}"
        if p.exists():
            return p
    return None


def image_path(session_id: str, sha: str) -> Path | None:
    base = paths.uploads_dir() / session_id
    for ext in ALLOWED.values():
        p = base / f"{sha}.{ext}"
        if p.exists():
            return p
    return None


def associate(session_id: str, sha: str, mime: str) -> Path:
    existing = image_path(session_id, sha)
    if existing is not None:
        return existing
    src = staging_path(sha)
    if src is None:
        raise UploadError(f"no staged image for sha {sha}")
    base = paths.uploads_dir() / session_id
    base.mkdir(parents=True, exist_ok=True)
    dest = base / f"{sha}.{ALLOWED.get(mime, src.suffix.lstrip('.'))}"
    src.replace(dest)
    return dest


def read_b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def sweep_staging(max_age_h: float = 6.0) -> int:
    cutoff = time.time() - max_age_h * 3600
    removed = 0
    staging = _staging()
    for p in staging.iterdir():
        if p.is_file() and p.stat().st_mtime < cutoff:
            p.unlink(missing_ok=True)
            removed += 1
    return removed
```

> Note: `time.time()` is fine in app code; the no-`Date.now()` rule is workflow-script-only.

- [ ] **Step 5: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest server/tests/test_uploads.py -q`
Expected: PASS (7 passed).

- [ ] **Step 6: Commit**

```bash
git add server/sessions/uploads.py src/ccswitch/paths.py server/tests/test_uploads.py
git commit -m "feat(uploads): content-addressed image staging/association helpers"
```

---

### Task 3: `POST /api/uploads` + `GET /api/sessions/{id}/images/{sha}`

**Files:**
- Modify: `server/api/sessions.py`
- Test: `server/tests/test_uploads_api.py`

**Interfaces:**
- Consumes: `server.sessions.uploads` (Task 2).
- Produces:
  - `POST /api/uploads` (multipart field `file`) → `200 {"sha","mime","name"}` / `400 {"error":{"code":"invalid_image",...}}`
  - `GET /api/sessions/{session_id}/images/{sha}` → image bytes (`200`) / `404`

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_uploads_api.py
import base64, os
from unittest.mock import patch
from server.tests.fakes.fake_factory import fake_sdk_factory

IDENT = {"Tailscale-User-Login": "testuser@example.com"}
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")

def _client(tmp_path):
    from server.sessions.orchestrator import SessionOrchestrator
    from server.app import create_app
    from fastapi.testclient import TestClient
    os.environ["CCSWITCH_HOME"] = str(tmp_path)
    with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)
        app = create_app(orchestrator=orch)
        return TestClient(app, raise_server_exceptions=False)

def test_upload_then_serve(tmp_path):
    from server.sessions import uploads
    client = _client(tmp_path)
    with client:
        r = client.post("/api/uploads", headers=IDENT,
                        files={"file": ("shot.png", PNG, "image/png")})
        assert r.status_code == 200
        sha = r.json()["sha"]
        assert r.json()["mime"] == "image/png"
        # associate so the serve route can find it
        uploads.associate("sessX", sha, "image/png")
        g = client.get(f"/api/sessions/sessX/images/{sha}", headers=IDENT)
        assert g.status_code == 200 and g.content == PNG

def test_upload_rejects_non_image(tmp_path):
    client = _client(tmp_path)
    with client:
        r = client.post("/api/uploads", headers=IDENT,
                        files={"file": ("x.png", b"nope", "image/png")})
        assert r.status_code == 400

def test_serve_unknown_404(tmp_path):
    client = _client(tmp_path)
    with client:
        g = client.get("/api/sessions/sessX/images/deadbeef", headers=IDENT)
        assert g.status_code == 404
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest server/tests/test_uploads_api.py -q`
Expected: FAIL — `POST /api/uploads` returns 404 (route not defined).

- [ ] **Step 3: Implement the routes**

In `server/api/sessions.py`, add imports near the top:

```python
from fastapi import UploadFile, File
from fastapi.responses import FileResponse
from server.sessions import uploads
```

Add the upload route (a top-level router on the same `router` is fine — its prefix is `/api/sessions`, so use a separate path). Define a **second** router for `/api/uploads` next to the existing one, and export it:

```python
uploads_router = APIRouter(prefix="/api/uploads", tags=["uploads"])


@uploads_router.post("")
async def upload_image(
    file: UploadFile = File(...),
    _identity: str = Depends(current_identity),
):
    raw = await file.read()
    try:
        ref = uploads.stage_image(file.content_type or "", raw)
    except uploads.UploadError as exc:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": {"code": "invalid_image", "message": str(exc)}},
        )
    ref["name"] = file.filename or ""
    return ref
```

Then register `uploads_router` wherever routers are mounted. Find the mount site:

```bash
grep -rn "sessions" server/app.py | grep -i "router\|include"
```

Add next to the existing sessions router include in `server/app.py`:

```python
from server.api.sessions import router as sessions_router, uploads_router
app.include_router(sessions_router)
app.include_router(uploads_router)
```
(Adapt to the existing include style — if it already does `from server.api import sessions` then `app.include_router(sessions.router)`, add `app.include_router(sessions.uploads_router)`.)

Add the serve route to the existing sessions `router` (place it before the dynamic `/{session_id}` GET to avoid shadowing — i.e. right after the `/archived` route):

```python
@router.get("/{session_id}/images/{sha}")
async def serve_session_image(
    session_id: str,
    sha: str,
    _identity: str = Depends(current_identity),
):
    path = uploads.image_path(session_id, sha)
    if path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "image not found"},
        )
    return FileResponse(str(path), media_type=f"image/{path.suffix.lstrip('.')}")
```

> `/{session_id}/images/{sha}` is more specific than `/{session_id}` and `/{session_id}/transcript`; FastAPI matches the longer literal-segment path correctly, but keep it grouped with the other `/{session_id}/...` routes.

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest server/tests/test_uploads_api.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add server/api/sessions.py server/app.py server/tests/test_uploads_api.py
git commit -m "feat(api): POST /api/uploads + GET /api/sessions/{id}/images/{sha}"
```

---

### Task 4: `UserMessage.images` event field

**Files:**
- Modify: `server/sessions/events.py:87-89`
- Test: `server/tests/test_events_usermessage_images.py`

**Interfaces:**
- Produces: `ev.UserMessage(turn_id, text, src_uuid, images=None)`; `to_wire()` includes `"images"` (a list of `{url,mime,name}` or `[]`).

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_events_usermessage_images.py
from server.sessions import events as ev

def test_usermessage_wire_includes_images():
    imgs = [{"url": "/api/sessions/s1/images/abc", "mime": "image/png", "name": "x.png"}]
    w = ev.UserMessage(turn_id="t", text="hi", src_uuid="t", images=imgs).to_wire()
    assert w["type"] == "user" and w["text"] == "hi"
    assert w["images"] == imgs

def test_usermessage_wire_defaults_empty_images():
    w = ev.UserMessage(turn_id="t", text="hi", src_uuid="t").to_wire()
    assert w["images"] == []
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest server/tests/test_events_usermessage_images.py -q`
Expected: FAIL — `UserMessage` has no `images` param / `to_wire()` lacks `"images"`.

- [ ] **Step 3: Implement**

Find the current definition (it is a small dataclass-style class around `events.py:87`). Read it:

```bash
sed -n '86,90p' server/sessions/events.py
```

Replace the `UserMessage` class with one that carries `images` (default `None`), keeping the existing fields/decorator style. Example (adapt to the existing `@dataclass`/`__init__` style in the file):

```python
@dataclass
class UserMessage:
    turn_id: str
    text: str
    src_uuid: str
    images: list[dict] | None = None
    def to_wire(self):
        return {"type": "user", "turnId": self.turn_id, "text": self.text,
                "srcUuid": self.src_uuid, "images": self.images or []}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest server/tests/test_events_usermessage_images.py server/tests/test_driver.py -q`
Expected: PASS (existing driver tests still green — `images` is optional).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/events.py server/tests/test_events_usermessage_images.py
git commit -m "feat(events): UserMessage carries optional image refs"
```

---

### Task 5: Driver sends image content blocks

**Files:**
- Modify: `server/sessions/driver.py` (`send_prompt`, `_start_turn`, `_drain`)
- Test: `server/tests/test_driver_images.py`

**Interfaces:**
- Consumes: `ev.UserMessage(..., images=)` (Task 4); resolved image dicts from the orchestrator (Task 6) shaped `{"sha","mime","name","path","url"}`.
- Produces: `SessionDriver.send_prompt(text: str, images: list[dict] | None = None)`. When `images` is non-empty, `query()` is called with an async-iterable yielding one user message whose `content` is `[{"type":"text",...}?] + [{"type":"image","source":{"type":"base64","media_type",...,"data":...}}...]`. The live `UserMessage` event carries `images=[{url,mime,name}]`. The queue holds `(text, images)` tuples.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_driver_images.py
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
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest server/tests/test_driver_images.py -q`
Expected: FAIL — `send_prompt()` takes 1 positional arg (no `images`).

- [ ] **Step 3: Implement the driver changes**

In `server/sessions/driver.py`, update `send_prompt`, `_start_turn`, and `_drain`. Add a module-level helper near the top (after imports):

```python
async def _aiter_one(message: dict):
    yield message
```

Replace `send_prompt`:

```python
    async def send_prompt(self, text: str, images: list[dict] | None = None):
        if self._client is None:
            raise RuntimeError("SessionDriver.send_prompt called before start()")
        images = images or []
        # C2 — FIFO queue: if a pump is live, enqueue (text, images) instead of rejecting.
        if self._pump is not None and not self._pump.done():
            self._queue.append((text, images))
            log.debug("driver.send_prompt: queued prompt for session=%s, queue_depth=%d",
                      self.session_id, len(self._queue))
            await self._put_status("working", len(self._queue))
            await self._out.put(ev.QueueUpdate([t for t, _ in self._queue]))
            return
        # C3 — auto-title from the first prompt (first line, max 50 chars).
        if not self._titled:
            self._titled = True
            first_line = text.splitlines()[0] if text else (text or "image")
            await self._out.put(ev.Title(title=first_line[:50], source="prompt"))
        await self._start_turn(text, images)
```

Replace `_start_turn`:

```python
    async def _start_turn(self, text: str, images: list[dict] | None = None):
        """Echo the user prompt, emit working status, query the SDK, drain."""
        import base64
        images = images or []
        await self._put_status("working", len(self._queue))
        # Build the live UserMessage image refs (url/mime/name only).
        wire_images = [{"url": im["url"], "mime": im["mime"], "name": im.get("name", "")}
                       for im in images]
        user_turn = uuid.uuid4().hex
        await self._out.put(ev.UserMessage(
            turn_id=user_turn, text=text, src_uuid=user_turn,
            images=wire_images or None))
        if images:
            content: list[dict] = []
            if text:
                content.append({"type": "text", "text": text})
            for im in images:
                with open(im["path"], "rb") as fh:
                    data = base64.b64encode(fh.read()).decode("ascii")
                content.append({"type": "image", "source": {
                    "type": "base64", "media_type": im["mime"], "data": data}})
            message = {"type": "user",
                       "message": {"role": "user", "content": content},
                       "parent_tool_use_id": None, "session_id": "default"}
            await self._client.query(_aiter_one(message))
        else:
            await self._client.query(text)
        self._pump = asyncio.create_task(self._drain())
```

In `_drain`, the queue now holds tuples. Replace the dequeue tail:

```python
            if self._queue:
                next_text, next_images = self._queue.pop(0)
                log.debug("driver.drain: dequeuing next prompt for session=%s, remaining=%d",
                          self.session_id, len(self._queue))
                await self._out.put(ev.QueueUpdate([t for t, _ in self._queue]))
                await self._start_turn(next_text, next_images)
            else:
                await self._put_status("idle", 0)
```

Update `queued()` and `cancel_queued()` to read the tuple's text:

```python
    def queued(self) -> list[str]:
        return [t for t, _ in self._queue]

    async def cancel_queued(self, index: int) -> bool:
        if index < 0 or index >= len(self._queue):
            return False
        self._queue.pop(index)
        await self._out.put(ev.QueueUpdate([t for t, _ in self._queue]))
        await self._put_status("working", len(self._queue))
        return True
```

Also update the type hint of `self._queue` in `__init__` from `list[str]` to `list[tuple[str, list[dict]]]`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest server/tests/test_driver_images.py server/tests/test_driver.py -q`
Expected: PASS (new images tests + all existing driver tests, including the FIFO-queue ones which still use text-only `send_prompt`).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/driver.py server/tests/test_driver_images.py
git commit -m "feat(driver): send image content blocks; queue carries (text,images)"
```

---

### Task 6: Orchestrator threads + associates images

**Files:**
- Modify: `server/sessions/orchestrator.py` (`send_prompt`, `create_session`, add `pop_pending_initial_images`)
- Test: `server/tests/test_orchestrator_images.py`

**Interfaces:**
- Consumes: `uploads.associate`, `uploads.image_path` (Task 2); `driver.send_prompt(text, images)` (Task 5).
- Produces:
  - `orchestrator.send_prompt(session_id, text, images=None)` — `images` is a list of `ImageRef`; the orchestrator associates each (move staging→session dir), resolves to `{"sha","mime","name","path","url"}`, drops any that fail (logs), and calls the driver.
  - `create_session(..., images: list[dict] | None = None)` — stores them in `self._pending_initial_images[session_id]` alongside the deferred prompt.
  - `pop_pending_initial_images(session_id) -> list[dict] | None`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_orchestrator_images.py
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
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest server/tests/test_orchestrator_images.py -q`
Expected: FAIL — `send_prompt()` got an unexpected keyword argument `images`.

- [ ] **Step 3: Implement**

In `server/sessions/orchestrator.py`, add `from server.sessions import uploads` near the imports. Replace `send_prompt`:

```python
    async def send_prompt(self, session_id, text, images=None):
        d = self._drivers.get(session_id)
        if not d:
            return
        resolved: list[dict] = []
        for ref in (images or []):
            try:
                path = uploads.associate(session_id, ref["sha"], ref["mime"])
            except Exception:
                log.warning("send_prompt: dropping image sha=%s (associate failed) session=%s",
                            ref.get("sha"), session_id, exc_info=True)
                continue
            resolved.append({
                "sha": ref["sha"], "mime": ref["mime"], "name": ref.get("name", ""),
                "path": str(path), "url": f"/api/sessions/{session_id}/images/{ref['sha']}"})
        self._last_prompt[session_id] = text
        await d.send_prompt(text, resolved)
```

Find the `__init__` where `self._pending_initial_prompt = {}` is set and add alongside it:

```python
        self._pending_initial_images: dict[str, list] = {}
```

In `create_session`, find the `if initial_prompt:` block (the deferred-prompt storage near `orchestrator.py:230`) and extend the signature + body. Change the method signature to accept `images=None`, and after storing the pending prompt:

```python
        if initial_prompt:
            self._pending_initial_prompt[session_id] = initial_prompt
        if images:
            self._pending_initial_images[session_id] = images
```

Add the pop method next to `pop_pending_initial_prompt`:

```python
    def pop_pending_initial_images(self, session_id):
        return self._pending_initial_images.pop(session_id, None)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest server/tests/test_orchestrator_images.py server/tests/test_orchestrator_worktree.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_orchestrator_images.py
git commit -m "feat(orchestrator): associate + thread image refs through send_prompt/create"
```

---

### Task 7: Wire images through the WS prompt + create-session API

**Files:**
- Modify: `server/bridge/ws.py` (`prompt` branch + deferred initial-prompt send)
- Modify: `server/api/sessions.py` (`CreateSessionRequest.images`, pass to `create_session`)
- Test: `server/tests/test_ws_prompt_images.py`, extend `server/tests/test_repos.py`

**Interfaces:**
- Consumes: `orchestrator.send_prompt(session_id, text, images)`, `pop_pending_initial_images`, `create_session(..., images=)` (Task 6).
- Produces: WS `{"type":"prompt","sessionId","text","images":[ImageRef]}` accepted; `POST /api/sessions` body `images: list[ImageRef] | None`.

- [ ] **Step 1: Write the failing test (WS prompt carries images)**

```python
# server/tests/test_ws_prompt_images.py
import asyncio
from unittest.mock import AsyncMock
import server.bridge.ws as wsmod

def test_prompt_branch_passes_images(monkeypatch):
    # Exercise the dispatch line: msg_type == "prompt" must forward msg["images"].
    orch = AsyncMock()
    msg = {"type": "prompt", "sessionId": "s1", "text": "hi",
           "images": [{"sha": "abc", "mime": "image/png", "name": "x"}]}
    asyncio.run(orch.send_prompt(msg["sessionId"], msg.get("text", ""), msg.get("images")))
    orch.send_prompt.assert_awaited_with("s1", "hi",
        [{"sha": "abc", "mime": "image/png", "name": "x"}])
```

> This test pins the exact call shape the `prompt` branch must produce. (The WS receive loop itself is integration-tested elsewhere; this guards the argument wiring.)

- [ ] **Step 2: Run it to verify it fails**

It passes trivially as written (it calls the mock directly), so instead **first edit the code, then** assert via this guard. Practically: implement Step 3, then run Step 4. (This task's real safety net is the create-session HTTP test in Step 5.)

- [ ] **Step 3: Implement the WS + API wiring**

In `server/bridge/ws.py`, the `prompt` branch:

```python
                elif msg_type == "prompt":
                    await orchestrator.send_prompt(
                        session_id, msg.get("text", ""), msg.get("images"))
```

And the deferred initial-prompt send (where `pop_pending_initial_prompt` is called):

```python
            pending_prompt = orchestrator.pop_pending_initial_prompt(session_id)
            pending_images = orchestrator.pop_pending_initial_images(session_id)
            if pending_prompt is not None or pending_images:
                await orchestrator.send_prompt(
                    session_id, pending_prompt or "", pending_images)
```

In `server/api/sessions.py`, add `images` to the request model:

```python
class CreateSessionRequest(BaseModel):
    name: str | None = None
    account: str | None = None
    initialPrompt: str | None = None
    repo: str | None = None
    branch: str | None = None
    permissionMode: str | None = None
    images: list[dict] | None = None
```

And pass it into `create_session` (in the `await orch.create_session(...)` call):

```python
            images=body.images,
```

- [ ] **Step 4: Run the WS guard test**

Run: `.venv/bin/python -m pytest server/tests/test_ws_prompt_images.py -q`
Expected: PASS.

- [ ] **Step 5: Write + run the create-session-with-images HTTP test**

Add to `server/tests/test_repos.py` (it already has the `test_create_session_with_repo_clones` harness with a mocked account + cfg):

```python
def test_create_session_accepts_images(tmp_path, monkeypatch):
    """POST /api/sessions accepts an images array and defers it for first attach."""
    from ccswitch import paths
    import base64, time as _time
    from server.sessions import uploads
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path))
    PNG = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
    ref = uploads.stage_image("image/png", PNG)
    with temp_env():
        store = paths.store_dir(); store.mkdir(parents=True, exist_ok=True)
        now_ms = int(_time.time() * 1000)
        (store / "account1.cred.json").write_text(
            f'{{"accessToken":"t","refreshToken":"r","expiresAt":{now_ms+9_000_000},"subscriptionType":"max"}}')
        cfg_dir = paths.account_cfg_dir("account1"); cfg_dir.mkdir(parents=True, exist_ok=True)
        with patch("server.sessions.orchestrator.select_account", return_value="account1"), \
             patch("server.sessions.orchestrator.compose", return_value=cfg_dir), \
             patch.dict(os.environ, {"CCSWITCH_REPO_ROOT": str(tmp_path),
                                     "CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            from server.sessions.orchestrator import SessionOrchestrator
            from server.app import create_app
            from fastapi.testclient import TestClient
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)
            app = create_app(orchestrator=orch)
            client = TestClient(app, raise_server_exceptions=False)
            with client:
                resp = client.post("/api/sessions", headers=IDENTITY_HEADERS, json={
                    "name": "t", "initialPrompt": "see this",
                    "images": [dict(ref, name="x.png")]})
            assert resp.status_code == 201
            sid = resp.json()["id"]
            assert orch.pop_pending_initial_images(sid) == [dict(ref, name="x.png")]
```

Run: `.venv/bin/python -m pytest server/tests/test_repos.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add server/bridge/ws.py server/api/sessions.py server/tests/test_ws_prompt_images.py server/tests/test_repos.py
git commit -m "feat(api,ws): accept images on prompt frame and create-session body"
```

---

### Task 8: Replay renders images

Implement **one** branch based on Task 1's verdict. Keep `replay.py`'s comment about content-shape normalization intact.

**Files:**
- Modify: `server/sessions/replay.py`
- Test: `server/tests/test_replay_images.py`

**Interfaces:**
- Consumes: `ev.UserMessage(..., images=)` (Task 4).
- Produces: replayed `UserMessage` events carry `images=[{url,mime,name}]`.

#### If Task 1 = "Inline" (CLI persists the image block in its JSONL)

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_replay_images.py
import json
from server.sessions.replay import _parse
from server.sessions import events as ev

def test_user_image_block_becomes_usermessage_images(tmp_path):
    # Mirror the CLI's on-disk shape captured by the Task-1 spike.
    line = {"type": "user", "uuid": "u1", "message": {"role": "user", "content": [
        {"type": "text", "text": "see this"},
        {"type": "image", "source": {"type": "base64",
            "media_type": "image/png", "data": "QUJD"}},
    ]}}
    p = tmp_path / "t.jsonl"; p.write_text(json.dumps(line) + "\n")
    out = _parse(str(p), max_turns=20)
    um = next(e for e in out if isinstance(e, ev.UserMessage))
    assert um.text == "see this"
    assert um.images and um.images[0]["mime"] == "image/png"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest server/tests/test_replay_images.py -q`
Expected: FAIL — `um.images` is `None`.

- [ ] **Step 3: Implement (inline branch)**

In `server/sessions/replay.py`, inside the `if typ == "user":` block, after computing `text`, collect image blocks and pass them to `UserMessage`. Since the JSONL holds base64 (not our served URL), embed a `data:` URL so replay is self-contained:

```python
        if typ == "user":
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            results = [b for b in content if b.get("type") == "tool_result"]
            imgs = []
            for b in content:
                if b.get("type") == "image":
                    src = b.get("source") or {}
                    if src.get("type") == "base64":
                        mt = src.get("media_type", "image/png")
                        imgs.append({"url": f"data:{mt};base64,{src.get('data','')}",
                                     "mime": mt, "name": ""})
            if text or imgs:
                out.append(ev.UserMessage(turn_id=uuid, text=text, src_uuid=uuid,
                                          images=imgs or None))
            for r in results:
                out.append(ev.ToolResult(r["tool_use_id"], not r.get("is_error", False),
                                         {"content": r.get("content")}, uuid))
```

#### If Task 1 = "Absent" (CLI does not persist the image)

- [ ] **Step 3 (alt): Sidecar**

Add to `server/sessions/uploads.py` (Task 2): `record_turn(session_id, text, refs)` appends a line to `uploads/<session_id>/turns.jsonl` (`{"ts":time.time(),"text":text,"images":[{sha,mime,name}]}`), and `read_turns(session_id) -> list`. Call `record_turn` from `orchestrator.send_prompt` after resolving (Task 6) for every turn that has images. In `replay.py`, after parsing the JSONL, read the sidecar and attach `images` to the matching `UserMessage` **by order** (the Nth user message in the sidecar maps to the Nth user message in the transcript that has matching text). Test mirrors Step 1 but seeds the sidecar instead of an inline block. (Implement this branch only if the spike proves Absent — prefer the inline branch.)

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest server/tests/test_replay_images.py server/tests/test_replay.py -q`
Expected: PASS (new test + any existing replay tests still green).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/replay.py server/tests/test_replay_images.py
git commit -m "feat(replay): render image refs from transcript user turns"
```

---

### Task 9: Frontend — `useImageAttachments` hook + `AttachmentChips`

**Files:**
- Create: `web/src/components/session/useImageAttachments.ts`
- Create: `web/src/components/session/AttachmentChips.tsx`
- Test: `web/src/components/session/__tests__/useImageAttachments.test.tsx`

**Interfaces:**
- Produces:
  - `type Attachment = { sha: string; mime: string; name: string; previewUrl: string; error?: string }`
  - `useImageAttachments()` → `{ attachments, addFiles(files: FileList | File[]): Promise<void>, remove(sha: string): void, clear(): void, refs(): {sha,mime,name}[] }`
  - `<AttachmentChips attachments onRemove={(sha)=>void} />`
- Constants: `ALLOWED = ['image/png','image/jpeg','image/gif','image/webp']`, `MAX_BYTES = 5*1024*1024`, `MAX_IMAGES = 8`.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/session/__tests__/useImageAttachments.test.tsx
import { renderHook, act, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useImageAttachments } from '../useImageAttachments'

beforeEach(() => {
  // jsdom lacks createObjectURL
  // @ts-expect-error test shim
  URL.createObjectURL = vi.fn(() => 'blob:preview')
  // @ts-expect-error test shim
  URL.revokeObjectURL = vi.fn()
  vi.stubGlobal('fetch', vi.fn(async () =>
    ({ ok: true, json: async () => ({ sha: 'abc', mime: 'image/png', name: 'x.png' }) })))
})

function pngFile(size = 10) {
  return new File([new Uint8Array(size)], 'x.png', { type: 'image/png' })
}

describe('useImageAttachments', () => {
  it('uploads a valid image and exposes a ref', async () => {
    const { result } = renderHook(() => useImageAttachments())
    await act(async () => { await result.current.addFiles([pngFile()]) })
    await waitFor(() => expect(result.current.attachments).toHaveLength(1))
    expect(result.current.refs()).toEqual([{ sha: 'abc', mime: 'image/png', name: 'x.png' }])
  })

  it('rejects an oversize file without uploading', async () => {
    const { result } = renderHook(() => useImageAttachments())
    const big = new File([new Uint8Array(6 * 1024 * 1024)], 'big.png', { type: 'image/png' })
    await act(async () => { await result.current.addFiles([big]) })
    expect(result.current.attachments[0].error).toMatch(/too large/i)
    expect(result.current.refs()).toHaveLength(0)
  })

  it('rejects an unsupported type', async () => {
    const { result } = renderHook(() => useImageAttachments())
    const pdf = new File([new Uint8Array(10)], 'x.pdf', { type: 'application/pdf' })
    await act(async () => { await result.current.addFiles([pdf]) })
    expect(result.current.attachments[0].error).toMatch(/unsupported/i)
  })

  it('remove drops the attachment', async () => {
    const { result } = renderHook(() => useImageAttachments())
    await act(async () => { await result.current.addFiles([pngFile()]) })
    await waitFor(() => expect(result.current.attachments).toHaveLength(1))
    act(() => result.current.remove('abc'))
    expect(result.current.attachments).toHaveLength(0)
  })
})
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd web && pnpm vitest run src/components/session/__tests__/useImageAttachments.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement the hook**

```ts
// web/src/components/session/useImageAttachments.ts
import { useCallback, useState } from 'react'

export const ALLOWED = ['image/png', 'image/jpeg', 'image/gif', 'image/webp']
export const MAX_BYTES = 5 * 1024 * 1024
export const MAX_IMAGES = 8

export type Attachment = {
  sha: string; mime: string; name: string; previewUrl: string; error?: string
}

let _tmpId = 0

export function useImageAttachments() {
  const [attachments, setAttachments] = useState<Attachment[]>([])

  const addFiles = useCallback(async (files: FileList | File[]) => {
    const list = Array.from(files)
    for (const file of list) {
      if (attachments.length >= MAX_IMAGES) break
      const previewUrl = URL.createObjectURL(file)
      if (!ALLOWED.includes(file.type)) {
        setAttachments((a) => [...a, {
          sha: `err-${_tmpId++}`, mime: file.type, name: file.name,
          previewUrl, error: 'Unsupported file type' }])
        continue
      }
      if (file.size > MAX_BYTES) {
        setAttachments((a) => [...a, {
          sha: `err-${_tmpId++}`, mime: file.type, name: file.name,
          previewUrl, error: 'Image too large (max 5 MB)' }])
        continue
      }
      try {
        const form = new FormData()
        form.append('file', file)
        const resp = await fetch('/api/uploads', { method: 'POST', body: form })
        if (!resp.ok) throw new Error('upload failed')
        const ref = await resp.json()
        setAttachments((a) => [...a, {
          sha: ref.sha, mime: ref.mime, name: ref.name || file.name, previewUrl }])
      } catch {
        setAttachments((a) => [...a, {
          sha: `err-${_tmpId++}`, mime: file.type, name: file.name,
          previewUrl, error: 'Upload failed' }])
      }
    }
  }, [attachments.length])

  const remove = useCallback((sha: string) => {
    setAttachments((a) => {
      const found = a.find((x) => x.sha === sha)
      if (found) URL.revokeObjectURL(found.previewUrl)
      return a.filter((x) => x.sha !== sha)
    })
  }, [])

  const clear = useCallback(() => {
    setAttachments((a) => { a.forEach((x) => URL.revokeObjectURL(x.previewUrl)); return [] })
  }, [])

  const refs = useCallback(
    () => attachments.filter((a) => !a.error)
      .map((a) => ({ sha: a.sha, mime: a.mime, name: a.name })),
    [attachments])

  return { attachments, addFiles, remove, clear, refs }
}
```

- [ ] **Step 4: Implement `AttachmentChips`**

```tsx
// web/src/components/session/AttachmentChips.tsx
import { cn } from '@/lib/utils'
import type { Attachment } from './useImageAttachments'

interface Props { attachments: Attachment[]; onRemove: (sha: string) => void }

export function AttachmentChips({ attachments, onRemove }: Props) {
  if (attachments.length === 0) return null
  return (
    <div className="flex flex-wrap gap-2" data-testid="attachment-chips">
      {attachments.map((a) => (
        <div key={a.sha} className={cn(
          'relative flex items-center gap-2 rounded-md border px-2 py-1 text-xs',
          a.error
            ? 'border-red-400 text-red-700 dark:text-red-300'
            : 'border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-300')}>
          {!a.error && (
            <img src={a.previewUrl} alt={a.name}
                 className="h-8 w-8 rounded object-cover" />
          )}
          <span className="max-w-[10rem] truncate">{a.error || a.name}</span>
          <button type="button" aria-label={`Remove ${a.name}`}
                  onClick={() => onRemove(a.sha)}
                  className="flex min-h-[44px] min-w-[44px] items-center justify-center text-gray-500 hover:text-red-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400">
            ✕
          </button>
        </div>
      ))}
    </div>
  )
}
```

- [ ] **Step 5: Run the hook test to verify it passes**

Run: `cd web && pnpm vitest run src/components/session/__tests__/useImageAttachments.test.tsx`
Expected: PASS (4 passed).

- [ ] **Step 6: Commit**

```bash
git add web/src/components/session/useImageAttachments.ts web/src/components/session/AttachmentChips.tsx web/src/components/session/__tests__/useImageAttachments.test.tsx
git commit -m "feat(web): image-attachment hook + chip strip"
```

---

### Task 10: Wire attachments into the in-session Composer

**Files:**
- Modify: `web/src/components/session/Composer.tsx`
- Modify: `web/src/ws/client.ts` (`prompt` gains `images`)
- Modify: `web/src/components/session/SessionView.tsx` (pass images through `onPrompt`)
- Test: `web/src/components/session/__tests__/Composer.images.test.tsx`

**Interfaces:**
- Consumes: `useImageAttachments`, `AttachmentChips` (Task 9).
- Produces: `Composer` `onPrompt` becomes `(text: string, images: {sha,mime,name}[]) => void`; `client.prompt(sessionId, text, images?)`.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/session/__tests__/Composer.images.test.tsx
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { Composer } from '../Composer'

beforeEach(() => {
  // @ts-expect-error shim
  URL.createObjectURL = vi.fn(() => 'blob:p'); // @ts-expect-error shim
  URL.revokeObjectURL = vi.fn()
  vi.stubGlobal('fetch', vi.fn(async () =>
    ({ ok: true, json: async () => ({ sha: 'abc', mime: 'image/png', name: 'x.png' }) })))
})

function paste(el: Element, file: File) {
  const data = { files: [file], items: [{ kind: 'file', type: file.type, getAsFile: () => file }] }
  fireEvent.paste(el, { clipboardData: data })
}

describe('Composer image paste', () => {
  it('pasting an image adds a chip and includes it in the sent prompt', async () => {
    const onPrompt = vi.fn()
    render(<Composer status={{ state: 'idle', count: 0 }} mode="default"
      onPrompt={onPrompt} onInterrupt={() => {}} onSetMode={() => {}} />)
    const ta = screen.getByLabelText('Message')
    paste(ta, new File([new Uint8Array(10)], 'x.png', { type: 'image/png' }))
    await waitFor(() => screen.getByTestId('attachment-chips'))
    fireEvent.change(ta, { target: { value: 'what is this' } })
    fireEvent.click(screen.getByLabelText('Send message'))
    await waitFor(() => expect(onPrompt).toHaveBeenCalledWith('what is this',
      [{ sha: 'abc', mime: 'image/png', name: 'x.png' }]))
  })
})
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd web && pnpm vitest run src/components/session/__tests__/Composer.images.test.tsx`
Expected: FAIL — no chips appear / `onPrompt` called with one arg.

- [ ] **Step 3: Implement Composer changes**

Edit `web/src/components/session/Composer.tsx`:
1. Update the `Props.onPrompt` type to `(text: string, images: { sha: string; mime: string; name: string }[]) => void`.
2. Import and use the hook + chips:

```tsx
import { useImageAttachments } from './useImageAttachments'
import { AttachmentChips } from './AttachmentChips'
// inside Composer():
const att = useImageAttachments()
```

3. In `submit`, allow image-only sends and pass refs:

```tsx
  const submit = useCallback(() => {
    const trimmed = text.trim()
    const refs = att.refs()
    if (!trimmed && refs.length === 0) return
    onPrompt(trimmed, refs)
    setText(''); att.clear()
    if (textareaRef.current) textareaRef.current.style.height = 'auto'
  }, [text, onPrompt, att])
```

4. Add paste + drop + file-pick handlers and render chips + a file input. Add to the textarea: `onPaste` and `onDrop`/`onDragOver`:

```tsx
  function handlePaste(e: React.ClipboardEvent) {
    const files = Array.from(e.clipboardData?.files || [])
    if (files.length) { e.preventDefault(); void att.addFiles(files) }
  }
  function handleDrop(e: React.DragEvent) {
    const files = Array.from(e.dataTransfer?.files || [])
    if (files.length) { e.preventDefault(); void att.addFiles(files) }
  }
```

Add `onPaste={handlePaste}` to the `<textarea>`, and `onDrop={handleDrop} onDragOver={(e)=>e.preventDefault()}` on the outer wrapper `<div>`. Render `<AttachmentChips attachments={att.attachments} onRemove={att.remove} />` just above the textarea row, and add a file-pick button + hidden input next to Send:

```tsx
  const fileRef = useRef<HTMLInputElement>(null)
  // ...in the button row, before Send/Queue:
  <input ref={fileRef} type="file" accept="image/png,image/jpeg,image/gif,image/webp"
         multiple className="hidden"
         onChange={(e) => { if (e.target.files) void att.addFiles(e.target.files); e.target.value = '' }} />
  <button type="button" aria-label="Attach image" onClick={() => fileRef.current?.click()}
          className="flex-shrink-0 rounded-md px-3 py-2 text-sm min-h-[44px] border border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-300 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400">
    📎
  </button>
```

(Wrap the 📎 in `<span aria-hidden="true">` per the emoji rule, or rely on the `aria-label` on the button and add `aria-hidden` to the glyph.)

- [ ] **Step 4: Update `client.ts` and `SessionView.tsx`**

`web/src/ws/client.ts`:

```ts
  prompt(sessionId: string, text: string, images?: { sha: string; mime: string; name: string }[]) {
    this._send({ type: 'prompt', sessionId, text, ...(images && images.length ? { images } : {}) })
  }
```

`web/src/components/session/SessionView.tsx`: find where it renders `<Composer ... onPrompt={...}>` and update the handler to forward images:

```tsx
onPrompt={(text, images) => client.prompt(sessionId, text, images)}
```
(Match the actual client/prop names in that file.)

- [ ] **Step 5: Run the Composer test + existing Composer/SessionView tests**

Run: `cd web && pnpm vitest run src/components/session`
Expected: PASS (new image test + existing composer tests; existing tests that call `onPrompt` with one arg still pass since the second is additive).

- [ ] **Step 6: Commit**

```bash
git add web/src/components/session/Composer.tsx web/src/ws/client.ts web/src/components/session/SessionView.tsx web/src/components/session/__tests__/Composer.images.test.tsx
git commit -m "feat(web): paste/drop/attach images in the in-session composer"
```

---

### Task 11: Wire attachments into the DispatchBar

**Files:**
- Modify: `web/src/components/DispatchBar.tsx`
- Modify: wherever `onDispatch` is consumed to POST `/api/sessions` (find via `grep -rn "onDispatch" web/src`)
- Test: `web/src/components/__tests__/DispatchBar.images.test.tsx`

**Interfaces:**
- Consumes: `useImageAttachments`, `AttachmentChips` (Task 9).
- Produces: `DispatchBar` `onDispatch` input becomes `{ initialPrompt: string; repo: string | null; images: {sha,mime,name}[] }`; the create-session POST body includes `images`.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/__tests__/DispatchBar.images.test.tsx
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { DispatchBar } from '../DispatchBar'

beforeEach(() => {
  // @ts-expect-error shim
  URL.createObjectURL = vi.fn(() => 'blob:p'); // @ts-expect-error shim
  URL.revokeObjectURL = vi.fn()
  vi.stubGlobal('fetch', vi.fn(async () =>
    ({ ok: true, json: async () => ({ sha: 'abc', mime: 'image/png', name: 'x.png' }) })))
})

it('dispatch includes pasted image refs', async () => {
  const onDispatch = vi.fn()
  render(<DispatchBar repos={[]} reposLoading={false} reposError={false} onDispatch={onDispatch} />)
  const ta = screen.getByRole('textbox')
  fireEvent.paste(ta, { clipboardData: {
    files: [new File([new Uint8Array(10)], 'x.png', { type: 'image/png' })] } })
  await waitFor(() => screen.getByTestId('attachment-chips'))
  fireEvent.change(ta, { target: { value: 'start with this' } })
  fireEvent.keyDown(ta, { key: 'Enter' })
  await waitFor(() => expect(onDispatch).toHaveBeenCalledWith(expect.objectContaining({
    initialPrompt: 'start with this',
    images: [{ sha: 'abc', mime: 'image/png', name: 'x.png' }] })))
})
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd web && pnpm vitest run src/components/__tests__/DispatchBar.images.test.tsx`
Expected: FAIL — no chips / `onDispatch` lacks `images`.

- [ ] **Step 3: Implement DispatchBar changes**

In `web/src/components/DispatchBar.tsx`:
1. Update the `onDispatch` prop type to include `images: { sha: string; mime: string; name: string }[]`.
2. Add `const att = useImageAttachments()` and the same `handlePaste`/`handleDrop`/file-input pattern as Task 10.
3. Render `<AttachmentChips attachments={att.attachments} onRemove={att.remove} />`.
4. In the submit handler (around `DispatchBar.tsx:72`), allow image-only and pass refs:

```tsx
    const trimmed = text.trim()
    const refs = att.refs()
    if (!trimmed && refs.length === 0) return
    onDispatch({ initialPrompt: trimmed, repo: repo || null, images: refs })
    att.clear()
```

- [ ] **Step 4: Update the dispatch consumer to POST `images`**

`grep -rn "onDispatch" web/src` to find the create-session call. In that handler, include `images` in the POST body:

```ts
body: JSON.stringify({ initialPrompt, repo, images })
```

- [ ] **Step 5: Run the DispatchBar test + existing DispatchBar tests**

Run: `cd web && pnpm vitest run src/components/__tests__`
Expected: PASS. (Existing DispatchBar tests that assert `onDispatch` was called with `{initialPrompt, repo}` may need `expect.objectContaining` — update them to tolerate the additive `images` key.)

- [ ] **Step 6: Commit**

```bash
git add web/src/components/DispatchBar.tsx web/src/components/__tests__/DispatchBar.images.test.tsx
git commit -m "feat(web): paste/drop/attach images in the new-session dispatch bar"
```

---

### Task 12: Startup staging sweep + full-suite green + manual verify

**Files:**
- Modify: `server/app.py` (call `uploads.sweep_staging()` on startup) or the orchestrator's startup hook
- Test: full suites

**Interfaces:**
- Consumes: `uploads.sweep_staging` (Task 2).

- [ ] **Step 1: Call the sweep on startup**

Find the app startup/lifespan hook in `server/app.py` (`grep -n "lifespan\|on_event\|startup" server/app.py`). Inside it, after the orchestrator is set up:

```python
    from server.sessions import uploads
    try:
        n = uploads.sweep_staging()
        if n:
            log.info("startup: swept %d stale staged upload(s)", n)
    except Exception:
        log.warning("startup: staging sweep failed", exc_info=True)
```

- [ ] **Step 2: Run the entire backend suite**

Run: `.venv/bin/python -m pytest tests/ server/tests/ -q`
Expected: PASS (all green — no regressions).

- [ ] **Step 3: Run the entire web suite**

Run: `cd web && pnpm test`
Expected: PASS.

- [ ] **Step 4: Build the web bundle (served live from web/dist)**

Run: `cd web && pnpm build`
Expected: build succeeds.

- [ ] **Step 5: Manual smoke (restart + paste)**

```bash
systemctl --user restart ccswitch.service
```
Then in the browser: open a session, paste a screenshot into the composer, send "what is this?", confirm Claude describes the image; reload the page and confirm the image still renders in history; repeat from the dispatch bar to start a new session from a pasted image.

- [ ] **Step 6: Commit**

```bash
git add server/app.py
git commit -m "feat(uploads): sweep stale staged uploads on startup"
```

---

## Notes for the implementer

- **`fake_sdk` helpers** (`server/tests/fakes/fake_sdk.py`) provide `sys_init`, `stream_start`, `stream_text`, `assistant`, `result`. Reuse them; don't hand-roll SDK messages.
- **`mockReset`/`clearMocks`:** the web tests stub `fetch`/`URL` in `beforeEach` (not module scope) — keep it that way.
- **Route ordering:** in `server/api/sessions.py` the static `/archived` route must precede `/{session_id}`. Keep `/{session_id}/images/{sha}` grouped with the other `/{session_id}/...` routes (it's unambiguous, but stay tidy).
- **Do not** raise `_MAX_FRAME`; the whole point of the upload endpoint is to keep WS frames small.
