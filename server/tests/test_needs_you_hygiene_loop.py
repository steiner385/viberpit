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


# --- close-out assessment (design §3) ----------------------------------------------------

def _closeout(number: int, **over: Any) -> QueueRow:
    from server.automations.backlog_github import closeout_hold_reason
    return _row(number, status="Held", hold_kind="owner_action", hold_by="runner",
                hold_reason=closeout_hold_reason("the PR said `Refs`"), body=f"b{number}",
                **over)


def _assessor(decision="close"):
    from server.pm import closeout_assess as ca
    from server.pm.outcomes import body_fingerprint
    calls: list[str] = []

    async def assess(row):
        calls.append(row.ref)
        d = None if decision is None else ca.Decision(decision, "Left for you:\n- [ ] x"
                                                       if decision == "checklist" else "t")
        return ca.assessment_state(fingerprint=body_fingerprint(row.title, row.body),
                                   decision=d, summary="s", at="now")
    return assess, calls


@pytest.mark.asyncio
async def test_assessments_are_capped_fleet_wide_and_acted_on_the_same_tick():
    rows_a = [_closeout(n) for n in (1, 2)]
    rows_b = [_closeout(n) for n in (3, 4)]
    t = _Harness([_product(rows_a, slug="a"), _product(rows_b, slug="b")])
    assess, calls = _assessor("close")
    t.loop._assess = assess
    await t.loop.tick()
    assert len(calls) == h.ASSESSMENTS_PER_TICK == 3
    assert [(a.rule, a.ref, a.action) for a in t.applied] == [
        ("closeout_assess", r, "close") for r in calls]
    assert _kinds(t.ctx).count("hygiene_assessed") == 3


@pytest.mark.asyncio
async def test_cautious_recommends_the_assessed_outcome_and_dry_run_only_reports_it():
    assess, _ = _assessor("checklist")
    t = _Harness([_product([_closeout(1)], risk="cautious")])
    t.loop._assess = assess
    await t.loop.tick()
    assert t.applied == []
    [(pid, a)] = t.recommended
    assert (pid, a.rule, a.action) == ("pid-p", "closeout_assess", "rewrite_hold")
    assert a.detail["reason"].startswith("Left for you:")

    d = _Harness([_product([_closeout(1)])], mode="dry_run")
    d.loop._assess = assess
    await d.loop.tick()
    assert d.applied == [] and d.recommended == []
    assert _kinds(d.ctx) == ["hygiene_assessed", "hygiene_dry_run"]


@pytest.mark.asyncio
async def test_an_unusable_or_failed_assessment_acts_on_nothing(caplog):
    assess, calls = _assessor(None)
    t = _Harness([_product([_closeout(1)])])
    t.loop._assess = assess
    await t.loop.tick()
    assert calls == ["o/r#1"] and t.applied == []

    async def unavailable(row):
        return None
    u = _Harness([_product([_closeout(1)])])
    u.loop._assess = unavailable
    await u.loop.tick()
    assert u.applied == [] and "hygiene_assessed" not in _kinds(u.ctx)

    async def boom(row):
        raise RuntimeError("x")
    b = _Harness([_product([_closeout(1)])])
    b.loop._assess = boom
    with caplog.at_level(logging.WARNING):
        await b.loop.tick()
    assert b.applied == [] and "assess_crashed" in caplog.text


@pytest.mark.asyncio
async def test_a_stored_assessment_is_not_re_asked():
    from server.pm.outcomes import body_fingerprint
    row = _closeout(1)
    stored = {"fingerprint": body_fingerprint(row.title, row.body), "decision": None,
              "text": "", "summary": "", "at": "x", "appliedAt": None}
    p = replace(_product([row]), closeout_states={row.ref: stored})
    assess, calls = _assessor("close")
    t = _Harness([p])
    t.loop._assess = assess
    await t.loop.tick()
    assert calls == [] and t.applied == []


# --- parked review (design §4) -------------------------------------------------------------

def _parked(number: int, **over: Any) -> QueueRow:
    return _row(number, status="Backlog", labels=("parked",), body=f"b{number}",
                gh_created_at=NOW - timedelta(days=200 - number), **over)


def _reviewer(verdict: str | None = "unpark"):
    from server.pm import parked_review as pr
    from server.pm.outcomes import body_fingerprint
    calls: list[str] = []

    async def review(row):
        calls.append(row.ref)
        v = pr.Verdict(verdict, "x", "still wanted") if verdict else None
        return pr.review_state(fingerprint=body_fingerprint(row.title, row.body), verdict=v,
                               prs=[], repo=row.repo, at=NOW.isoformat())
    return review, calls


def _with_parked_review(t: _Harness, review) -> list[tuple[str, dict]]:
    runs: list[tuple[str, dict]] = []

    async def record(slug, run):
        runs.append((slug, dict(run)))
    t.loop._review_parked = review
    t.loop._record_parked_run = record
    return runs


@pytest.mark.asyncio
async def test_a_due_run_reviews_parked_issues_across_ticks_and_only_recommends():
    """Balanced risk APPLIES the other rules, yet a parked review is only ever a
    recommendation (design §4: no automatic unpark, ever)."""
    from server.pm import parked_review as pr
    rows = [_parked(n) for n in range(1, 8)]
    t = _Harness([_product(rows, risk="bold")])
    review, calls = _reviewer("unpark")
    runs = _with_parked_review(t, review)
    await t.loop.tick()
    assert calls == [f"o/r#{n}" for n in range(1, pr.REVIEWS_PER_TICK + 1)]
    assert t.applied == []
    assert {a.ref for _, a in t.recommended} == set(calls)
    assert {a.rule for _, a in t.recommended} == {"parked_review"}
    assert t.kept[-1][1] == {("parked_review", r, "unpark") for r in calls}
    slug, run = runs[-1]
    assert slug == "p" and run["pending"] == ["o/r#6", "o/r#7"] and run["startedAt"]
    assert "hygiene_parked_review" in _kinds(t.ctx)


@pytest.mark.asyncio
async def test_a_pending_run_continues_and_a_finished_run_waits_a_week():
    from server.pm import parked_review as pr
    rows = [_parked(n) for n in (1, 2, 3)]
    started = (NOW - timedelta(days=2)).isoformat()
    review, calls = _reviewer("keep")
    p = replace(_product(rows), parked_run={"startedAt": started, "pending": ["o/r#2", "o/r#9"]})
    t = _Harness([p])
    runs = _with_parked_review(t, review)
    await t.loop.tick()
    # o/r#9 left the parked set: dropped; o/r#2 reviewed; no new run (not due).
    assert calls == ["o/r#2"] and runs[-1] == ("p", {"startedAt": started, "pending": []})
    assert t.recommended == []   # keep -> nothing

    idle = _Harness([replace(_product(rows), parked_run={"startedAt": started, "pending": []})])
    review2, calls2 = _reviewer()
    runs2 = _with_parked_review(idle, review2)
    await idle.loop.tick()
    assert calls2 == [] and runs2 == []
    assert pr.REVIEW_INTERVAL == timedelta(days=7)


@pytest.mark.asyncio
async def test_over_the_threshold_a_run_is_due_daily():
    rows = [_parked(n) for n in (1, 2, 3)]
    started = (NOW - timedelta(days=1, minutes=1)).isoformat()
    base = _product(rows)
    p = replace(base, policy={**base.policy, "parked_review_threshold": 2},
                parked_run={"startedAt": started, "pending": []})
    t = _Harness([p])
    review, calls = _reviewer("keep")
    _with_parked_review(t, review)
    await t.loop.tick()
    assert calls == ["o/r#1", "o/r#2", "o/r#3"]


@pytest.mark.asyncio
async def test_no_helper_leaves_the_run_pending_and_dry_run_still_reviews():
    rows = [_parked(1)]

    async def unavailable(row):
        return None
    t = _Harness([_product(rows)])
    runs = _with_parked_review(t, unavailable)
    await t.loop.tick()
    assert runs[-1][1]["pending"] == ["o/r#1"] and t.recommended == []

    d = _Harness([_product(rows)], mode="dry_run")
    review, calls = _reviewer("unpark")
    _with_parked_review(d, review)
    await d.loop.tick()
    assert calls == ["o/r#1"] and d.recommended == []
    assert _kinds(d.ctx)[-1] == "hygiene_dry_run" and "would unpark o/r#1" in d.ctx.events[-1][1]


# --- daily owner digest push (design §5) -------------------------------------------------------

class _Digest:
    def __init__(self, view, *, quiet=False, last=None) -> None:
        self.view = view
        self.quiet = quiet
        self.last = dict(last or {})
        self.sent: list[tuple[str, str, str, str]] = []
        self.recorded: list[tuple[str, dict]] = []

    async def in_quiet_hours(self, now):
        return self.quiet

    async def load(self):
        return self.view

    async def last_sent(self):
        return self.last

    async def notify(self, title, body, *, slug, url):
        self.sent.append((title, body, slug, url))

    async def record_sent(self, slug, detail):
        self.recorded.append((slug, detail))
        self.last[slug] = NOW


def _view(**items_by_slug):
    return {"products": [{"slug": s, "items": items} for s, items in items_by_slug.items()]}


_ITEMS = [{"id": "hold:wi1", "recommendation": {"action": "approve_escalation"}},
          {"id": "gate:x", "recommendation": None}]


@pytest.mark.asyncio
async def test_the_digest_push_sends_one_summary_per_product_once_a_day():
    t = _Harness([_product([], slug="p"), _product([], slug="q")])
    digest = _Digest(_view(p=_ITEMS, q=[]))
    t.loop._digest = digest
    await t.loop.tick()
    await t.loop.tick()
    assert digest.sent == [("p: today's digest", "2 items need you — 1 has a recommendation",
                            "p", "/roadmap/p?tab=needs-you")]
    assert digest.recorded[0][1]["total"] == 2
    assert "hygiene_digest_sent" in _kinds(t.ctx)
    t.now = NOW + timedelta(hours=24)
    await t.loop.tick()
    assert len(digest.sent) == 2


@pytest.mark.asyncio
async def test_the_digest_push_skips_quiet_hours_and_products_with_hygiene_off():
    t = _Harness([_product([], slug="p")])
    quiet = _Digest(_view(p=_ITEMS), quiet=True)
    t.loop._digest = quiet
    await t.loop.tick()
    assert quiet.sent == [] and quiet.recorded == []

    off = _Harness([_product([], slug="p", toggle="off")])
    digest = _Digest(_view(p=_ITEMS))
    off.loop._digest = digest
    await off.loop.tick()
    assert digest.sent == []


@pytest.mark.asyncio
async def test_a_failed_push_records_nothing_and_retries(caplog):
    t = _Harness([_product([], slug="p")])
    digest = _Digest(_view(p=_ITEMS))

    async def boom(*a, **k):
        raise RuntimeError("push down")
    digest.notify = boom  # type: ignore[method-assign]
    t.loop._digest = digest
    with caplog.at_level(logging.WARNING):
        await t.loop.tick()
    assert digest.recorded == [] and "digest_send_failed" in caplog.text


# --- owner_evidence reminders (#3113) ----------------------------------------------------------

def _stub_stamp(monkeypatch) -> list:
    """The reminder's narrowed ``lastRemindedAt`` write, recorded instead of run (the
    harness has no database; ``holds.stamp_evidence_reminded`` has its own tests)."""
    import contextlib

    import server.db
    from server.pm import holds
    stamped: list = []

    class _Db:
        async def run_sync(self, fn):
            return fn(None)

    @contextlib.asynccontextmanager
    async def fake_session():
        yield _Db()
    monkeypatch.setattr(server.db, "db_session", fake_session)
    def stamp(_s, ref, *, hold_set_at, now):
        stamped.append((ref, now))
        return True
    monkeypatch.setattr(holds, "stamp_evidence_reminded", stamp)
    return stamped


def _ask(number: int, **over: Any) -> QueueRow:
    base = dict(hold_kind="owner_evidence", hold_by="runner", url=f"https://x/{number}",
                hold_set_at=NOW - timedelta(days=4), hold_reason="needs owner record",
                hold_detail={"request": "Record the DPA acceptance dates"})
    base.update(over)
    return _row(number, **base)


@pytest.mark.asyncio
async def test_a_due_evidence_ask_is_reminded_once_and_stamped(monkeypatch):
    stamped = _stub_stamp(monkeypatch)
    t = _Harness([_product([_ask(1), _ask(2, hold_set_at=NOW - timedelta(hours=1))])])
    digest = _Digest(_view(p=[]))
    t.loop._digest = digest
    await t.loop.tick()
    assert digest.sent == [("p: evidence needed on o/r#1",
                            "Record the DPA acceptance dates. Post it as a comment on this issue.",
                            "p", "https://x/1")]
    assert stamped == [("o/r#1", NOW)]
    assert "hygiene_evidence_reminded" in _kinds(t.ctx)


@pytest.mark.asyncio
async def test_evidence_reminders_wait_out_quiet_hours_and_respect_the_product_switch(monkeypatch):
    stamped = _stub_stamp(monkeypatch)
    quiet = _Harness([_product([_ask(1)])])
    quiet.loop._digest = _Digest(_view(p=[]), quiet=True)
    await quiet.loop.tick()
    off = _Harness([_product([_ask(1)], toggle="off")])
    off.loop._digest = _Digest(_view(p=[]))
    await off.loop.tick()
    dry = _Harness([_product([_ask(1)])], mode="dry_run")
    dry.loop._digest = digest = _Digest(_view(p=[]))
    await dry.loop.tick()
    assert stamped == [] and digest.sent == []
    assert any("would remind — o/r#1" in e[1] for e in dry.ctx.events)
