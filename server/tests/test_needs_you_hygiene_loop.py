"""The needs-you hygiene loop (design 2026-10-14 §2): mode × product toggle × risk
tolerance decide dry run / apply / recommend; caps; supersede; failure backoff."""
from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from server.automations.context import AutomationContext
from server.automations.needs_you_hygiene import NeedsYouHygiene, NeedsYouHygieneLoop
from server.pm import hygiene as h
from server.pm.queue import QueueRow

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(hours=30)


def _row(number: int, **over: Any) -> QueueRow:
    base: dict[str, Any] = dict(
        ref=f"o/r#{number}", repo="o/r", number=number, product="p", title=f"Issue {number}",
        body="", url="u", status="Done", score=None, gh_created_at=NOW, gh_updated_at=NOW,
        assignees=(), author="tony", labels=(), open_children=0, open_blockers=(),
        live_session=False, claimed=False, work_item_id=f"wi{number}")
    base.update(over)
    return QueueRow(**base)


class _Ctx(AutomationContext):
    def __init__(self) -> None:
        super().__init__("needs-you-hygiene")
        self.events: list[tuple[str, str, dict]] = []

    async def emit(self, level, summary, **kw):  # type: ignore[override]
        self.events.append((level, summary, kw))


class _Harness:
    def __init__(self, products, *, mode="on", results=None) -> None:
        self.products = products
        self.mode = mode
        self.applied: list[h.HygieneAction] = []
        self.recommended: list[tuple[str, h.HygieneAction]] = []
        self.kept: list[tuple[str, set]] = []
        self.sampled = 0
        self.results = results or {}
        self.now = NOW
        self.ctx = _Ctx()
        self.loop = NeedsYouHygieneLoop(
            self.ctx, read_config=self.read_config, load=self.load,
            apply_action=self.apply, record_recommendation=self.recommend,
            supersede_recommendations=self.supersede, record_samples=self.samples,
            now=lambda: self.now)

    async def read_config(self):
        return self.mode, {}

    async def load(self):
        return self.products

    async def apply(self, action):
        self.applied.append(action)
        return self.results.get(action.ref, {"ok": True})

    async def recommend(self, project_id, action):
        new = (project_id, action.key) not in {(p, a.key) for p, a in self.recommended}
        self.recommended.append((project_id, action))
        return new

    async def supersede(self, project_id, keep):
        self.kept.append((project_id, set(keep)))
        return 0

    async def samples(self):
        self.sampled += 1


def _product(rows, *, risk="balanced", toggle="on", slug="p") -> h.ProductInputs:
    rows = [replace(r, product=slug) for r in rows]
    return h.ProductInputs(
        slug=slug, project_id=f"pid-{slug}",
        policy={"risk_tolerance": risk, "needs_you_automation": toggle},
        rows=rows, board_updated_at={r.work_item_id: OLD for r in rows}, stale_edges=[])


def _kinds(ctx):
    return [kw.get("kind") for _, _, kw in ctx.events]


@pytest.mark.asyncio
async def test_on_and_balanced_applies_and_emits():
    t = _Harness([_product([_row(1)])])
    await t.loop.tick()
    assert [a.ref for a in t.applied] == ["o/r#1"] and t.recommended == []
    assert _kinds(t.ctx) == ["hygiene_applied"]
    assert t.sampled == 1


@pytest.mark.asyncio
async def test_cautious_records_a_recommendation_once_and_supersedes_the_rest():
    t = _Harness([_product([_row(1)], risk="cautious")])
    await t.loop.tick()
    await t.loop.tick()
    assert t.applied == []
    assert [(p, a.ref) for p, a in t.recommended] == [("pid-p", "o/r#1")] * 2
    # Announced once, not every tick.
    assert _kinds(t.ctx) == ["hygiene_recommended"]
    assert t.kept[-1] == ("pid-p", {("done_but_open", "o/r#1", "close")})


@pytest.mark.asyncio
@pytest.mark.parametrize("auto,toggle", [("dry_run", "on"), ("on", "dry_run")])
async def test_dry_run_only_emits_what_it_would_do(auto, toggle):
    t = _Harness([_product([_row(1)], toggle=toggle)], mode=auto)
    await t.loop.tick()
    await t.loop.tick()
    assert t.applied == [] and t.recommended == [] and t.kept == []
    assert _kinds(t.ctx) == ["hygiene_dry_run"]        # once per finding
    assert "would close o/r#1" in t.ctx.events[0][1]


@pytest.mark.asyncio
async def test_product_toggle_off_leaves_it_alone_and_automation_off_does_nothing():
    t = _Harness([_product([_row(1)], toggle="off"), _product([_row(2)], slug="q")])
    await t.loop.tick()
    assert [a.ref for a in t.applied] == ["o/r#2"]
    off = _Harness([_product([_row(1)])], mode="off")
    await off.loop.tick()
    assert off.applied == [] and off.ctx.events == [] and off.sampled == 0


@pytest.mark.asyncio
async def test_caps_limit_writes_per_tick():
    t = _Harness([_product([_row(n) for n in range(1, 15)])])
    await t.loop.tick()
    assert len(t.applied) == h.ACTION_CAPS["close"] == 10


@pytest.mark.asyncio
async def test_a_refused_write_is_info_and_backs_off(caplog):
    t = _Harness([_product([_row(1)])],
                 results={"o/r#1": {"ok": False, "step": "conflict", "error": "nope"}})
    with caplog.at_level(logging.INFO, logger="server.automations.needs_you_hygiene"):
        await t.loop.tick()
        await t.loop.tick()
    assert len(t.applied) == 1                    # not retried on the next tick
    assert [lvl for lvl, _, _ in t.ctx.events] == ["info"]
    assert _kinds(t.ctx) == ["hygiene_failed"]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    t.now = NOW + timedelta(hours=25)
    await t.loop.tick()
    assert len(t.applied) == 2                    # retried after the backoff


@pytest.mark.asyncio
async def test_one_products_crash_does_not_stop_the_others(caplog):
    t = _Harness([_product([_row(1)]), _product([_row(2)], slug="q")])
    calls = []

    async def apply(action):
        calls.append(action.ref)
        if action.ref == "o/r#1":
            raise RuntimeError("boom")
        return {"ok": True}

    t.loop._apply_action = apply
    await t.loop.tick()
    assert calls == ["o/r#1", "o/r#2"]


@pytest.mark.asyncio
async def test_status_reads_the_published_counts():
    t = _Harness([_product([_row(1)]), _product([_row(2)], risk="cautious", slug="q")])
    await t.loop.tick()
    status = NeedsYouHygiene().status(t.ctx)
    assert status.counts == {"applied": 1, "recommended": 1, "dryRun": 0, "failed": 0}
    assert status.level == "active"
