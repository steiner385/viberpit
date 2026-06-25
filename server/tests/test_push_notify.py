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
async def test_notify_posts_to_configured_webhook_not_deep_link(monkeypatch):
    # Regression: notify(url="/session/<id>") must treat the deep-link as
    # notification metadata, NOT as the webhook endpoint. Previously the
    # relative deep-link was forwarded as push()'s target and urlopen raised
    # ValueError("unknown url type: '/session/<id>'") on every notification.
    import json

    from server.sessions import push as push_mod
    import server.config as config

    monkeypatch.setattr(config, "PUSH_WEBHOOK_URL", "https://hooks.example/notify")
    monkeypatch.setattr(config, "VAPID_PRIVATE_KEY", None)  # silence webpush sink

    posted: dict = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=0):
        posted["url"] = req.full_url
        posted["body"] = json.loads(req.data.decode())
        return _Resp()

    monkeypatch.setattr(push_mod.urllib.request, "urlopen", fake_urlopen)

    await push_mod.notify("Needs you", "session X", url="/session/abc")

    # Posted to the configured webhook, not the relative deep-link.
    assert posted["url"] == "https://hooks.example/notify"
    # The deep-link rides along in the payload (consistent with the web-push sink).
    assert posted["body"] == {"title": "Needs you", "body": "session X", "url": "/session/abc"}


@pytest.mark.asyncio
async def test_notify_fans_out_to_both_sinks(monkeypatch):
    from server.sessions import push as push_mod
    calls = {"webhook": 0, "webpush": 0}

    async def fake_push(title, body, *, link=None, url=None, opener=None):
        calls["webhook"] += 1
        return True

    async def fake_webpush(payload, *, sender=None):
        calls["webpush"] += 1

    monkeypatch.setattr(push_mod, "push", fake_push)
    monkeypatch.setattr(push_mod, "_webpush_sink", fake_webpush)
    await push_mod.notify("t", "b", url="https://app/session/1")
    assert calls == {"webhook": 1, "webpush": 1}
