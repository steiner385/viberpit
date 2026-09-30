"""``SessionOrchestrator._decision_escalation_load``: dispatch-off products are
skipped, ``config["products"]`` narrows, and one product's failure is isolated."""
from __future__ import annotations

import asyncio
import logging

from server.tests.test_backlog_query_queue import (_init, _orch_with_ctx, _seed_item,
                                                   _seed_product)


async def _seed_two(tmp_path):
    await _init(tmp_path)
    await _seed_product("on-prod", "o/on", policy={"dispatch": "auto"})
    await _seed_product("off-prod", "o/off", policy={"dispatch": "off"})
    for repo in ("o/on", "o/off"):
        for n in (1, 2):
            await _seed_item(repo, n, hold_kind="owner_action")


def test_load_skips_dispatch_off_products_and_honours_the_products_filter(tmp_path):
    async def run():
        await _seed_two(tmp_path)
        orch = _orch_with_ctx()
        assert [e.slug for e in await orch._decision_escalation_load({})] == ["on-prod"]
        # the filter narrows further; it can never re-admit a dispatch-off product
        assert [e.slug for e in await orch._decision_escalation_load(
            {"products": ["on-prod"]})] == ["on-prod"]
        assert await orch._decision_escalation_load({"products": ["off-prod"]}) == []
    asyncio.run(run())


def test_one_products_load_failure_does_not_silence_the_others(tmp_path, monkeypatch, caplog):
    import server.pm.escalation as esc_mod
    real = esc_mod.product_escalation

    def flaky(slug, *a, **kw):
        if slug == "bad-prod":
            raise RuntimeError("bad rows")
        return real(slug, *a, **kw)
    monkeypatch.setattr(esc_mod, "product_escalation", flaky)

    async def run():
        await _init(tmp_path)
        await _seed_product("bad-prod", "o/bad")
        await _seed_product("good-prod", "o/good")
        for repo in ("o/bad", "o/good"):
            await _seed_item(repo, 1, hold_kind="owner_action")
        orch = _orch_with_ctx()
        with caplog.at_level(logging.WARNING):
            out = await orch._decision_escalation_load({})
        assert [e.slug for e in out] == ["good-prod"]
        assert "decision_escalation.product_load_failed slug=bad-prod" in caplog.text
    asyncio.run(run())
