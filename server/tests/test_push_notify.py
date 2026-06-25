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
