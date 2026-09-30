"""Loop tests for decision-escalation (spec 2026-09-30 §5, decisions 3-5)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from server.automations.context import AutomationContext
from server.automations.decision_escalation import DecisionEscalationLoop
from server.pm.escalation import ProductEscalation
from server.tests.test_decision_escalation_policy import _item  # the HeldItem builder

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=3)


def _esc(slug, *, dispatchable=0, held=(), starving=True, oldest=None):
    return ProductEscalation(slug=slug, dispatchable=dispatchable, skipped={},
                             held=tuple(held), starving=starving, oldest_hold_hours=oldest)


class Fakes:
    def __init__(self, escs, *, mode="notify", quiet=(None, None, None), last=None):
        self.escs, self.mode, self.quiet = escs, mode, quiet
        self.last = dict(last or {})
        self.sent: list[tuple[str, str, dict]] = []
        self.recorded: list[tuple[str, dict]] = []
        self.now = NOW

    async def read_config(self):
        return self.mode, {"starving_max_dispatchable": 1, "hold_age_hours": 48,
                           "downstream_threshold": 3, "repeat_hours": 24, "products": []}

    async def load_escalations(self, config):
        return list(self.escs)

    async def quiet_window(self):
        return self.quiet

    async def last_digests(self):
        return dict(self.last)

    async def record_digest(self, slug, detail):
        self.recorded.append((slug, detail))
        self.last[slug] = (detail, self.now.replace(tzinfo=None))

    async def notify(self, title, body, *, slug, url):
        self.sent.append((title, body, {"slug": slug, "url": url}))

    def loop(self):
        return DecisionEscalationLoop(AutomationContext("decision-escalation"),
                                      read_config=self.read_config,
                                      load_escalations=self.load_escalations,
                                      quiet_window=self.quiet_window,
                                      last_digests=self.last_digests,
                                      record_digest=self.record_digest,
                                      notify=self.notify, now=lambda: self.now)


def test_one_digest_per_escalating_product_with_audit_row():
    f = Fakes([_esc("kindash", held=[_item("o/k#1")]),
               _esc("viberpit", dispatchable=9, held=[_item("o/r#1")], starving=False)])
    asyncio.run(f.loop().tick())
    assert [s[0] for s in f.sent] == ["kindash: 1 held (1 decision), 0 dispatchable"]
    assert f.sent[0][2]["url"] == "/roadmap?scope=inbox&product=kindash"
    (slug, detail), = f.recorded
    assert slug == "kindash" and detail["heldCount"] == 1 and detail["starving"] is True
    assert detail["reasons"] == ["starving"] and detail["topRefs"] == ["o/k#1"]
    assert set(detail) >= {"slug", "fingerprint", "heldCount", "dispatchable", "starving",
                           "reasons", "topRefs", "heldRefs", "maxDownstream"}
    assert detail["heldRefs"] == ["o/k#1"] and detail["maxDownstream"] == 0


def test_unchanged_situation_is_silent_until_repeat_hours():
    f = Fakes([_esc("kindash", held=[_item("o/k#1")])])
    asyncio.run(f.loop().tick())
    asyncio.run(f.loop().tick())
    assert len(f.sent) == 1, "same fingerprint inside repeat_hours: silent (survives a restart — the row is the memory)"
    f.now = NOW + timedelta(hours=25)
    asyncio.run(f.loop().tick())
    assert len(f.sent) == 2


def test_quiet_hours_defer_without_recording():
    f = Fakes([_esc("kindash", held=[_item("o/k#1")])], quiet=("00:00", "23:59", None))
    asyncio.run(f.loop().tick())
    assert f.sent == [] and f.recorded == []
    f.quiet = (None, None, None)
    asyncio.run(f.loop().tick())
    assert len(f.sent) == 1


def test_quiet_hours_localizes_against_stored_timezone():
    """#2562: a window that is quiet in UTC at NOW must NOT defer once a timezone
    many hours off UTC is stored, if NOW converted to that zone falls outside it."""
    # NOW is 2026-01-04T00:00:00Z. A window quiet only 00:00-00:01 UTC covers it in
    # UTC (no timezone stored) but +12h shifts local time to noon, well outside it.
    f = Fakes([_esc("kindash", held=[_item("o/k#1")])], quiet=("00:00", "00:01", None))
    asyncio.run(f.loop().tick())
    assert f.sent == [] and f.recorded == [], "no stored timezone: window applies to bare UTC now"

    f.quiet = ("00:00", "00:01", "Etc/GMT-12")  # Etc/GMT-12 is UTC+12
    asyncio.run(f.loop().tick())
    assert len(f.sent) == 1, "stored timezone: local now (noon) falls outside the quiet window"


def test_off_mode_does_nothing_and_status_reports_counts():
    from server.automations.decision_escalation import DecisionEscalation
    f = Fakes([_esc("kindash", held=[_item("o/k#1")])], mode="off")
    loop = f.loop()
    asyncio.run(loop.tick())
    assert f.sent == []
    f.mode = "notify"
    asyncio.run(loop.tick())
    status = DecisionEscalation().status(loop._ctx)
    assert status.level == "warn" and status.counts["held"] == 1
    assert status.counts["starving_products"] == 1
    assert status.products == [{"slug": "kindash", "dispatchable": 0, "held": 1,
                                "starving": True, "oldestHoldHours": None}]
    assert "1 product" in status.text and "1 held" in status.text and "1 starving" in status.text


def test_actionable_items_are_the_ranked_held_list_with_one_step_actions():
    f = Fakes([_esc("kindash", held=[_item("o/k#1", downstream=4),
                                     _item("o/k#2", kind="owner_action")])])
    loop = f.loop()
    asyncio.run(loop.tick())
    items = asyncio.run(loop.actionable_items())
    assert [i.id for i in items] == ["o/k#1", "o/k#2"]
    assert [a.kind for a in items[0].actions] == ["accept_recommended"]
    assert items[0].actions[0].label == "Accept recommendation"
    assert [a.kind for a in items[1].actions] == ["retry"]
    assert items[0].url == "/roadmap?scope=inbox&proposal=dp-1"
    assert "blocks 4" in (items[0].subtitle or "")


def test_one_products_failed_notify_does_not_abort_the_tick():
    """Per-product isolation: a notify that raises for the first product still
    lets the second product's digest send and still publishes the counts."""
    f = Fakes([_esc("kindash", held=[_item("o/k#1")]),
               _esc("viberpit", held=[_item("o/r#1")])])
    real_notify = f.notify

    async def flaky(title, body, *, slug, url):
        if slug == "kindash":
            raise RuntimeError("push backend down")
        await real_notify(title, body, slug=slug, url=url)
    f.notify = flaky
    loop = f.loop()
    asyncio.run(loop.tick())
    assert [r[0] for r in f.recorded] == ["viberpit"], "a failed notify records no digest"
    assert len(f.sent) == 1 and "viberpit" in f.sent[0][0]
    assert getattr(loop._ctx, "decision_escalation_counts")["held"] == 2


def test_accepting_one_of_five_is_silent_but_a_sixth_re_pages():
    five = [_item(f"o/k#{i}") for i in range(1, 6)]
    f = Fakes([_esc("kindash", held=five)])
    asyncio.run(f.loop().tick())
    assert len(f.sent) == 1
    f.escs = [_esc("kindash", held=five[1:])]  # the operator accepted #1
    asyncio.run(f.loop().tick())
    assert len(f.sent) == 1, "a shrink is the operator's own progress: no re-page"
    f.escs = [_esc("kindash", held=[*five[1:], _item("o/k#6")])]
    asyncio.run(f.loop().tick())
    assert len(f.sent) == 2
