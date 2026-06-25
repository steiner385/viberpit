# Self-Hosted Web Push Notifications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver browser-native push notifications from ccswitch itself (no third-party service) to Android, fired by the existing session/PR event detectors.

**Architecture:** A service worker receives VAPID-signed, end-to-end-encrypted Web Push messages. The existing `push()` webhook sink stays; a new `notify()` dispatcher fans out to both it and a new web-push sink that sends to every stored browser subscription via `pywebpush`. The orchestrator's two existing notify call sites switch from `push()` to `notify()`. A small `/api/push/*` router stores subscriptions; a settings toggle subscribes the browser.

**Tech Stack:** Python FastAPI + SQLAlchemy(async SQLite) backend; `pywebpush` for Web Push; `cryptography` (already a dep) for VAPID keygen; React + Vite + TS frontend; a static service worker.

## Global Constraints

- **Dependency floors (verbatim):** `cryptography>=43`, `claude-agent-sdk==0.2.105`. New dep added to the `[server]` optional-dependencies extra only.
- **New DB columns MUST be nullable** — dev SQLite `init_db` backfills via `create_all`; no ALTER. A NOT NULL column needs a real migration (forbidden here).
- **Timestamps:** use `server/time_iso.py:iso_utc` for any new wire/stored timestamp; never bare `datetime.utcnow().isoformat()`.
- **No parallel implementations:** there is exactly ONE notification dispatcher (`notify()`); the webhook and web-push sinks live behind it. Do not duplicate the event detection.
- **Fire-and-forget:** every sink and the dispatcher swallow all exceptions and never raise into the caller (matches the existing `push()` contract).
- **Lint the WHOLE tree before every commit:** `ruff check src/ server/` and (web) `eslint . --max-warnings 0` — test-file violations have repeatedly broken CI. Real type gate is `tsc -b` (NOT `--noEmit`).
- **Backend test env (this worktree):** clone venv `/mnt/ssk-ssd/tony/GitHub/.ccswitch/clones/steiner385/cc-account-switcher/.venv/bin/python`, `PYTHONPATH=$PWD/src`, and `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1`. API tests build the app with an **injected orchestrator** (`create_app(orchestrator=orch)`) — this is required on the deploy host to bypass live-daemon-socket detection.
- **Web test env:** `web/node_modules` must be a real `npm ci` install (NOT the clone symlink — it misses `react-router-dom`/`sonner`). Run `rm -f web/node_modules && npm ci` first.

---

## File Structure

- `pyproject.toml` — add `pywebpush` to the `[server]` extra.
- `server/config.py` — add `VAPID_PRIVATE_KEY`, `VAPID_PUBLIC_KEY`, `VAPID_SUBJECT` module constants.
- `server/sessions/push.py` — add `generate_vapid_keys()`, the web-push sink, and the `notify()` dispatcher (keep `push()` as the webhook sink).
- `server/sessions/push_store.py` (new) — subscription persistence: `upsert_subscription`, `delete_subscription`, `list_subscriptions`.
- `server/db/models.py` — add `PushSubscription` model.
- `server/api/push.py` (new) — `/api/push/*` router.
- `server/app.py` — mount the push router before `mount_spa`.
- `server/sessions/orchestrator.py:678,704` — `push_mod.push(...)` → `push_mod.notify(...)`.
- `web/public/sw.js` (new) — the service worker.
- `web/src/api/push.ts` (new) — typed client functions for `/api/push/*`.
- `web/src/components/settings/NotificationsToggle.tsx` (new) + `web/src/components/settings/SettingsPanel.tsx` (wire it in).
- Tests: `server/tests/test_vapid_keygen.py`, `test_push_subscription_store.py`, `test_push_notify.py`, `test_api_push.py`; `web/src/components/settings/__tests__/NotificationsToggle.test.tsx`.

---

### Task 1: Dependency + VAPID config + keygen helper

**Files:**
- Modify: `pyproject.toml` (add to `[server]` extra)
- Modify: `server/config.py` (after line 153)
- Modify: `server/sessions/push.py` (add `generate_vapid_keys`)
- Test: `server/tests/test_vapid_keygen.py`

**Interfaces:**
- Produces: `server.sessions.push.generate_vapid_keys() -> tuple[str, str]` returning `(private_key_pem, application_server_key_b64url)`. Config: `server.config.VAPID_PRIVATE_KEY: str | None`, `VAPID_PUBLIC_KEY: str | None`, `VAPID_SUBJECT: str` (defaults `"mailto:ccswitch@localhost"`).

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_vapid_keygen.py
"""VAPID keypair generation for self-hosted Web Push."""
from __future__ import annotations

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from server.sessions.push import generate_vapid_keys


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_generate_vapid_keys_shapes():
    priv_pem, app_server_key = generate_vapid_keys()
    assert priv_pem.startswith("-----BEGIN PRIVATE KEY-----")
    raw = _b64url_decode(app_server_key)
    # Uncompressed P-256 point: 65 bytes, leading 0x04.
    assert len(raw) == 65
    assert raw[0] == 0x04
    assert "=" not in app_server_key  # base64url, unpadded (browser applicationServerKey)


def test_generate_vapid_keys_unique():
    assert generate_vapid_keys()[0] != generate_vapid_keys()[0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_vapid_keygen.py -v`
Expected: FAIL — `ImportError: cannot import name 'generate_vapid_keys'`.

- [ ] **Step 3: Add the dependency, config constants, and the keygen helper**

In `pyproject.toml`, add `"pywebpush"` to the `[project.optional-dependencies]` `server` list (alongside `fastapi`, `aiosqlite`, …).

In `server/config.py`, after line 153 (`PUSH_WEBHOOK_URL = ...`):

```python
VAPID_PRIVATE_KEY: str | None = (os.environ.get("CCSWITCH_VAPID_PRIVATE_KEY") or "").strip() or None
VAPID_PUBLIC_KEY: str | None = (os.environ.get("CCSWITCH_VAPID_PUBLIC_KEY") or "").strip() or None
VAPID_SUBJECT: str = (os.environ.get("CCSWITCH_VAPID_SUBJECT") or "").strip() or "mailto:ccswitch@localhost"
```

In `server/sessions/push.py`, add at the top of the module (after the existing imports):

```python
import base64
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def generate_vapid_keys() -> tuple[str, str]:
    """Generate a VAPID keypair. Returns (private_key_pem, application_server_key_b64url).

    The PEM private key is what pywebpush signs with; the application server key is the
    unpadded-base64url uncompressed P-256 point the browser passes as applicationServerKey.
    Run once: `python -m server.sessions.push --keygen`, paste the output into the env file.
    """
    priv = ec.generate_private_key(ec.SECP256R1())
    pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    point = priv.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    app_server_key = base64.urlsafe_b64encode(point).rstrip(b"=").decode()
    return pem, app_server_key
```

Also add a `__main__` keygen entrypoint at the END of `server/sessions/push.py`:

```python
if __name__ == "__main__":  # pragma: no cover
    import sys
    if "--keygen" in sys.argv:
        priv, pub = generate_vapid_keys()
        print("# Paste into /etc/ccswitch/ccswitch.env, then restart the services:")
        print(f"CCSWITCH_VAPID_PRIVATE_KEY={priv!r}")
        print(f"CCSWITCH_VAPID_PUBLIC_KEY={pub}")
```

Install the new dep into the clone venv so tests can import it later:
`/mnt/ssk-ssd/tony/GitHub/.ccswitch/clones/steiner385/cc-account-switcher/.venv/bin/python -m pip install pywebpush`

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_vapid_keygen.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml server/config.py server/sessions/push.py server/tests/test_vapid_keygen.py
git commit -m "feat(push): VAPID keypair generation + config + pywebpush dep"
```

---

### Task 2: `PushSubscription` model

**Files:**
- Modify: `server/db/models.py`
- Test: `server/tests/test_push_subscription_model.py`

**Interfaces:**
- Produces: `server.db.models.PushSubscription` with columns `endpoint: str` (PK), `p256dh: str`, `auth: str`, `identity: str | None`, `created_at: str | None`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_push_subscription_model.py
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))


def test_push_subscription_columns():
    from server.db.models import PushSubscription
    cols = PushSubscription.__table__.columns
    assert "endpoint" in cols and cols["endpoint"].primary_key
    for name in ("p256dh", "auth", "identity", "created_at"):
        assert name in cols
    # identity + created_at must be nullable (init_db backfills; no migration).
    assert cols["identity"].nullable
    assert cols["created_at"].nullable
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_push_subscription_model.py -v`
Expected: FAIL — `ImportError: cannot import name 'PushSubscription'`.

- [ ] **Step 3: Add the model**

In `server/db/models.py`, following the existing `mapped_column` style, add:

```python
class PushSubscription(Base):
    """A browser Web Push subscription. endpoint is the natural unique key."""

    __tablename__ = "push_subscriptions"

    endpoint: Mapped[str] = mapped_column(String, primary_key=True)
    p256dh: Mapped[str] = mapped_column(String, nullable=False)
    auth: Mapped[str] = mapped_column(String, nullable=False)
    # Nullable so init_db backfills existing rows without a migration.
    identity: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str | None] = mapped_column(String, nullable=True)
```

(Use whatever `Base`, `Mapped`, `mapped_column`, `String` import the file already has.)

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_push_subscription_model.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/db/models.py server/tests/test_push_subscription_model.py
git commit -m "feat(push): push_subscriptions model (nullable cols)"
```

---

### Task 3: Subscription store (persistence helpers)

**Files:**
- Create: `server/sessions/push_store.py`
- Test: `server/tests/test_push_subscription_store.py`

**Interfaces:**
- Consumes: `server.db.models.PushSubscription`; `server.db.db_session` (async context manager yielding an `AsyncSession`); `server.time_iso.iso_utc`.
- Produces:
  - `async def upsert_subscription(db, *, endpoint: str, p256dh: str, auth: str, identity: str | None) -> None`
  - `async def delete_subscription(db, endpoint: str) -> None`
  - `async def list_subscriptions(db) -> list[PushSubscription]`

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_push_subscription_store.py
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))


@pytest.mark.asyncio
async def test_upsert_list_delete(tmp_path):
    from server.db import init_db, db_session
    from server.sessions import push_store

    await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")

    async with db_session() as db:
        await push_store.upsert_subscription(db, endpoint="https://x/1", p256dh="k", auth="a", identity="me")
        await db.commit()
    async with db_session() as db:
        subs = await push_store.list_subscriptions(db)
        assert len(subs) == 1 and subs[0].endpoint == "https://x/1"
        assert subs[0].created_at  # stamped via iso_utc

    # re-upsert same endpoint = idempotent (no duplicate, updates keys)
    async with db_session() as db:
        await push_store.upsert_subscription(db, endpoint="https://x/1", p256dh="k2", auth="a2", identity="me")
        await db.commit()
    async with db_session() as db:
        subs = await push_store.list_subscriptions(db)
        assert len(subs) == 1 and subs[0].p256dh == "k2"

    async with db_session() as db:
        await push_store.delete_subscription(db, "https://x/1")
        await db.commit()
    async with db_session() as db:
        assert await push_store.list_subscriptions(db) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_push_subscription_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.sessions.push_store'`.

- [ ] **Step 3: Implement the store**

```python
# server/sessions/push_store.py
"""Persistence for browser Web Push subscriptions."""
from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.db.models import PushSubscription
from server.time_iso import iso_utc


async def upsert_subscription(db: AsyncSession, *, endpoint: str, p256dh: str,
                              auth: str, identity: str | None) -> None:
    row = await db.get(PushSubscription, endpoint)
    if row is None:
        db.add(PushSubscription(endpoint=endpoint, p256dh=p256dh, auth=auth,
                                identity=identity, created_at=iso_utc()))
    else:
        row.p256dh, row.auth, row.identity = p256dh, auth, identity


async def delete_subscription(db: AsyncSession, endpoint: str) -> None:
    await db.execute(delete(PushSubscription).where(PushSubscription.endpoint == endpoint))


async def list_subscriptions(db: AsyncSession) -> list[PushSubscription]:
    return list((await db.execute(select(PushSubscription))).scalars().all())
```

(Confirm `iso_utc` is callable with no args producing an ISO-8601 UTC string; if its signature differs, call it as the codebase does elsewhere.)

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_push_subscription_store.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/push_store.py server/tests/test_push_subscription_store.py
git commit -m "feat(push): subscription store (upsert/list/delete)"
```

---

### Task 4: Web-push sink + `notify()` dispatcher

**Files:**
- Modify: `server/sessions/push.py`
- Test: `server/tests/test_push_notify.py`

**Interfaces:**
- Consumes: `server.config.VAPID_PRIVATE_KEY/VAPID_PUBLIC_KEY/VAPID_SUBJECT`; `server.sessions.push_store`; `server.db.db_session`; `pywebpush.webpush`, `pywebpush.WebPushException`.
- Produces:
  - `async def notify(title: str, body: str, *, url: str | None = None) -> None` — the dispatcher.
  - `async def _webpush_sink(payload: dict, *, sender=webpush) -> None` — sends to all subs, prunes dead ones.
  - Keeps existing `async def push(title, body, *, url=None, opener=...) -> bool` UNCHANGED (the webhook sink).

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_push_notify.py
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))


@pytest.mark.asyncio
async def test_webpush_sink_sends_and_prunes_410(tmp_path, monkeypatch):
    from server.db import init_db, db_session
    from server.sessions import push as push_mod, push_store
    import server.config as config

    await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    monkeypatch.setattr(config, "VAPID_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----")
    monkeypatch.setattr(config, "VAPID_PUBLIC_KEY", "pub")

    async with db_session() as db:
        await push_store.upsert_subscription(db, endpoint="https://good/1", p256dh="k", auth="a", identity=None)
        await push_store.upsert_subscription(db, endpoint="https://dead/2", p256dh="k", auth="a", identity=None)
        await db.commit()

    sent = []

    class FakeResp:
        def __init__(self, code): self.status_code = code

    def fake_sender(*, subscription_info, data, vapid_private_key, vapid_claims, timeout):
        sent.append(subscription_info["endpoint"])
        if subscription_info["endpoint"] == "https://dead/2":
            exc = push_mod.WebPushException("gone")
            exc.response = FakeResp(410)
            raise exc
        return FakeResp(201)

    await push_mod._webpush_sink({"title": "t", "body": "b"}, sender=fake_sender)

    assert set(sent) == {"https://good/1", "https://dead/2"}
    async with db_session() as db:
        remaining = [s.endpoint for s in await push_store.list_subscriptions(db)]
    assert remaining == ["https://good/1"]  # the 410 endpoint was pruned


@pytest.mark.asyncio
async def test_webpush_sink_noop_without_vapid(monkeypatch):
    from server.sessions import push as push_mod
    import server.config as config
    monkeypatch.setattr(config, "VAPID_PRIVATE_KEY", None)
    # Must not raise and must not touch the DB.
    await push_mod._webpush_sink({"title": "t", "body": "b"}, sender=lambda **k: None)


@pytest.mark.asyncio
async def test_notify_fans_out_to_both_sinks(monkeypatch):
    from server.sessions import push as push_mod
    calls = {"webhook": 0, "webpush": 0}

    async def fake_push(title, body, *, url=None, opener=None):
        calls["webhook"] += 1
        return True

    async def fake_webpush(payload, *, sender=None):
        calls["webpush"] += 1

    monkeypatch.setattr(push_mod, "push", fake_push)
    monkeypatch.setattr(push_mod, "_webpush_sink", fake_webpush)
    await push_mod.notify("t", "b", url="https://app/session/1")
    assert calls == {"webhook": 1, "webpush": 1}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_push_notify.py -v`
Expected: FAIL — `AttributeError: module 'server.sessions.push' has no attribute '_webpush_sink'` (and no `WebPushException`/`notify`).

- [ ] **Step 3: Implement the sink + dispatcher**

In `server/sessions/push.py`, add imports near the top:

```python
from pywebpush import WebPushException, webpush

from server.db import db_session
from server.sessions import push_store
```

Then add:

```python
def _send_one(subscription_info: dict, data: str, *, sender) -> int:
    """Send one Web Push. Returns the HTTP status; raises WebPushException on failure."""
    resp = sender(
        subscription_info=subscription_info,
        data=data,
        vapid_private_key=config.VAPID_PRIVATE_KEY,
        vapid_claims={"sub": config.VAPID_SUBJECT},
        timeout=5,
    )
    return getattr(resp, "status_code", 0)


async def _webpush_sink(payload: dict, *, sender=webpush) -> None:
    """Send `payload` to every stored subscription. Prune 404/410. Never raises."""
    if not config.VAPID_PRIVATE_KEY:
        return
    data = json.dumps(payload)
    async with db_session() as db:
        subs = await push_store.list_subscriptions(db)
        dead: list[str] = []
        for s in subs:
            sub_info = {"endpoint": s.endpoint,
                        "keys": {"p256dh": s.p256dh, "auth": s.auth}}
            try:
                await asyncio.to_thread(_send_one, sub_info, data, sender=sender)
            except WebPushException as e:
                if getattr(getattr(e, "response", None), "status_code", None) in (404, 410):
                    dead.append(s.endpoint)
                else:
                    log.warning("webpush.send_failed", exc_info=True)
            except Exception:
                log.warning("webpush.send_failed", exc_info=True)
        for endpoint in dead:
            await push_store.delete_subscription(db, endpoint)
        if dead:
            await db.commit()


async def notify(title: str, body: str, *, url: str | None = None) -> None:
    """Fan a notification out to all sinks (webhook + web-push). Fire-and-forget; never raises."""
    try:
        await push(title, body, url=url)
    except Exception:
        log.warning("notify.webhook_failed", exc_info=True)
    payload = {"title": title, "body": body}
    if url:
        payload["url"] = url
    try:
        await _webpush_sink(payload)
    except Exception:
        log.warning("notify.webpush_failed", exc_info=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_push_notify.py server/tests/test_push.py -v`
Expected: PASS (new tests pass; the existing webhook `push()` tests still pass — `push()` is unchanged).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/push.py server/tests/test_push_notify.py
git commit -m "feat(push): web-push sink + notify() dispatcher (webhook + web-push)"
```

---

### Task 5: `/api/push/*` router + mount

**Files:**
- Create: `server/api/push.py`
- Modify: `server/app.py` (import + `include_router` before `mount_spa`)
- Test: `server/tests/test_api_push.py`

**Interfaces:**
- Consumes: `server.api.settings` patterns (`APIRouter`, `current_identity`, `get_db`); `server.sessions.push_store`; `server.config`.
- Produces: router `server.api.push.router` with `GET /api/push/config`, `POST /api/push/subscribe`, `DELETE /api/push/subscribe`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_api_push.py
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

IDENTITY = {"Tailscale-User-Login": "testuser@example.com"}


def _client(monkeypatch):
    import server.config as config
    monkeypatch.setattr(config, "VAPID_PUBLIC_KEY", "PUBKEY")
    monkeypatch.setattr(config, "VAPID_PRIVATE_KEY", "PRIV")
    from server.sessions.orchestrator import SessionOrchestrator
    from server.app import create_app
    from fastapi.testclient import TestClient
    with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
        orch = SessionOrchestrator(poll_interval=9999.0)
        return TestClient(create_app(orchestrator=orch), raise_server_exceptions=False)


def test_config_returns_public_key(monkeypatch):
    c = _client(monkeypatch)
    r = c.get("/api/push/config", headers=IDENTITY)
    assert r.status_code == 200
    assert r.json() == {"vapidPublicKey": "PUBKEY", "enabled": True}


def test_subscribe_then_unsubscribe(monkeypatch):
    c = _client(monkeypatch)
    sub = {"endpoint": "https://push/1", "keys": {"p256dh": "k", "auth": "a"}}
    assert c.post("/api/push/subscribe", json=sub, headers=IDENTITY).status_code == 200
    # idempotent re-subscribe
    assert c.post("/api/push/subscribe", json=sub, headers=IDENTITY).status_code == 200
    r = c.request("DELETE", "/api/push/subscribe", json={"endpoint": "https://push/1"}, headers=IDENTITY)
    assert r.status_code == 200
    # unauthenticated is rejected
    assert c.get("/api/push/config").status_code == 401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_api_push.py -v`
Expected: FAIL — 404 on `/api/push/config` (router not mounted).

- [ ] **Step 3: Implement the router and mount it**

```python
# server/api/push.py
"""Web Push subscription API: VAPID public key + subscribe/unsubscribe."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from server import config
from server.auth.middleware import current_identity
from server.db import get_db
from server.sessions import push_store

router = APIRouter(prefix="/api/push", tags=["push"])


class SubscriptionKeys(BaseModel):
    p256dh: str
    auth: str


class SubscribeBody(BaseModel):
    endpoint: str
    keys: SubscriptionKeys


class UnsubscribeBody(BaseModel):
    endpoint: str


@router.get("/config")
async def push_config(identity: str = Depends(current_identity)) -> dict:
    return {"vapidPublicKey": config.VAPID_PUBLIC_KEY, "enabled": bool(config.VAPID_PRIVATE_KEY)}


@router.post("/subscribe")
async def subscribe(body: SubscribeBody, identity: str = Depends(current_identity), db=Depends(get_db)) -> dict:
    await push_store.upsert_subscription(
        db, endpoint=body.endpoint, p256dh=body.keys.p256dh, auth=body.keys.auth, identity=identity)
    await db.commit()
    return {"ok": True}


@router.delete("/subscribe")
async def unsubscribe(body: UnsubscribeBody, identity: str = Depends(current_identity), db=Depends(get_db)) -> dict:
    await push_store.delete_subscription(db, body.endpoint)
    await db.commit()
    return {"ok": True}
```

In `server/app.py`, import the router with the other API routers and add (with the other `include_router` calls, all of which run before `mount_spa`):

```python
from server.api.push import router as push_router
...
app.include_router(push_router)
```

(Confirm `current_identity` returns the identity string and is the same dependency `settings.py` uses; mirror its exact import.)

- [ ] **Step 4: Run test to verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_api_push.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/api/push.py server/app.py server/tests/test_api_push.py
git commit -m "feat(push): /api/push config + subscribe/unsubscribe router"
```

---

### Task 6: Switch orchestrator call sites to `notify()`

**Files:**
- Modify: `server/sessions/orchestrator.py:678,704`
- Test: `server/tests/test_orchestrator_push.py` (the existing `_maybe_push` test — find it and update; if none exists, add the test below)

**Interfaces:**
- Consumes: `server.sessions.push.notify` (Task 4).

- [ ] **Step 1: Update/Write the test**

Locate the existing test that asserts `push_mod.push` is called from `_maybe_push` (grep `_maybe_push` and `push` under `server/tests/`). Change its expectation from `push` to `notify`. If no such test exists, add:

```python
# server/tests/test_orchestrator_push.py
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))


@pytest.mark.asyncio
async def test_maybe_push_calls_notify(monkeypatch):
    from server.sessions import push as push_mod
    calls = []

    async def fake_notify(title, body, *, url=None):
        calls.append((title, body))

    monkeypatch.setattr(push_mod, "notify", fake_notify)
    # Drive _maybe_push the way the existing orchestrator tests construct an orchestrator
    # + a session in a needs-you state, then assert calls is non-empty.
    # (Mirror the existing orchestrator test setup in server/tests/.)
    assert hasattr(push_mod, "notify")
```

- [ ] **Step 2: Run test to verify it fails (or the existing one fails after editing the expectation)**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_orchestrator_push.py -v`
Expected: the edited existing test FAILS because the code still calls `push`.

- [ ] **Step 3: Switch the call sites**

In `server/sessions/orchestrator.py`, change line 678:

```python
                await push_mod.notify("ccswitch — needs you", f"{name}: {status}")
```

and line 704:

```python
        await push_mod.notify("ccswitch — PR broke", f"{rec.name}: {repo} CI failing")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_orchestrator_push.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_orchestrator_push.py
git commit -m "feat(push): route orchestrator notifications through notify() dispatcher"
```

---

### Task 7: Service worker

**Files:**
- Create: `web/public/sw.js`

**Interfaces:** none (browser runtime). Vite copies `web/public/*` to `dist/` root → served at `/sw.js` at root scope by `SPAStaticFiles`.

- [ ] **Step 1: Write the service worker**

```javascript
// web/public/sw.js — ccswitch Web Push service worker (minimal).
self.addEventListener('push', (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (_e) { data = {}; }
  const title = data.title || 'ccswitch';
  const options = {
    body: data.body || '',
    data: { url: data.url || '/' },
    icon: '/favicon.svg',
    tag: data.tag || 'ccswitch',
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil((async () => {
    const all = await clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const c of all) {
      if ('focus' in c) { c.navigate(url); return c.focus(); }
    }
    return clients.openWindow(url);
  })());
});
```

- [ ] **Step 2: Build and verify it's served correctly**

Run (from `web/`): `npm run build` then start the app and check:
`curl -sI https://dobby.tail0dd570.ts.net/sw.js | grep -i content-type`
Expected: a JavaScript content-type (e.g. `text/javascript` or `application/javascript`), NOT `text/html`. If it returns `text/html`, the SPA fallback is intercepting it — confirm `web/dist/sw.js` exists after build (Vite copies `public/`); no code change should be needed since it's a real file.

- [ ] **Step 3: Commit**

```bash
git add web/public/sw.js
git commit -m "feat(push): service worker (push -> showNotification, click -> open session)"
```

---

### Task 8: Frontend — `NotificationsToggle` + settings wiring

**Files:**
- Create: `web/src/api/push.ts`
- Create: `web/src/components/settings/NotificationsToggle.tsx`
- Modify: `web/src/components/settings/SettingsPanel.tsx` (render `<NotificationsToggle />`)
- Test: `web/src/components/settings/__tests__/NotificationsToggle.test.tsx`

**Interfaces:**
- Consumes: `web/src/api/client.ts` (`api.get`/`api.post`/`api.del` — mirror the exact method names the file exports).
- Produces: `urlBase64ToUint8Array(b64: string): Uint8Array`, `enablePush(): Promise<'subscribed'|'denied'|'unsupported'>`, `disablePush(): Promise<void>` in `web/src/api/push.ts`.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/settings/__tests__/NotificationsToggle.test.tsx
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { NotificationsToggle } from '../NotificationsToggle'

vi.mock('@/api/client', () => ({
  api: {
    get: vi.fn(async () => ({ vapidPublicKey: 'BPUB', enabled: true })),
    post: vi.fn(async () => ({ ok: true })),
    del: vi.fn(async () => ({ ok: true })),
  },
}))

describe('NotificationsToggle', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('shows unsupported when the browser lacks Push', () => {
    // @ts-expect-error force-unset
    delete (globalThis as any).PushManager
    render(<NotificationsToggle />)
    expect(screen.getByText(/not supported/i)).toBeInTheDocument()
  })

  it('subscribes on enable when supported and permission granted', async () => {
    ;(globalThis as any).PushManager = function () {}
    ;(globalThis as any).Notification = { requestPermission: vi.fn(async () => 'granted'), permission: 'default' }
    const subscribe = vi.fn(async () => ({
      toJSON: () => ({ endpoint: 'https://p/1', keys: { p256dh: 'k', auth: 'a' } }),
    }))
    ;(navigator as any).serviceWorker = {
      register: vi.fn(async () => ({ pushManager: { getSubscription: vi.fn(async () => null), subscribe } })),
      ready: Promise.resolve({ pushManager: { getSubscription: vi.fn(async () => null), subscribe } }),
    }
    const { api } = await import('@/api/client')
    render(<NotificationsToggle />)
    fireEvent.click(await screen.findByRole('button', { name: /enable/i }))
    await waitFor(() => expect(api.post).toHaveBeenCalledWith('/push/subscribe', expect.objectContaining({ endpoint: 'https://p/1' })))
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `web/`): `./node_modules/.bin/vitest run src/components/settings/__tests__/NotificationsToggle.test.tsx`
Expected: FAIL — cannot resolve `../NotificationsToggle`.

- [ ] **Step 3: Implement the api helpers and the component**

```ts
// web/src/api/push.ts
import { api } from '@/api/client'

export function urlBase64ToUint8Array(base64: string): Uint8Array {
  const padding = '='.repeat((4 - (base64.length % 4)) % 4)
  const b64 = (base64 + padding).replace(/-/g, '+').replace(/_/g, '/')
  const raw = atob(b64)
  const out = new Uint8Array(raw.length)
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i)
  return out
}

export function pushSupported(): boolean {
  return typeof window !== 'undefined'
    && 'serviceWorker' in navigator
    && 'PushManager' in globalThis
    && 'Notification' in globalThis
}

export async function enablePush(): Promise<'subscribed' | 'denied' | 'unsupported'> {
  if (!pushSupported()) return 'unsupported'
  const cfg = await api.get<{ vapidPublicKey: string; enabled: boolean }>('/push/config')
  if (!cfg.enabled || !cfg.vapidPublicKey) return 'unsupported'
  const permission = await Notification.requestPermission()
  if (permission !== 'granted') return 'denied'
  const reg = await navigator.serviceWorker.register('/sw.js')
  const existing = await reg.pushManager.getSubscription()
  const sub = existing ?? await reg.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: urlBase64ToUint8Array(cfg.vapidPublicKey),
  })
  await api.post('/push/subscribe', sub.toJSON())
  return 'subscribed'
}

export async function disablePush(): Promise<void> {
  if (!pushSupported()) return
  const reg = await navigator.serviceWorker.ready
  const sub = await reg.pushManager.getSubscription()
  if (sub) {
    await api.del('/push/subscribe', { endpoint: sub.endpoint })
    await sub.unsubscribe()
  }
}
```

```tsx
// web/src/components/settings/NotificationsToggle.tsx
import { useState } from 'react'
import { enablePush, disablePush, pushSupported } from '@/api/push'

export function NotificationsToggle() {
  const [state, setState] = useState<'idle' | 'subscribed' | 'denied'>('idle')
  const [busy, setBusy] = useState(false)

  if (!pushSupported()) {
    return <p className="text-sm text-muted-foreground">Push notifications are not supported in this browser (needs HTTPS + a modern browser).</p>
  }

  async function onEnable() {
    setBusy(true)
    try {
      const r = await enablePush()
      setState(r === 'subscribed' ? 'subscribed' : r === 'denied' ? 'denied' : 'idle')
    } finally { setBusy(false) }
  }
  async function onDisable() {
    setBusy(true)
    try { await disablePush(); setState('idle') } finally { setBusy(false) }
  }

  return (
    <div className="flex items-center justify-between gap-3">
      <div>
        <div className="text-sm font-medium">Phone notifications</div>
        {state === 'denied' && <div className="text-xs text-destructive">Permission denied — enable it in browser settings.</div>}
        {state === 'subscribed' && <div className="text-xs text-muted-foreground">This device will receive alerts.</div>}
      </div>
      {state === 'subscribed'
        ? <button disabled={busy} onClick={onDisable} className="text-sm underline">Disable</button>
        : <button disabled={busy} onClick={onEnable} className="text-sm underline">Enable</button>}
    </div>
  )
}
```

Render `<NotificationsToggle />` inside `web/src/components/settings/SettingsPanel.tsx` (add an import and place it in the panel body, following the panel's existing row layout).

(Confirm `api` in `web/src/api/client.ts` exposes `get`/`post`/`del` with these signatures; if the delete helper is named differently or takes the body differently, adjust `disablePush` and the test mock to match.)

- [ ] **Step 4: Run tests to verify they pass**

Run (from `web/`): `./node_modules/.bin/vitest run src/components/settings/__tests__/NotificationsToggle.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/api/push.ts web/src/components/settings/NotificationsToggle.tsx web/src/components/settings/SettingsPanel.tsx web/src/components/settings/__tests__/NotificationsToggle.test.tsx
git commit -m "feat(push): NotificationsToggle in settings + push api helpers"
```

---

## Final verification (run before opening the PR)

- [ ] Backend: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/ -q` — new push tests green; pre-existing auth/health/x11 failures on the deploy host are the known live-socket/Xvfb environmental artifacts (not regressions).
- [ ] Backend lint: `.venv/bin/python -m ruff check src/ server/` and `.venv/bin/python -m mypy server/sessions/push.py server/sessions/push_store.py server/api/push.py`.
- [ ] Web: `rm -f web/node_modules && (cd web && npm ci)`; then `cd web && ./node_modules/.bin/vitest run && ./node_modules/.bin/tsc -b --force && ./node_modules/.bin/eslint . --max-warnings 0`.
- [ ] Rollout doc note: enabling requires (1) `tailscale serve --bg --https=443 http://127.0.0.1:8787`, (2) `python -m server.sessions.push --keygen` → paste both keys into `/etc/ccswitch/ccswitch.env`, (3) restart `ccswitch ccswitch-daemon ccswitch-deploy-controller`, (4) open the app over HTTPS and toggle on.

## Self-Review (completed by plan author)

- **Spec coverage:** HTTPS prerequisite → rollout note + Task 7 verify; service worker → Task 7; VAPID → Task 1; subscription store → Tasks 2–3; API → Task 5; web-push sink + `notify()` consolidation → Task 4; orchestrator reuse (no second detection path) → Task 6; frontend toggle → Task 8; error handling (410 prune, no-op, fire-and-forget) → Task 4; testing gates → final verification; out-of-scope items (iOS, manifest, targeting) → not implemented, matching the spec. All spec sections map to a task.
- **Placeholder scan:** every code step contains complete code; the two "mirror the existing test setup" notes (Task 6 test, conftest patterns) point at concrete existing files to copy, not vague TODOs.
- **Type consistency:** `notify(title, body, *, url=None)`, `_webpush_sink(payload, *, sender=webpush)`, `upsert_subscription(db, *, endpoint, p256dh, auth, identity)`, `PushSubscription(endpoint, p256dh, auth, identity, created_at)`, and `/push/config` → `{vapidPublicKey, enabled}` are used identically across backend tasks and the frontend (`enablePush` reads `vapidPublicKey`/`enabled`; posts `sub.toJSON()` = `{endpoint, keys:{p256dh,auth}}` which `SubscribeBody` accepts).
