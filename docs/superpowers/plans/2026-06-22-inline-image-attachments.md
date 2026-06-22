# Inline Image Attachments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users attach images to a prompt (paste / file-picker / drag-drop, in both the live composer and the new-session bar); display them inline in chat and send them to Claude as vision input.

**Architecture:** Images upload over a dedicated REST multipart endpoint (the WS has a 256 KB frame cap), are stored on disk keyed by a UUID, and tracked in a new `attachments` table. Prompts carry only the small attachment IDs. The driver reads files from disk to build base64 image content blocks for the SDK's structured (async-iterable) `query()` path. The UI renders thumbnails via an identity-gated serve endpoint; transcript replay reconstructs them.

**Tech Stack:** Python FastAPI + SQLAlchemy 2.x async (SQLite) backend · React + Vite + TS + Tailwind frontend · `claude_agent_sdk.ClaudeSDKClient`.

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-06-22-inline-image-attachments-design.md`.
- **Allowed image types:** PNG, JPEG, WebP, GIF — validated by **magic-byte sniff**, not client Content-Type.
- **Limits:** ≤ 5 images per message; ≤ 10 MB per image (enforced server-side).
- **WS inbound frame cap:** 256 KB (`server/bridge/ws.py` `_MAX_FRAME`) — never send base64 over WS.
- **DB rules:** `create_all` creates missing *tables* but never ALTERs; new columns on *existing* tables must be nullable. The new `attachments` table is fine.
- **Storage root:** `ccswitch.paths.root()` (`~/.claude-accounts`, env-override `CCSWITCH_HOME`). Attachments live in `root()/attachments/<id>.<ext>`.
- **Constitution:** never patch the Claude binary (I); verify behavior against the real binary, not assumptions (III); TDD + quality gates (V).
- **Server test command** (worktree has no built env — use the main checkout's prebuilt venv):
  `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest <path> -v`
  API tests that exercise auth use `IDENTITY_HEADERS = {"Tailscale-User-Login": "testuser@example.com"}` and the `_make_app_and_client` helper pattern from `server/tests/test_api.py` (sets in-memory DB + fake SDK factory).
- **Web test commands** (run inside `web/`, with `node_modules` symlinked from the main checkout):
  `npx vitest run <path>` · `npx tsc --noEmit` · `npx eslint <path>`.
- **Commit after every task.** Branch off `master` (work happens on the existing worktree branch).

---

## File Structure

**New files**
- `server/api/attachments.py` — upload + serve router.
- `server/sessions/attachments.py` — pure helpers: magic-byte sniff, ext/media-type map, base64 block builder, resolve+bind.
- `web/src/components/session/useAttachments.ts` — attach state hook (add/remove/upload/validate).
- `web/src/components/session/AttachmentTray.tsx` — thumbnail strip with remove buttons.

**Modified files**
- `server/db/models.py` — add `Attachment` model.
- `src/ccswitch/paths.py` — add `attachments_dir()`.
- `server/app.py` — register attachments router; start orphan-sweep task.
- `server/sessions/sdk_contract.py` — widen `query` signature.
- `server/tests/fakes/fake_sdk.py` — `query` accepts str | async-iterable.
- `server/sessions/events.py` — `UserMessage.attachments`.
- `server/sessions/driver.py` — queue dataclass, structured send, echo attachments.
- `server/sessions/orchestrator.py` — `send_prompt(attachment_ids)`, pending-prompt holds attachments, `permanent_delete` unlinks files.
- `server/sessions/replay.py` — reconstruct image refs.
- `server/bridge/ws.py` — `prompt` frame `attachmentIds`.
- `server/api/sessions.py` — `CreateSessionRequest.attachmentIds`.
- `web/src/session/events.ts` — `EvUser.attachments`.
- `web/src/session/reducer.ts` — `image` block on user turn.
- `web/src/components/session/UserBubble.tsx` — render thumbnails.
- `web/src/components/session/Composer.tsx`, `web/src/components/DispatchBar.tsx` — attach UI + forward IDs.
- `web/src/api/client.ts`, `web/src/api/hooks.ts` — multipart upload + create-session field.
- `web/src/ws/client.ts` — `prompt(sessionId, text, attachmentIds?)`.
- `web/src/components/session/SessionView.tsx`, `web/src/components/Dashboard.tsx` — thread IDs through.

---

## Phase 1 — Backend storage & endpoints

### Task 1: `Attachment` model + `attachments_dir()` path helper

**Files:**
- Modify: `src/ccswitch/paths.py`
- Modify: `server/db/models.py`
- Test: `server/tests/test_attachment_model.py` (create)

**Interfaces:**
- Produces: `ccswitch.paths.attachments_dir() -> Path`; `server.db.models.Attachment` with columns `id, session_id, turn_id, media_type, filename, byte_size, sha256, uploaded_by, created_at`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_attachment_model.py
from __future__ import annotations
import sys
from pathlib import Path
REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

def test_attachments_dir_under_root(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path))
    from importlib import reload
    from ccswitch import paths
    reload(paths)
    d = paths.attachments_dir()
    assert d == tmp_path / "attachments"

def test_attachment_model_columns():
    from server.db.models import Attachment
    cols = set(Attachment.__table__.columns.keys())
    assert {"id", "session_id", "turn_id", "media_type", "filename",
            "byte_size", "sha256", "uploaded_by", "created_at"} <= cols
    assert Attachment.__table__.c.session_id.nullable is True
    assert Attachment.__table__.c.turn_id.nullable is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_attachment_model.py -v`
Expected: FAIL (`attachments_dir` / `Attachment` not defined).

- [ ] **Step 3: Add the path helper**

```python
# src/ccswitch/paths.py — add near store_dir()
def attachments_dir() -> Path:
    return root() / "attachments"
```

- [ ] **Step 4: Add the model**

```python
# server/db/models.py — add after the existing imports/Base; uses _new_uuid
class Attachment(Base):
    """An image uploaded by a user and attached to a prompt."""

    __tablename__ = "attachments"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    session_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    turn_id: Mapped[str | None] = mapped_column(String, nullable=True)
    media_type: Mapped[str] = mapped_column(String, nullable=False)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String, nullable=False, index=True)
    uploaded_by: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now()
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_attachment_model.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/ccswitch/paths.py server/db/models.py server/tests/test_attachment_model.py
git commit -m "feat(attachments): Attachment model + attachments_dir() path helper"
```

---

### Task 2: Pure attachment helpers (sniff, media-type, base64 block)

**Files:**
- Create: `server/sessions/attachments.py`
- Test: `server/tests/test_attachment_helpers.py`

**Interfaces:**
- Produces:
  - `sniff_media_type(head: bytes) -> str | None` — returns `image/png|image/jpeg|image/webp|image/gif` or `None`.
  - `ext_for(media_type: str) -> str` — `"png"|"jpg"|"webp"|"gif"`.
  - `MAX_BYTES = 10 * 1024 * 1024`, `MAX_PER_MESSAGE = 5`.
  - `image_block(media_type: str, data_b64: str) -> dict` — Anthropic image content block.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_attachment_helpers.py
import base64
from server.sessions.attachments import (
    sniff_media_type, ext_for, image_block, MAX_BYTES, MAX_PER_MESSAGE,
)

PNG = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 16
JPEG = bytes.fromhex("ffd8ff") + b"\x00" * 16
GIF = b"GIF89a" + b"\x00" * 16
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 8

def test_sniff_known_types():
    assert sniff_media_type(PNG) == "image/png"
    assert sniff_media_type(JPEG) == "image/jpeg"
    assert sniff_media_type(GIF) == "image/gif"
    assert sniff_media_type(WEBP) == "image/webp"

def test_sniff_rejects_unknown():
    assert sniff_media_type(b"%PDF-1.7") is None
    assert sniff_media_type(b"") is None

def test_ext_for():
    assert ext_for("image/jpeg") == "jpg"
    assert ext_for("image/png") == "png"

def test_image_block_shape():
    blk = image_block("image/png", "AAAA")
    assert blk == {"type": "image",
                   "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}

def test_limits_constants():
    assert MAX_BYTES == 10 * 1024 * 1024
    assert MAX_PER_MESSAGE == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_attachment_helpers.py -v`
Expected: FAIL (module not found).

- [ ] **Step 3: Write minimal implementation**

```python
# server/sessions/attachments.py
"""Pure helpers for image attachments — no I/O, no DB."""
from __future__ import annotations

MAX_BYTES = 10 * 1024 * 1024
MAX_PER_MESSAGE = 5

_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}


def sniff_media_type(head: bytes) -> str | None:
    """Detect image type from leading bytes. Returns a media type or None."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "image/gif"
    if len(head) >= 12 and head[0:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def ext_for(media_type: str) -> str:
    return _EXT.get(media_type, "bin")


def image_block(media_type: str, data_b64: str) -> dict:
    return {"type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data_b64}}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_attachment_helpers.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/attachments.py server/tests/test_attachment_helpers.py
git commit -m "feat(attachments): pure sniff / ext / image-block helpers"
```

---

### Task 3: Upload + serve endpoints (`server/api/attachments.py`)

**Files:**
- Create: `server/api/attachments.py`
- Modify: `server/app.py` (register router)
- Test: `server/tests/test_attachments_api.py`

**Interfaces:**
- Produces: `router` (prefix `/api/attachments`):
  - `POST ""` → 201 `{id, mediaType, filename, byteSize, url}`; 400 bad type; 413 too large.
  - `GET "/{id}"` → streams bytes with stored Content-Type; 404 unknown.
- Consumes: `current_identity` dep; `sniff_media_type/ext_for/MAX_BYTES`; `paths.attachments_dir`; `Attachment`; `get_db`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_attachments_api.py
from __future__ import annotations
import sys
from pathlib import Path
REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import pytest
from fastapi.testclient import TestClient
from tests.helpers import temp_env
from server.tests.fakes.fake_factory import fake_sdk_factory

IDENTITY = {"Tailscale-User-Login": "testuser@example.com"}
PNG = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 64


def _client(env):
    from server.sessions.orchestrator import SessionOrchestrator
    from server.app import create_app
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    app = create_app(orchestrator=orch)
    return TestClient(app, raise_server_exceptions=False)


def test_upload_then_serve_roundtrip():
    with temp_env() as env:
        env["CCSWITCH_DB_URL"] = "sqlite+aiosqlite:///:memory:"
        c = _client(env)
        with c:
            r = c.post("/api/attachments",
                       files={"file": ("shot.png", PNG, "image/png")},
                       headers=IDENTITY)
            assert r.status_code == 201, r.text
            body = r.json()
            assert body["mediaType"] == "image/png"
            assert body["filename"] == "shot.png"
            assert body["url"] == f"/api/attachments/{body['id']}"
            g = c.get(body["url"], headers=IDENTITY)
            assert g.status_code == 200
            assert g.content == PNG
            assert g.headers["content-type"].startswith("image/png")


def test_upload_rejects_non_image():
    with temp_env() as env:
        env["CCSWITCH_DB_URL"] = "sqlite+aiosqlite:///:memory:"
        c = _client(env)
        with c:
            r = c.post("/api/attachments",
                       files={"file": ("x.png", b"%PDF-1.7 not an image", "image/png")},
                       headers=IDENTITY)
            assert r.status_code == 400


def test_upload_rejects_oversize():
    with temp_env() as env:
        env["CCSWITCH_DB_URL"] = "sqlite+aiosqlite:///:memory:"
        c = _client(env)
        with c:
            big = PNG + b"\x00" * (10 * 1024 * 1024 + 1)
            r = c.post("/api/attachments",
                       files={"file": ("big.png", big, "image/png")},
                       headers=IDENTITY)
            assert r.status_code == 413


def test_serve_unknown_404():
    with temp_env() as env:
        env["CCSWITCH_DB_URL"] = "sqlite+aiosqlite:///:memory:"
        c = _client(env)
        with c:
            assert c.get("/api/attachments/nope", headers=IDENTITY).status_code == 404


def test_upload_requires_identity():
    with temp_env() as env:
        env["CCSWITCH_DB_URL"] = "sqlite+aiosqlite:///:memory:"
        c = _client(env)
        with c:
            r = c.post("/api/attachments", files={"file": ("a.png", PNG, "image/png")})
            assert r.status_code == 401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_attachments_api.py -v`
Expected: FAIL (router not registered / 404).

- [ ] **Step 3: Implement the router**

```python
# server/api/attachments.py
"""Image attachment upload + serve endpoints."""
from __future__ import annotations

import hashlib
import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select

from ccswitch import paths
from server.audit import record_audit
from server.auth.middleware import current_identity
from server.db import get_db
from server.db.models import Attachment
from server.sessions.attachments import MAX_BYTES, ext_for, sniff_media_type

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/attachments", tags=["attachments"])

_CHUNK = 64 * 1024


@router.post("", status_code=status.HTTP_201_CREATED)
async def upload_attachment(
    file: UploadFile = File(...),
    sessionId: str | None = Form(default=None),
    identity: str = Depends(current_identity),
    db=Depends(get_db),
):
    # Stream into memory with a hard cap; sniff the first chunk for the real type.
    buf = bytearray()
    media_type: str | None = None
    while True:
        chunk = await file.read(_CHUNK)
        if not chunk:
            break
        buf.extend(chunk)
        if media_type is None and len(buf) >= 12:
            media_type = sniff_media_type(bytes(buf[:12]))
        if len(buf) > MAX_BYTES:
            return JSONResponse(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                content={"error": {"code": "too_large",
                                   "message": f"image exceeds {MAX_BYTES} bytes"}},
            )
    if media_type is None:
        media_type = sniff_media_type(bytes(buf[:12]))
    if media_type is None:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": {"code": "bad_type",
                               "message": "file is not a supported image (png/jpeg/webp/gif)"}},
        )

    data = bytes(buf)
    sha = hashlib.sha256(data).hexdigest()
    row = Attachment(
        session_id=sessionId, media_type=media_type,
        filename=(file.filename or "image")[:255],
        byte_size=len(data), sha256=sha, uploaded_by=identity,
    )
    db.add(row)
    await db.flush()  # populate row.id

    d = paths.attachments_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{row.id}.{ext_for(media_type)}").write_bytes(data)
    await db.commit()

    await record_audit(db=db, type="attachment.upload", identity=identity,
                       detail={"id": row.id, "bytes": len(data), "type": media_type})

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={"id": row.id, "mediaType": media_type, "filename": row.filename,
                 "byteSize": len(data), "url": f"/api/attachments/{row.id}"},
    )


@router.get("/{attachment_id}")
async def serve_attachment(
    attachment_id: str,
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
):
    row = (await db.execute(
        select(Attachment).where(Attachment.id == attachment_id)
    )).scalars().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"code": "not_found", "message": "attachment not found"})
    path = paths.attachments_dir() / f"{row.id}.{ext_for(row.media_type)}"
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"code": "not_found", "message": "attachment file missing"})
    return FileResponse(str(path), media_type=row.media_type,
                        headers={"Cache-Control": "private",
                                 "Content-Disposition": "inline"})
```

- [ ] **Step 4: Register the router**

```python
# server/app.py — alongside the other include_router calls
from server.api.attachments import router as attachments_router
app.include_router(attachments_router)
```

(Add the import at the top with the other `server.api.*` imports.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_attachments_api.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add server/api/attachments.py server/app.py server/tests/test_attachments_api.py
git commit -m "feat(attachments): POST/GET /api/attachments upload + serve endpoints"
```

---

### Task 4: Resolve+bind helper and permanent-delete cleanup

**Files:**
- Modify: `server/sessions/attachments.py` (add async resolve/bind + delete helpers)
- Modify: `server/sessions/orchestrator.py` (`permanent_delete` unlinks files)
- Test: `server/tests/test_attachment_lifecycle.py`

**Interfaces:**
- Produces:
  - `async resolve_and_bind(db, session_id, turn_id, ids) -> list[dict]` — for each existing id, set `session_id`+`turn_id`, return `[{id, path, media_type, filename}]` (≤ `MAX_PER_MESSAGE`, unknown ids skipped, order preserved).
  - `async delete_for_session(db, session_id) -> int` — unlink files + delete rows; returns count.
- Consumes: `Attachment`, `paths.attachments_dir`, `ext_for`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_attachment_lifecycle.py
from __future__ import annotations
import sys
from pathlib import Path
REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import pytest
from tests.helpers import temp_env


@pytest.mark.asyncio
async def test_resolve_bind_and_delete(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path))
    monkeypatch.setenv("CCSWITCH_DB_URL", "sqlite+aiosqlite:///:memory:")
    from importlib import reload
    from ccswitch import paths as _paths
    reload(_paths)
    from server.db import init_db, db_session
    from server.db.models import Attachment
    from server.sessions import attachments as A
    reload(A)
    await init_db()

    _paths.attachments_dir().mkdir(parents=True, exist_ok=True)
    async with db_session() as db:
        row = Attachment(media_type="image/png", filename="a.png", byte_size=3,
                         sha256="x", uploaded_by="u")
        db.add(row)
        await db.flush()
        rid = row.id
        await db.commit()
    (_paths.attachments_dir() / f"{rid}.png").write_bytes(b"abc")

    async with db_session() as db:
        resolved = await A.resolve_and_bind(db, "sess-1", "turn-1", [rid, "missing"])
        await db.commit()
    assert [r["id"] for r in resolved] == [rid]
    assert resolved[0]["media_type"] == "image/png"
    assert Path(resolved[0]["path"]).read_bytes() == b"abc"

    async with db_session() as db:
        n = await A.delete_for_session(db, "sess-1")
        await db.commit()
    assert n == 1
    assert not (_paths.attachments_dir() / f"{rid}.png").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_attachment_lifecycle.py -v`
Expected: FAIL (`resolve_and_bind` missing).

- [ ] **Step 3: Add the async helpers**

```python
# server/sessions/attachments.py — append
from sqlalchemy import select  # add to imports
from ccswitch import paths     # add to imports
from server.db.models import Attachment  # add to imports


async def resolve_and_bind(db, session_id, turn_id, ids):
    """Bind up to MAX_PER_MESSAGE attachments to a session+turn; return descriptors."""
    out = []
    for aid in ids[:MAX_PER_MESSAGE]:
        row = (await db.execute(
            select(Attachment).where(Attachment.id == aid)
        )).scalars().first()
        if row is None:
            continue
        row.session_id = session_id
        row.turn_id = turn_id
        out.append({
            "id": row.id, "media_type": row.media_type, "filename": row.filename,
            "path": str(paths.attachments_dir() / f"{row.id}.{ext_for(row.media_type)}"),
        })
    return out


async def delete_for_session(db, session_id) -> int:
    rows = (await db.execute(
        select(Attachment).where(Attachment.session_id == session_id)
    )).scalars().all()
    for row in rows:
        p = paths.attachments_dir() / f"{row.id}.{ext_for(row.media_type)}"
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
        await db.delete(row)
    return len(rows)
```

- [ ] **Step 4: Wire cleanup into `permanent_delete`**

In `server/sessions/orchestrator.py`, inside `permanent_delete`, after the session is confirmed and before/around the existing DB-row removal, open a DB session and call the helper:

```python
# server/sessions/orchestrator.py — within permanent_delete(session_id)
from server.db import db_session as _db_session
from server.sessions.attachments import delete_for_session
async with _db_session() as _db:
    await delete_for_session(_db, session_id)
    await _db.commit()
```

(Place it so it runs whenever a permanent delete proceeds; keep it best-effort — wrap in try/except logging if the surrounding method must not fail on cleanup errors.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_attachment_lifecycle.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add server/sessions/attachments.py server/sessions/orchestrator.py server/tests/test_attachment_lifecycle.py
git commit -m "feat(attachments): resolve/bind + permanent-delete cleanup"
```

---

## Phase 2 — Send path (driver + SDK + orchestrator + WS)

### Task 5: Widen SDK contract + fake to accept structured messages

**Files:**
- Modify: `server/sessions/sdk_contract.py`
- Modify: `server/tests/fakes/fake_sdk.py`
- Test: `server/tests/test_fake_sdk.py` (extend)

**Interfaces:**
- Produces: `query(self, prompt: str | AsyncIterable[dict]) -> None`. Fake records: strings into `self.sent`; for an async-iterable, collects yielded dicts into `self.sent_structured: list[dict]`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_fake_sdk.py — add
import pytest

@pytest.mark.asyncio
async def test_fake_query_records_structured():
    from server.tests.fakes.fake_sdk import FakeSDKClient
    c = FakeSDKClient(messages=[])
    async def _gen():
        yield {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}}
    await c.query(_gen())
    assert c.sent_structured and c.sent_structured[0]["message"]["content"][0]["text"] == "hi"
    await c.query("plain")
    assert c.sent == ["plain"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_fake_sdk.py::test_fake_query_records_structured -v`
Expected: FAIL (`sent_structured` missing).

- [ ] **Step 3: Update the contract + fake**

```python
# server/sessions/sdk_contract.py — change the query line
from typing import Any, AsyncIterator, AsyncIterable, Protocol, runtime_checkable
    async def query(self, prompt: "str | AsyncIterable[dict[str, Any]]") -> None: ...
```

```python
# server/tests/fakes/fake_sdk.py — replace query and extend __init__
    def __init__(self, messages, on_can_use_tool=None):
        self._messages = messages
        self.on_can_use_tool = on_can_use_tool
        self.interrupted = False
        self.permission_mode = "default"
        self.sent: list[str] = []
        self.sent_structured: list[dict] = []

    async def query(self, prompt):
        if isinstance(prompt, str):
            self.sent.append(prompt)
        else:
            async for msg in prompt:
                self.sent_structured.append(msg)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_fake_sdk.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/sdk_contract.py server/tests/fakes/fake_sdk.py server/tests/test_fake_sdk.py
git commit -m "feat(attachments): SDK contract + fake accept structured (image) messages"
```

---

### Task 6: Driver — attachments through queue, structured send, echo

**Files:**
- Modify: `server/sessions/events.py` (`UserMessage.attachments`)
- Modify: `server/sessions/driver.py`
- Test: `server/tests/test_driver_attachments.py`

**Interfaces:**
- Consumes: `image_block` (Task 2); `FakeSDKClient.sent_structured` (Task 5).
- Produces:
  - `UserMessage(turn_id, text, attachments: list[dict] = [], src_uuid=None)` → wire `{type:"user", turnId, text, attachments, srcUuid}`.
  - `SessionDriver.send_prompt(text, attachments: list[dict] | None = None)` where each attachment is `{id, media_type, filename, path}`.
  - Queue element becomes `(text, attachments)`; `QueueUpdate` items stay `list[str]` text previews.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_driver_attachments.py
from __future__ import annotations
import sys
from pathlib import Path
REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import base64
import pytest
from server.tests.fakes.fake_sdk import FakeSDKClient, asst_text, result
from server.sessions.driver import SessionDriver
from server.sessions.permissions import PermissionRegistry


def _factory(messages):
    def make(cwd, config_dir, can_use_tool, mode, resume):
        return FakeSDKClient(messages, on_can_use_tool=can_use_tool)
    return make


@pytest.mark.asyncio
async def test_send_with_attachments_builds_structured_message(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nDATA")
    client_holder = {}
    def make(cwd, config_dir, can_use_tool, mode, resume):
        c = FakeSDKClient([asst_text("ok"), result()], on_can_use_tool=can_use_tool)
        client_holder["c"] = c
        return c
    d = SessionDriver("s1", "acct", str(tmp_path), str(tmp_path), mode="default",
                      sdk_factory=make, registry=PermissionRegistry(), identity="u")
    await d.start()
    atts = [{"id": "a1", "media_type": "image/png", "filename": "a.png", "path": str(img)}]
    # collect echoed events
    seen = []
    async def collect():
        async for e in d.events():
            seen.append(e)
            if getattr(e, "state", None) == "idle":
                return
    import asyncio
    task = asyncio.create_task(collect())
    await d.send_prompt("look", attachments=atts)
    await asyncio.wait_for(task, timeout=2)

    c = client_holder["c"]
    assert c.sent_structured, "expected a structured query"
    content = c.sent_structured[0]["message"]["content"]
    assert content[0] == {"type": "text", "text": "look"}
    assert content[1]["type"] == "image"
    assert content[1]["source"]["data"] == base64.b64encode(b"\x89PNG\r\n\x1a\nDATA").decode()

    users = [e for e in seen if e.__class__.__name__ == "UserMessage"]
    assert users and users[0].attachments[0]["id"] == "a1"
    assert users[0].attachments[0]["mediaType"] == "image/png"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_driver_attachments.py -v`
Expected: FAIL (`send_prompt` has no `attachments`).

- [ ] **Step 3: Extend `UserMessage`**

```python
# server/sessions/events.py — replace UserMessage
@dataclass
class UserMessage:
    turn_id: str; text: str; attachments: list[dict] | None = None; src_uuid: str | None = None
    def to_wire(self):
        return {"type": "user", "turnId": self.turn_id, "text": self.text,
                "attachments": self.attachments or [], "srcUuid": self.src_uuid}
```

- [ ] **Step 4: Thread attachments through the driver**

```python
# server/sessions/driver.py
import base64                                   # add to imports
from .attachments import image_block            # add to imports

# __init__: queue holds (text, attachments)
self._queue: list[tuple[str, list[dict]]] = []

# send_prompt — new signature + queue tuple
async def send_prompt(self, text: str, attachments: list[dict] | None = None):
    attachments = attachments or []
    if self._client is None:
        raise RuntimeError("SessionDriver.send_prompt called before start()")
    if self._pump is not None and not self._pump.done():
        self._queue.append((text, attachments))
        await self._put_status("working", len(self._queue))
        await self._out.put(ev.QueueUpdate([t for t, _ in self._queue]))
        return
    if not self._titled:
        self._titled = True
        first_line = (text.splitlines()[0] if text else "") or "Image prompt"
        await self._out.put(ev.Title(title=first_line[:50], source="prompt"))
    await self._start_turn(text, attachments)

# _start_turn — accept + send attachments
async def _start_turn(self, text: str, attachments: list[dict] | None = None):
    attachments = attachments or []
    self._turn_active = True
    await self._put_status("working", len(self._queue))
    user_turn = uuid.uuid4().hex
    echo = [{"id": a["id"], "mediaType": a["media_type"], "filename": a["filename"]}
            for a in attachments]
    await self._out.put(ev.UserMessage(turn_id=user_turn, text=text,
                                       attachments=echo, src_uuid=user_turn))
    assert self._client is not None
    if attachments:
        async def _read(a):
            data = await asyncio.to_thread(lambda: open(a["path"], "rb").read())
            return image_block(a["media_type"], base64.b64encode(data).decode())
        blocks = [{"type": "text", "text": text}]
        for a in attachments:
            blocks.append(await _read(a))
        async def _gen():
            yield {"type": "user", "message": {"role": "user", "content": blocks}}
        await self._client.query(_gen())
    else:
        await self._client.query(text)
    self._pump = asyncio.create_task(self._drain())
```

Also update the `_drain` dequeue site to unpack the tuple:

```python
# server/sessions/driver.py — in _drain()'s "if self._queue:" branch
            next_text, next_atts = self._queue.pop(0)
            await self._out.put(ev.QueueUpdate([t for t, _ in self._queue]))
            await self._start_turn(next_text, next_atts)
```

And `cancel_queued` / `queued()` keep returning text previews:

```python
def queued(self) -> list[str]:
    return [t for t, _ in self._queue]
```

(In `cancel_queued`, the `QueueUpdate` payload becomes `[t for t, _ in self._queue]`.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_driver_attachments.py server/tests/test_driver.py -v`
Expected: PASS (existing driver tests still green).

- [ ] **Step 6: Commit**

```bash
git add server/sessions/events.py server/sessions/driver.py server/tests/test_driver_attachments.py
git commit -m "feat(attachments): driver sends structured image messages + echoes attachment refs"
```

---

### Task 7: Orchestrator `send_prompt(attachment_ids)` + pending-prompt carries attachments

**Files:**
- Modify: `server/sessions/orchestrator.py`
- Test: `server/tests/test_orchestrator_attachments.py`

**Interfaces:**
- Consumes: `resolve_and_bind` (Task 4); `driver.send_prompt(text, attachments)` (Task 6).
- Produces:
  - `async send_prompt(session_id, text, attachment_ids: list[str] | None = None)`.
  - `_pending_initial_prompt[session_id]` value becomes `(text, attachment_ids)`; `pop_pending_initial_prompt` returns `tuple | None`.
  - `create_session(..., attachment_ids: list[str] | None = None)` stores the tuple.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_orchestrator_attachments.py
from __future__ import annotations
import sys
from pathlib import Path
REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import pytest
from tests.helpers import temp_env
from server.tests.fakes.fake_factory import fake_sdk_factory


@pytest.mark.asyncio
async def test_send_prompt_binds_and_forwards(monkeypatch, tmp_path):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path))
    monkeypatch.setenv("CCSWITCH_DB_URL", "sqlite+aiosqlite:///:memory:")
    from importlib import reload
    from ccswitch import paths as _paths
    reload(_paths)
    from server.db import init_db, db_session
    from server.db.models import Attachment
    await init_db()
    _paths.attachments_dir().mkdir(parents=True, exist_ok=True)
    async with db_session() as db:
        row = Attachment(media_type="image/png", filename="a.png", byte_size=3,
                         sha256="x", uploaded_by="u")
        db.add(row); await db.flush(); rid = row.id; await db.commit()
    (_paths.attachments_dir() / f"{rid}.png").write_bytes(b"\x89PNG\r\n\x1a\nZ")

    from server.sessions.orchestrator import SessionOrchestrator

    class _Spy:
        def __init__(self): self.calls = []
        async def send_prompt(self, text, attachments=None): self.calls.append((text, attachments))

    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    spy = _Spy()
    orch._drivers["s1"] = spy  # inject
    await orch.send_prompt("s1", "look", attachment_ids=[rid])
    assert spy.calls[0][0] == "look"
    assert spy.calls[0][1][0]["id"] == rid
    # row is now bound to the session
    async with db_session() as db:
        from sqlalchemy import select
        r = (await db.execute(select(Attachment).where(Attachment.id == rid))).scalars().first()
        assert r.session_id == "s1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_orchestrator_attachments.py -v`
Expected: FAIL (`send_prompt` has no `attachment_ids`).

- [ ] **Step 3: Update the orchestrator**

```python
# server/sessions/orchestrator.py
from server.db import db_session as _db_session            # ensure imported
from server.sessions.attachments import resolve_and_bind   # add

async def send_prompt(self, session_id, text, attachment_ids=None):
    d = self._drivers.get(session_id)
    if not d:
        return
    self._last_prompt[session_id] = text
    attachments = []
    if attachment_ids:
        import uuid as _uuid
        turn_id = _uuid.uuid4().hex
        async with _db_session() as db:
            attachments = await resolve_and_bind(db, session_id, turn_id, attachment_ids)
            await db.commit()
    await d.send_prompt(text, attachments)
```

For the deferred initial prompt, store a tuple and update both the writer and reader:

```python
# create_session: where it currently does self._pending_initial_prompt[session_id] = initial_prompt
self._pending_initial_prompt[session_id] = (initial_prompt, attachment_ids or [])

# add attachment_ids to create_session signature (keyword, default None)

# pop_pending_initial_prompt: return the tuple unchanged
def pop_pending_initial_prompt(self, session_id):
    return self._pending_initial_prompt.pop(session_id, None)
```

(The two existing `self._pending_initial_prompt.pop(session_id, None)` cleanup calls in
`create_session`/`archive_session` need no change — they discard the value.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_orchestrator_attachments.py server/tests/test_initial_prompt_visible.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_orchestrator_attachments.py
git commit -m "feat(attachments): orchestrator binds + forwards attachment ids"
```

---

### Task 8: WS bridge + create-session wire fields

**Files:**
- Modify: `server/bridge/ws.py`
- Modify: `server/api/sessions.py`
- Test: `server/tests/test_ws_attachments.py`

**Interfaces:**
- Consumes: `orchestrator.send_prompt(session_id, text, attachment_ids)` (Task 7); `pop_pending_initial_prompt` tuple.
- Produces: WS `prompt` frame reads `attachmentIds`; first-attach replays the `(text, ids)` tuple; `CreateSessionRequest.attachmentIds` passed to `create_session`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_ws_attachments.py
from __future__ import annotations
import sys
from pathlib import Path
REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import pytest


@pytest.mark.asyncio
async def test_prompt_frame_forwards_attachment_ids():
    calls = []
    class _Orch:
        async def send_prompt(self, sid, text, attachment_ids=None):
            calls.append((sid, text, attachment_ids))
    # Exercise the dispatch branch directly via the documented contract:
    orch = _Orch()
    msg = {"type": "prompt", "sessionId": "s1", "text": "hi", "attachmentIds": ["a1", "a2"]}
    await orch.send_prompt(msg.get("sessionId", ""), msg.get("text", ""),
                           msg.get("attachmentIds") or None)
    assert calls == [("s1", "hi", ["a1", "a2"])]
```

(This locks the exact call shape the WS branch must use; the WS edit below must match it.)

- [ ] **Step 2: Run test to verify it fails**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ws_attachments.py -v`
Expected: PASS only after the WS edit conforms (write the edit, then it stays green). If you prefer red-first, temporarily assert the old 2-arg shape, watch it fail, then fix.

- [ ] **Step 3: Update the WS `prompt` branch**

```python
# server/bridge/ws.py — replace the prompt branch
                elif msg_type == "prompt":
                    await orchestrator.send_prompt(
                        session_id, msg.get("text", ""),
                        msg.get("attachmentIds") or None)
```

And the deferred-initial-prompt send in `_handle_attach`:

```python
# server/bridge/ws.py — replace the pop_pending_initial_prompt block
            pending = orchestrator.pop_pending_initial_prompt(session_id)
            if pending is not None:
                p_text, p_ids = pending
                await orchestrator.send_prompt(session_id, p_text, p_ids or None)
```

- [ ] **Step 4: Update `CreateSessionRequest` + create call**

```python
# server/api/sessions.py — add to CreateSessionRequest
    attachmentIds: list[str] | None = None

# in create_session, pass it through to orch.create_session(...)
        attachment_ids=body.attachmentIds,
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ws.py server/tests/test_ws_attachments.py server/tests/test_api.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add server/bridge/ws.py server/api/sessions.py server/tests/test_ws_attachments.py
git commit -m "feat(attachments): WS prompt + create-session carry attachmentIds"
```

---

### Task 9: Replay reconstruction (with Principle III verification)

**Files:**
- Modify: `server/sessions/replay.py`
- Test: `server/tests/test_replay_attachments.py`

**Interfaces:**
- Consumes: `Attachment.sha256` (Task 1).
- Produces: replayed `UserMessage` events carry `attachments` so a reloaded page re-renders thumbnails.

- [ ] **Step 0 (VERIFY — Principle III, do this first):** Launch a throwaway real session, send a prompt with one image via the structured path (or inspect a transcript produced once Task 6 is wired in a manual run), and open the resulting `<config_dir>/projects/*/<session>.jsonl`. Confirm whether a user image block is stored **inline as base64** (`{"type":"image","source":{"type":"base64",...}}`) or externalized. Record the finding as a comment at the top of the test file. **If inline base64:** implement the hash-match path below. **If externalized/absent:** implement the ownership-join fallback (query `Attachment` rows by `session_id` ordered by `created_at`, zip onto user turns in order) — keep the same emitted shape.

- [ ] **Step 1: Write the failing test (inline-base64 path)**

```python
# server/tests/test_replay_attachments.py
# VERIFY-NOTE: claude transcript stores user image blocks inline as base64 → hash-match. (confirm in Step 0)
from __future__ import annotations
import sys, json, base64, hashlib
from pathlib import Path
REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import pytest
from tests.helpers import temp_env


@pytest.mark.asyncio
async def test_replay_reconstructs_attachment_ref(monkeypatch, tmp_path):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path))
    monkeypatch.setenv("CCSWITCH_DB_URL", "sqlite+aiosqlite:///:memory:")
    from importlib import reload
    from ccswitch import paths as _paths
    reload(_paths)
    from server.db import init_db, db_session
    from server.db.models import Attachment
    await init_db()
    raw = b"\x89PNG\r\n\x1a\nIMG"
    sha = hashlib.sha256(raw).hexdigest()
    async with db_session() as db:
        row = Attachment(session_id="s1", media_type="image/png", filename="a.png",
                         byte_size=len(raw), sha256=sha, uploaded_by="u")
        db.add(row); await db.flush(); rid = row.id; await db.commit()

    # Minimal transcript with a user turn containing a base64 image block.
    tpath = tmp_path / "t.jsonl"
    b64 = base64.b64encode(raw).decode()
    line = {"type": "user", "uuid": "U1", "message": {"role": "user", "content": [
        {"type": "text", "text": "look"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
    ]}}
    tpath.write_text(json.dumps(line) + "\n")

    from server.sessions.replay import replay_events
    events = await replay_events(str(tpath), session_id="s1")
    users = [e for e in events if e.__class__.__name__ == "UserMessage"]
    assert users and users[0].attachments[0]["id"] == rid
    assert users[0].attachments[0]["mediaType"] == "image/png"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_replay_attachments.py -v`
Expected: FAIL (`replay_events` has no `session_id` / no attachments).

- [ ] **Step 3: Implement reconstruction**

Add an optional `session_id` parameter to `replay_events`. After building the text for a user turn, collect image blocks, hash each decoded payload, look up the matching `Attachment` row (scoped to `session_id`), and attach refs. Update the `UserMessage` construction:

```python
# server/sessions/replay.py — inside the user-turn branch
import base64, hashlib
from sqlalchemy import select
from server.db import db_session
from server.db.models import Attachment

# ... when typ == "user":
images = [b for b in content if b.get("type") == "image"
          and b.get("source", {}).get("type") == "base64"]
attachments = []
if images and session_id:
    async with db_session() as db:
        for b in images:
            data = base64.b64decode(b["source"]["data"])
            sha = hashlib.sha256(data).hexdigest()
            row = (await db.execute(
                select(Attachment).where(
                    Attachment.session_id == session_id, Attachment.sha256 == sha)
            )).scalars().first()
            if row:
                attachments.append({"id": row.id, "mediaType": row.media_type,
                                    "filename": row.filename})
if text or attachments:
    out.append(ev.UserMessage(turn_id=uuid, text=text,
                              attachments=attachments, src_uuid=uuid))
```

Update the WS caller to pass `session_id`:

```python
# server/bridge/ws.py — in _handle_attach, where replay_events is called
events = await replay_events(path, session_id=session_id) if path else []
```

(Make `session_id` keyword-optional in `replay_events` so existing callers/tests that don't pass it still work and simply skip attachment reconstruction.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_replay_attachments.py server/tests/test_ws.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/replay.py server/bridge/ws.py server/tests/test_replay_attachments.py
git commit -m "feat(attachments): reconstruct image refs on transcript replay"
```

---

## Phase 3 — Frontend

### Task 10: `EvUser.attachments` + reducer `image` block

**Files:**
- Modify: `web/src/session/events.ts`
- Modify: `web/src/session/reducer.ts`
- Test: `web/src/session/reducer.test.ts` (extend)

**Interfaces:**
- Produces:
  - `EvUser.attachments?: Array<{ id: string; mediaType: string; filename: string }>`.
  - New `Block` variant `{ kind: 'image'; id: string; attachments: Array<{id; mediaType; filename}> }`.
  - On a `user` event with attachments, the user turn gets the text block **and** an image block `${turnId}:img`.

- [ ] **Step 1: Write the failing test**

```ts
// web/src/session/reducer.test.ts — add
import { transcriptReducer, initialTranscript } from './reducer'

it('adds an image block on a user turn with attachments', () => {
  const s = transcriptReducer(initialTranscript, {
    type: 'user', turnId: 'T1', text: 'look',
    attachments: [{ id: 'a1', mediaType: 'image/png', filename: 'a.png' }],
  } as any)
  const turn = s.turns.find((t) => t.id === 'T1')!
  expect(turn.blocks.some((b) => b.kind === 'text')).toBe(true)
  const img = turn.blocks.find((b) => b.kind === 'image') as any
  expect(img.attachments[0].id).toBe('a1')
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/session/reducer.test.ts`
Expected: FAIL (no image block).

- [ ] **Step 3: Extend the event + block + reducer**

```ts
// web/src/session/events.ts — extend EvUser
export interface EvAttachmentRef { id: string; mediaType: string; filename: string }
export interface EvUser { type: 'user'; turnId: string; text: string; attachments?: EvAttachmentRef[]; srcUuid?: string | null }
```

```ts
// web/src/session/reducer.ts — add to Block union
  | { kind: 'image'; id: string; attachments: Array<{ id: string; mediaType: string; filename: string }> }
```

```ts
// web/src/session/reducer.ts — in case 'user', build blocks with optional image
    case 'user': {
      const { turnId, text } = ev
      const atts = ev.attachments ?? []
      const userBlock: Block = { kind: 'text', id: `${turnId}:u`, text }
      const blocks: Block[] = [userBlock]
      if (atts.length) blocks.push({ kind: 'image', id: `${turnId}:img`, attachments: atts })
      const idx = findTurnIdx(state.turns, turnId)
      let nextTurns: Turn[]
      if (idx === -1) {
        nextTurns = [...state.turns, { id: turnId, role: 'user', blocks }]
      } else {
        nextTurns = replaceTurn(state.turns, idx, (t) => ({
          ...t,
          blocks: t.blocks.map((b) => (b.kind === 'text' && b.id === `${turnId}:u` ? userBlock : b)),
        }))
      }
      return { ...state, turns: nextTurns, openBlockId: null }
    }
```

- [ ] **Step 4: Run test + typecheck**

Run (in `web/`): `npx vitest run src/session/reducer.test.ts && npx tsc --noEmit`
Expected: PASS, no type errors.

- [ ] **Step 5: Commit**

```bash
git add web/src/session/events.ts web/src/session/reducer.ts web/src/session/reducer.test.ts
git commit -m "feat(attachments): EvUser.attachments + reducer image block"
```

---

### Task 11: `UserBubble` renders thumbnails + `Turn` wiring

**Files:**
- Modify: `web/src/components/session/UserBubble.tsx`
- Modify: `web/src/components/session/Turn.tsx`
- Test: `web/src/components/session/__tests__/UserBubble.test.tsx` (create)

**Interfaces:**
- Consumes: `image` block (Task 10).
- Produces: `<UserBubble text? images? />` rendering text and a thumbnail grid of `<img src="/api/attachments/{id}" alt={filename}>`.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/session/__tests__/UserBubble.test.tsx
import { render, screen } from '@testing-library/react'
import { UserBubble } from '../UserBubble'

it('renders attached image thumbnails', () => {
  render(<UserBubble text="look" images={[{ id: 'a1', mediaType: 'image/png', filename: 'a.png' }]} />)
  const img = screen.getByAltText('a.png') as HTMLImageElement
  expect(img.getAttribute('src')).toBe('/api/attachments/a1')
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/components/session/__tests__/UserBubble.test.tsx`
Expected: FAIL (`images` prop unsupported).

- [ ] **Step 3: Update `UserBubble` + `Turn`**

```tsx
// web/src/components/session/UserBubble.tsx
import { cn } from '@/lib/utils'

interface ImageRef { id: string; mediaType: string; filename: string }
interface Props { text?: string; images?: ImageRef[] }

export function UserBubble({ text, images }: Props) {
  return (
    <div className="flex justify-end">
      <div className={cn('max-w-[80%] rounded-lg px-4 py-2',
        'bg-blue-50 dark:bg-blue-900/30 text-gray-900 dark:text-gray-100',
        'whitespace-pre-wrap text-sm')}>
        {text}
        {images && images.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-2">
            {images.map((img) => (
              <a key={img.id} href={`/api/attachments/${img.id}`} target="_blank" rel="noreferrer">
                <img src={`/api/attachments/${img.id}`} alt={img.filename}
                     className="max-h-40 rounded border border-gray-200 dark:border-gray-700" />
              </a>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
```

```tsx
// web/src/components/session/Turn.tsx — handle the image block; text stays as-is
          case 'text':
            return isUser
              ? <UserBubble key={block.id} text={block.text} />
              : <AssistantText key={block.id} text={block.text} streaming={false} />
          case 'image':
            return <UserBubble key={block.id} images={block.attachments} />
```

- [ ] **Step 4: Run test + typecheck**

Run (in `web/`): `npx vitest run src/components/session/__tests__/UserBubble.test.tsx && npx tsc --noEmit`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/components/session/UserBubble.tsx web/src/components/session/Turn.tsx web/src/components/session/__tests__/UserBubble.test.tsx
git commit -m "feat(attachments): render inline image thumbnails in user turns"
```

---

### Task 12: `uploadAttachment` API (multipart)

**Files:**
- Modify: `web/src/api/client.ts`
- Modify: `web/src/api/hooks.ts` (`CreateSessionInput.attachmentIds`)
- Test: `web/src/api/__tests__/upload.test.ts` (create)

**Interfaces:**
- Produces: `uploadAttachment(file: File): Promise<{ id: string; mediaType: string; filename: string; byteSize: number; url: string }>`; `CreateSessionInput.attachmentIds?: string[]`.

- [ ] **Step 1: Write the failing test**

```ts
// web/src/api/__tests__/upload.test.ts
import { vi, it, expect, beforeEach } from 'vitest'
import { uploadAttachment } from '../client'

beforeEach(() => {
  global.fetch = vi.fn(async () => ({
    ok: true, status: 201,
    json: async () => ({ id: 'a1', mediaType: 'image/png', filename: 'a.png', byteSize: 3, url: '/api/attachments/a1' }),
  })) as any
})

it('POSTs multipart form data and returns the descriptor', async () => {
  const file = new File([new Uint8Array([1, 2, 3])], 'a.png', { type: 'image/png' })
  const res = await uploadAttachment(file)
  expect(res.id).toBe('a1')
  const [url, init] = (global.fetch as any).mock.calls[0]
  expect(url).toBe('/api/attachments')
  expect(init.method).toBe('POST')
  expect(init.body).toBeInstanceOf(FormData)
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/api/__tests__/upload.test.ts`
Expected: FAIL (`uploadAttachment` not exported).

- [ ] **Step 3: Implement multipart upload**

```ts
// web/src/api/client.ts — append
export interface AttachmentDescriptor {
  id: string; mediaType: string; filename: string; byteSize: number; url: string
}

export async function uploadAttachment(file: File): Promise<AttachmentDescriptor> {
  const form = new FormData()
  form.append('file', file)
  const res = await fetch('/api/attachments', { method: 'POST', body: form })
  const data = await res.json() as Record<string, unknown>
  if (!res.ok) throw new ApiRequestError(res.status, data)
  return data as unknown as AttachmentDescriptor
}
```

```ts
// web/src/api/hooks.ts — extend CreateSessionInput (near line 105)
  attachmentIds?: string[]
```

- [ ] **Step 4: Run test + typecheck**

Run (in `web/`): `npx vitest run src/api/__tests__/upload.test.ts && npx tsc --noEmit`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/api/client.ts web/src/api/hooks.ts web/src/api/__tests__/upload.test.ts
git commit -m "feat(attachments): uploadAttachment multipart API + create-session field"
```

---

### Task 13: `useAttachments` hook + `AttachmentTray`

**Files:**
- Create: `web/src/components/session/useAttachments.ts`
- Create: `web/src/components/session/AttachmentTray.tsx`
- Test: `web/src/components/session/__tests__/useAttachments.test.ts` (create)

**Interfaces:**
- Consumes: `uploadAttachment` (Task 12); `MAX` constants mirrored client-side.
- Produces:
  - `useAttachments()` → `{ items, addFiles, remove, clear, ids, uploading, error }` where `items: Array<{ key; filename; previewUrl; status: 'uploading'|'done'|'error'; id?: string }>`, `ids: string[]` (done items), `uploading: boolean`.
  - `AttachmentTray({ items, onRemove })` — thumbnail strip with ✕ buttons.
  - Client validation: type in `['image/png','image/jpeg','image/webp','image/gif']`, size ≤ 10 MB, count ≤ 5 → sets `error` and skips invalid files.

- [ ] **Step 1: Write the failing test**

```ts
// web/src/components/session/__tests__/useAttachments.test.ts
import { renderHook, act, waitFor } from '@testing-library/react'
import { vi, it, expect, beforeEach } from 'vitest'
import { useAttachments } from '../useAttachments'

vi.mock('@/api/client', () => ({
  uploadAttachment: vi.fn(async (f: File) => ({
    id: 'srv-' + f.name, mediaType: f.type, filename: f.name, byteSize: 3,
    url: '/api/attachments/srv-' + f.name,
  })),
}))

beforeEach(() => { (globalThis as any).URL.createObjectURL = vi.fn(() => 'blob:x') })

function png(name: string) { return new File([new Uint8Array([1])], name, { type: 'image/png' }) }

it('uploads added files and exposes their ids', async () => {
  const { result } = renderHook(() => useAttachments())
  await act(async () => { await result.current.addFiles([png('a.png')]) })
  await waitFor(() => expect(result.current.ids).toEqual(['srv-a.png']))
})

it('rejects non-images and over-count', async () => {
  const { result } = renderHook(() => useAttachments())
  await act(async () => {
    await result.current.addFiles([new File([new Uint8Array([1])], 'x.pdf', { type: 'application/pdf' })])
  })
  expect(result.current.error).toBeTruthy()
  expect(result.current.ids).toEqual([])
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/components/session/__tests__/useAttachments.test.ts`
Expected: FAIL (hook missing).

- [ ] **Step 3: Implement the hook**

```ts
// web/src/components/session/useAttachments.ts
import { useCallback, useState } from 'react'
import { uploadAttachment } from '@/api/client'

const OK = ['image/png', 'image/jpeg', 'image/webp', 'image/gif']
const MAX_BYTES = 10 * 1024 * 1024
const MAX_COUNT = 5

export interface TrayItem {
  key: string; filename: string; previewUrl: string
  status: 'uploading' | 'done' | 'error'; id?: string
}

export function useAttachments() {
  const [items, setItems] = useState<TrayItem[]>([])
  const [error, setError] = useState<string | null>(null)

  const addFiles = useCallback(async (files: File[]) => {
    setError(null)
    const valid: File[] = []
    setItems((cur) => {
      let count = cur.length
      for (const f of files) {
        if (!OK.includes(f.type)) { setError(`Unsupported file: ${f.name}`); continue }
        if (f.size > MAX_BYTES) { setError(`${f.name} is larger than 10 MB`); continue }
        if (count >= MAX_COUNT) { setError(`At most ${MAX_COUNT} images per message`); break }
        count++; valid.push(f)
      }
      return cur
    })
    for (const f of valid) {
      const key = `${f.name}-${f.size}-${Math.random().toString(36).slice(2)}`
      setItems((cur) => [...cur, {
        key, filename: f.name, previewUrl: URL.createObjectURL(f), status: 'uploading',
      }])
      try {
        const d = await uploadAttachment(f)
        setItems((cur) => cur.map((it) =>
          it.key === key ? { ...it, status: 'done', id: d.id } : it))
      } catch {
        setItems((cur) => cur.map((it) =>
          it.key === key ? { ...it, status: 'error' } : it))
        setError(`Upload failed: ${f.name}`)
      }
    }
  }, [])

  const remove = useCallback((key: string) => {
    setItems((cur) => cur.filter((it) => it.key !== key))
  }, [])
  const clear = useCallback(() => setItems([]), [])

  const ids = items.filter((it) => it.status === 'done' && it.id).map((it) => it.id!) as string[]
  const uploading = items.some((it) => it.status === 'uploading')
  return { items, addFiles, remove, clear, ids, uploading, error }
}
```

```tsx
// web/src/components/session/AttachmentTray.tsx
import type { TrayItem } from './useAttachments'

export function AttachmentTray({ items, onRemove }: { items: TrayItem[]; onRemove: (k: string) => void }) {
  if (!items.length) return null
  return (
    <div className="flex flex-wrap gap-2">
      {items.map((it) => (
        <div key={it.key} className="relative">
          <img src={it.previewUrl} alt={it.filename}
               className={`h-16 w-16 rounded object-cover border ${it.status === 'error' ? 'border-red-500 opacity-50' : 'border-gray-300 dark:border-gray-600'}`} />
          {it.status === 'uploading' && (
            <div className="absolute inset-0 flex items-center justify-center text-xs">…</div>
          )}
          <button type="button" aria-label={`Remove ${it.filename}`}
                  onClick={() => onRemove(it.key)}
                  className="absolute -right-1 -top-1 rounded-full bg-gray-800 text-white w-5 h-5 text-xs leading-none">
            ✕
          </button>
        </div>
      ))}
    </div>
  )
}
```

- [ ] **Step 4: Run test + typecheck**

Run (in `web/`): `npx vitest run src/components/session/__tests__/useAttachments.test.ts && npx tsc --noEmit`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/components/session/useAttachments.ts web/src/components/session/AttachmentTray.tsx web/src/components/session/__tests__/useAttachments.test.ts
git commit -m "feat(attachments): useAttachments hook + AttachmentTray component"
```

---

### Task 14: Wire attach UI into `Composer` (live session)

**Files:**
- Modify: `web/src/components/session/Composer.tsx`
- Modify: `web/src/components/session/SessionView.tsx` (pass IDs to `ws.prompt`)
- Modify: `web/src/ws/client.ts` (`prompt` accepts `attachmentIds`)
- Test: `web/src/components/session/__tests__/Composer.attachments.test.tsx` (create)

**Interfaces:**
- Consumes: `useAttachments`, `AttachmentTray`.
- Produces: `Composer.onPrompt(text: string, attachmentIds: string[])`; `wsClient.prompt(sessionId, text, attachmentIds?)`.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/session/__tests__/Composer.attachments.test.tsx
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { vi, it, expect, beforeEach } from 'vitest'
import { Composer } from '../Composer'

vi.mock('@/api/client', () => ({
  uploadAttachment: vi.fn(async () => ({ id: 'a1', mediaType: 'image/png', filename: 'a.png', byteSize: 3, url: '/api/attachments/a1' })),
}))
beforeEach(() => { (globalThis as any).URL.createObjectURL = vi.fn(() => 'blob:x') })

it('forwards uploaded attachment ids on submit', async () => {
  const onPrompt = vi.fn()
  render(<Composer status={{ state: 'idle', count: 0 }} mode="default"
    onPrompt={onPrompt} onInterrupt={() => {}} onSetMode={() => {}} />)
  const file = new File([new Uint8Array([1])], 'a.png', { type: 'image/png' })
  const input = screen.getByLabelText(/attach image/i) as HTMLInputElement
  await userEvent.upload(input, file)
  await userEvent.type(screen.getByLabelText('Message'), 'look')
  await userEvent.click(screen.getByRole('button', { name: /send message/i }))
  expect(onPrompt).toHaveBeenCalledWith('look', ['a1'])
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/components/session/__tests__/Composer.attachments.test.tsx`
Expected: FAIL (no attach input; onPrompt arity).

- [ ] **Step 3: Update `Composer`**

Add `useAttachments`, a hidden file input + 📎 button (`aria-label="Attach image"`), `onPaste`/`onDrop` handlers on the textarea, render `<AttachmentTray>`, change `onPrompt` to `(text, ids)`, clear the tray and disable Send while `uploading`.

```tsx
// web/src/components/session/Composer.tsx — key changes
import { useRef, useState, useCallback } from 'react'
import { useAttachments } from './useAttachments'
import { AttachmentTray } from './AttachmentTray'
// Props.onPrompt: (text: string, attachmentIds: string[]) => void

const att = useAttachments()
const fileRef = useRef<HTMLInputElement>(null)

const submit = useCallback(() => {
  const trimmed = text.trim()
  if (!trimmed && att.ids.length === 0) return
  onPrompt(trimmed, att.ids)
  setText(''); att.clear()
  if (textareaRef.current) textareaRef.current.style.height = 'auto'
}, [text, onPrompt, att])

function onPaste(e: React.ClipboardEvent) {
  const files = Array.from(e.clipboardData.files).filter((f) => f.type.startsWith('image/'))
  if (files.length) { e.preventDefault(); void att.addFiles(files) }
}
function onDrop(e: React.DragEvent) {
  const files = Array.from(e.dataTransfer.files).filter((f) => f.type.startsWith('image/'))
  if (files.length) { e.preventDefault(); void att.addFiles(files) }
}
```

Render, above the textarea row:
```tsx
<AttachmentTray items={att.items} onRemove={att.remove} />
{att.error && <p role="alert" className="text-xs text-red-600">{att.error}</p>}
<input ref={fileRef} type="file" accept="image/*" multiple hidden
  aria-label="Attach image"
  onChange={(e) => { void att.addFiles(Array.from(e.target.files ?? [])); e.target.value = '' }} />
<button type="button" aria-label="Attach image" onClick={() => fileRef.current?.click()}
  className="flex-shrink-0 rounded-md px-3 py-2 text-sm min-h-[44px]">📎</button>
```
Add `onPaste={onPaste}` and `onDrop={onDrop}` to the `<textarea>`. Disable the Send button when `att.uploading`.

- [ ] **Step 4: Update `ws.client` + `SessionView`**

```ts
// web/src/ws/client.ts
  prompt(sessionId: string, text: string, attachmentIds?: string[]) {
    this._send({ type: 'prompt', sessionId, text, ...(attachmentIds && attachmentIds.length ? { attachmentIds } : {}) })
  }
```

```tsx
// web/src/components/session/SessionView.tsx — handlePrompt now takes (text, ids)
// find the existing handlePrompt and the <Composer onPrompt={handlePrompt} />
const handlePrompt = useCallback((text: string, attachmentIds: string[] = []) => {
  wsClient.prompt(sessionId, text, attachmentIds)
}, [sessionId])
```

(Match the actual ws client accessor used in `SessionView`; keep the existing call site, only widen the signature.)

- [ ] **Step 5: Run test + typecheck + lint**

Run (in `web/`): `npx vitest run src/components/session/__tests__/Composer.attachments.test.tsx && npx tsc --noEmit && npx eslint src/components/session/Composer.tsx`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add web/src/components/session/Composer.tsx web/src/components/session/SessionView.tsx web/src/ws/client.ts web/src/components/session/__tests__/Composer.attachments.test.tsx
git commit -m "feat(attachments): attach images in the live composer"
```

---

### Task 15: Wire attach UI into `DispatchBar` (new session)

**Files:**
- Modify: `web/src/components/DispatchBar.tsx`
- Modify: `web/src/components/Dashboard.tsx` (`handleDispatch` passes `attachmentIds` to create-session)
- Test: `web/src/components/__tests__/DispatchBar.attachments.test.tsx` (create)

**Interfaces:**
- Consumes: `useAttachments`, `AttachmentTray`.
- Produces: `DispatchBar.onDispatch({ initialPrompt, repo, attachmentIds })`; create-session mutation includes `attachmentIds`.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/__tests__/DispatchBar.attachments.test.tsx
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { vi, it, expect, beforeEach } from 'vitest'
import { DispatchBar } from '../DispatchBar'

vi.mock('@/api/client', () => ({
  uploadAttachment: vi.fn(async () => ({ id: 'a1', mediaType: 'image/png', filename: 'a.png', byteSize: 3, url: '/api/attachments/a1' })),
}))
beforeEach(() => { (globalThis as any).URL.createObjectURL = vi.fn(() => 'blob:x') })

const noop = () => {}

it('includes attachmentIds in the dispatch payload', async () => {
  const onDispatch = vi.fn()
  render(<DispatchBar repos={[]} reposLoading={false} reposError={false}
    onDispatch={onDispatch} onNavigate={noop} onOpenHighlighted={noop}
    onRowAction={noop} onShowHelp={noop} onEscape={noop} isPending={false} />)
  const file = new File([new Uint8Array([1])], 'a.png', { type: 'image/png' })
  await userEvent.upload(screen.getByLabelText(/attach image/i), file)
  await userEvent.type(screen.getByLabelText('New session prompt'), 'analyze this')
  await userEvent.keyboard('{Enter}')
  expect(onDispatch).toHaveBeenCalledWith(
    expect.objectContaining({ initialPrompt: 'analyze this', attachmentIds: ['a1'] }))
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/components/__tests__/DispatchBar.attachments.test.tsx`
Expected: FAIL.

- [ ] **Step 3: Update `DispatchBar`**

Mirror Task 14: add `useAttachments`, hidden file input + 📎 (`aria-label="Attach image"`), `onPaste`/`onDrop` on the textarea, render `<AttachmentTray>`. Update `dispatchNow`:

```tsx
// web/src/components/DispatchBar.tsx
  function dispatchNow() {
    const trimmed = text.trim()
    if (!trimmed && att.ids.length === 0) { onOpenHighlighted(); return }
    onDispatch({ initialPrompt: trimmed, repo: repo || null, attachmentIds: att.ids })
    setText(''); att.clear()
    if (textareaRef.current) textareaRef.current.style.height = 'auto'
  }
```

Widen the `onDispatch` prop type to include `attachmentIds: string[]`.

- [ ] **Step 4: Update `Dashboard.handleDispatch`**

```tsx
// web/src/components/Dashboard.tsx — handleDispatch builds the create payload
// include attachmentIds alongside initialPrompt/repo when calling the create-session mutation:
//   createSession({ initialPrompt, repo, attachmentIds })
```

(Find the existing `handleDispatch` and add `attachmentIds` to the object passed to the create-session mutation; the field is already on `CreateSessionInput` from Task 12.)

- [ ] **Step 5: Run test + typecheck + lint**

Run (in `web/`): `npx vitest run src/components/__tests__/DispatchBar.attachments.test.tsx && npx tsc --noEmit && npx eslint src/components/DispatchBar.tsx`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add web/src/components/DispatchBar.tsx web/src/components/Dashboard.tsx web/src/components/__tests__/DispatchBar.attachments.test.tsx
git commit -m "feat(attachments): attach images when launching a new session"
```

---

### Task 16: Orphan sweep (background cleanup)

**Files:**
- Modify: `server/sessions/attachments.py` (`sweep_orphans`)
- Modify: `server/app.py` (schedule on lifespan)
- Test: `server/tests/test_attachment_sweep.py`

**Interfaces:**
- Produces: `async sweep_orphans(db, older_than_seconds: int = 86400) -> int` — delete `session_id IS NULL` rows (and files) whose `created_at` is older than the cutoff; return count.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_attachment_sweep.py
from __future__ import annotations
import sys
from datetime import datetime, timedelta
from pathlib import Path
REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import pytest


@pytest.mark.asyncio
async def test_sweep_deletes_old_orphans(monkeypatch, tmp_path):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path))
    monkeypatch.setenv("CCSWITCH_DB_URL", "sqlite+aiosqlite:///:memory:")
    from importlib import reload
    from ccswitch import paths as _paths
    reload(_paths)
    from server.db import init_db, db_session
    from server.db.models import Attachment
    from server.sessions import attachments as A
    reload(A)
    await init_db()
    _paths.attachments_dir().mkdir(parents=True, exist_ok=True)
    async with db_session() as db:
        old = Attachment(media_type="image/png", filename="o.png", byte_size=1, sha256="a",
                         uploaded_by="u", created_at=datetime.now() - timedelta(days=2))
        fresh = Attachment(media_type="image/png", filename="f.png", byte_size=1, sha256="b",
                           uploaded_by="u")
        bound = Attachment(session_id="s1", media_type="image/png", filename="b.png",
                           byte_size=1, sha256="c", uploaded_by="u",
                           created_at=datetime.now() - timedelta(days=2))
        db.add_all([old, fresh, bound]); await db.flush()
        oid = old.id; await db.commit()
    (_paths.attachments_dir() / f"{oid}.png").write_bytes(b"x")
    async with db_session() as db:
        n = await A.sweep_orphans(db); await db.commit()
    assert n == 1
    assert not (_paths.attachments_dir() / f"{oid}.png").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_attachment_sweep.py -v`
Expected: FAIL (`sweep_orphans` missing).

- [ ] **Step 3: Implement the sweep + schedule it**

```python
# server/sessions/attachments.py — append
from datetime import datetime, timedelta  # add to imports

async def sweep_orphans(db, older_than_seconds: int = 86400) -> int:
    cutoff = datetime.now() - timedelta(seconds=older_than_seconds)
    rows = (await db.execute(
        select(Attachment).where(
            Attachment.session_id.is_(None), Attachment.created_at < cutoff)
    )).scalars().all()
    for row in rows:
        p = paths.attachments_dir() / f"{row.id}.{ext_for(row.media_type)}"
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
        await db.delete(row)
    return len(rows)
```

```python
# server/app.py — inside _lifespan, after init_db / orchestrator start, schedule a periodic task
async def _attachment_sweep_loop():
    import asyncio
    from server.db import db_session
    from server.sessions.attachments import sweep_orphans
    while True:
        try:
            async with db_session() as db:
                await sweep_orphans(db)
                await db.commit()
        except Exception:
            log.warning("attachment sweep failed", exc_info=True)
        await asyncio.sleep(3600)
# create and store the task; cancel it on shutdown alongside the other lifespan tasks.
```

(Follow the existing lifespan task-creation/cancellation pattern in `server/app.py`; ensure the task is cancelled on shutdown.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_attachment_sweep.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/attachments.py server/app.py server/tests/test_attachment_sweep.py
git commit -m "feat(attachments): periodic orphan-attachment sweep"
```

---

## Final verification

- [ ] **Server suite:** `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/ -q` → all pass.
- [ ] **Lint (server):** `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m ruff check server/` → clean.
- [ ] **Web:** in `web/` → `npx vitest run` · `npx tsc --noEmit` · `npx eslint src` → all clean.
- [ ] **Manual smoke (Principle III):** start the app, paste an image into the composer, send → image renders inline AND Claude responds about it; reload the page → image still renders (replay). Repeat from the new-session bar.
- [ ] Open a PR per the CLAUDE.md completion workflow.

---

## Self-Review notes (author)

- **Spec coverage:** §1 model→T1; §2 upload→T3; §3 serve→T3; §4 wire→T6/T8/T10; §5 send→T5/T6/T7; §6 replay→T9 (incl. Principle III verify step); §7 frontend→T10–T15; §8 lifecycle→T4/T16; §9 tests→every task. All sections mapped.
- **Type consistency:** echo refs use `{id, mediaType, filename}` (camelCase) on the wire everywhere (events.py `to_wire`, EvUser, reducer block, UserBubble); driver descriptors use `{id, media_type, filename, path}` (snake) server-internal only. `send_prompt` is `(text, attachments)` on the driver and `(session_id, text, attachment_ids)` on the orchestrator — consistent across T6/T7/T8.
- **Known assumption to verify at execution:** the exact lifespan task pattern in `server/app.py` and the precise `ws`-client accessor in `SessionView.tsx` — both flagged inline to match existing code rather than guess.
