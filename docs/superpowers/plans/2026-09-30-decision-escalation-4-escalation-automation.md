# Decision escalation PR 4 — the `decision-escalation` automation and the `decision_needed` event class — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The operator learns promptly, through the existing notification rules, when a product's decisions are starving dispatch, ageing, or blocking a lot of work — one digest per product, never one ping per item, never during quiet hours — and can act on the ranked list from the automation's Needs-you tab.

**Architecture:** A new automation (`server/automations/decision_escalation.py`) in the exact shape of `DeployMonitor`: a class for the registry, a `Loop` with a locked `tick()`, and an `ActionProvider`. The tick reads the PR 2 read model (`server/pm/escalation.py`) over the queue rows, decides per product whether to escalate (pure `decide()`), dedupes on a fingerprint stored in the latest `pm.escalation.digest` audit row, defers while `in_quiet_hours`, and emits `notify_event("decision_needed", …)`. Orchestrator wiring copies the deploy-monitor lines verbatim; the wiring test is a clone of `test_deploy_monitor_wiring.py`.

**Tech Stack:** Python 3 / asyncio / SQLAlchemy async; pytest with injected clocks and fake seams (the `test_deploy_monitor_loop.py` mold).

**Spec:** `docs/superpowers/specs/2026-09-30-decision-escalation-design.md` §5 (and §2 decisions 3–5, 7), §10 PR 4.

**Depends on:** PR 2 merged (`server/pm/escalation.py`, `GET /api/pm/holds`). Verify on the branch base: `ls server/pm/escalation.py`.

## Global Constraints

- Branch off `origin/master` (`git fetch && git checkout -b feat/decision-escalation-automation origin/master`).
- Test env and gates as in plan 1 (`env -u …` + `/home/tony/GitHub/viberpit/.venv/bin/python`; ruff, mypy, `pytest tests/`, touched files, the full `-m "not slow"` server run; commit/PR text via files; trailers).
- Automation id `decision-escalation`, name "Decision Escalation", `work_class = "maintenance"`, `modes = ("off", "notify")`, `engine_policy = EnginePolicy.CLAUDE_ONLY`. Config keys and defaults exactly: `tick_min` 30, `starving_max_dispatchable` 1, `hold_age_hours` 48, `downstream_threshold` 3, `repeat_hours` 24, `products` (string-list, empty = every product with dispatch on). `ConfigField.type` must be one of `bool | int | string | enum | repo-multiselect | string-list | repo-number-map`.
- Event class literal `"decision_needed"` (double-quoted, inside 200 chars after `notify_event(`), `fanout_when_unconfigured=True`, `automation="decision-escalation"`, `url="/roadmap?scope=inbox&product=<slug>"`. Watch `CONDITION_MAP["product"]["needs_decision"] == ("decision_needed", "scope_automation")` (the spec wrote `"automation"`; the map's second element is the RULE SCOPE FIELD name, so `scope_automation` is the correct value — ruling recorded here).
- Quiet hours: the tick returns before evaluating when `in_quiet_hours(start, end, now)` (signature: two `"HH:MM"` strings + `now`; read `AppSettings.quiet_hours_start/end` the way `push.py:188-191` does). Nothing is stored; the next tick outside the window re-evaluates from live state.
- Dedup state = the latest `AuditEvent` with `type == "pm.escalation.digest"` and `detail.slug == <slug>`; write one per sent digest via `record_audit(db, "pm.escalation.digest", SYSTEM_IDENTITY, detail, via="automation")`. `AuditEvent.at` is naive UTC (`datetime.utcnow()` default) — compare with a naive UTC `now` for the `repeat_hours` test.
- Every rule is pure and injected-clock tested; no absolute date literals; no `func.now()`.
- Never resolve a decision automatically. The provider's triggers call the existing PM routes' service functions with the person-class actor gate intact.
- Orchestrator strings must match the wiring test EXACTLY (quote style, `getattr(self, "_decision_escalation_ctx", None)` form).
- Web label `{ value: 'decision_needed', label: 'Decision needed' }` AND the missing `{ value: 'account_limited', label: 'Account limited' }` in `notificationsApi.ts`.
- Registry-enumerating tests must gain a row: `test_work_class.py` (`"decision-escalation": "maintenance"`), `test_automation_engine_policy.py` (`"decision-escalation": EnginePolicy.CLAUDE_ONLY`).

## Review Focus

1. A product with dispatch **off** never escalates, however many holds it carries — an off product is not waiting on anyone. Pinned in Task 1.
2. The **same** fingerprint within `repeat_hours` sends nothing, even across a daemon restart — the audit row is the memory, not process state. Pinned in Task 2.
3. A **new** proposal (fingerprint change) on a product that is not starving/aged/high-downstream still sends nothing — the triggers, not the change, decide loudness; the inbox badge already shows the new item. Pinned in Task 1.
4. A tick inside quiet hours writes **no** audit row and sends nothing; the next tick outside sends (no "already handled" leak). Pinned in Task 2.
5. The provider's `accept_recommended` trigger must go through `decisions.accept` with `may_resolve` enforced for a system/agent actor — i.e. the trigger runs as the OPERATOR who tapped it (the API layer's identity), never as `runner`. Because `POST /api/automations/{id}/trigger` carries no actor into the provider today, the provider must call the service with the identity the route resolves — Task 3 threads it; pinned there.

---

### Task 1: Pure decision rule and digest text — `server/automations/decision_escalation.py` (part 1)

**Files:**
- Create: `server/automations/decision_escalation.py` (pure half: `Thresholds`, `decide`, `fingerprint`, `digest_text`)
- Test: `server/tests/test_decision_escalation_policy.py` (new)

**Interfaces:**
- Consumes: `server.pm.escalation.ProductEscalation`, `HeldItem` (PR 2).
- Produces:

```python
@dataclass(frozen=True)
class Thresholds:
    starving_max: int = 1; hold_age_hours: float = 48.0
    downstream_threshold: int = 3; repeat_hours: float = 24.0

@dataclass(frozen=True)
class Verdict:
    slug: str; escalate: bool; reasons: tuple[str, ...]   # subset of ("starving", "aged", "downstream")
    fingerprint: str

def decide(esc: ProductEscalation, t: Thresholds) -> Verdict
def fingerprint(esc: ProductEscalation) -> str          # sha256 hex of (slug, starving, sorted held refs, max downstream)
def should_send(verdict: Verdict, last: tuple[str, datetime] | None, *, now: datetime, t: Thresholds) -> bool
def digest_text(esc: ProductEscalation, verdict: Verdict, *, top: int = 3) -> tuple[str, str]  # (title, body)
```

- [ ] **Step 1: Write the failing tests**

```python
"""Pure tests for the decision-escalation rule (spec 2026-09-30 §5)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from server.automations.decision_escalation import (Thresholds, decide, digest_text,
                                                    fingerprint, should_send)
from server.pm.escalation import HeldItem, ProductEscalation

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=3)
T = Thresholds()


def _item(ref, *, kind="decision", downstream=0, age=1.0, recommended="b"):
    return HeldItem(ref=ref, repo="o/k", number=int(ref.rsplit("#", 1)[1]), product="kindash",
                    title=f"Issue {ref}", url=f"https://x/{ref}", hold_kind=kind,
                    hold_reason="why", hold_by="session:s1", hold_set_at=None, age_hours=age,
                    proposal_id="dp-1" if kind == "decision" else None, options=(),
                    recommended_key=recommended if kind == "decision" else None,
                    downstream=downstream, downstream_dispatchable=downstream,
                    downstream_refs=(), dispatchable_on_release=True, released_by="x")


def _esc(*, dispatchable=5, held=(), starving=False, oldest=None):
    return ProductEscalation(slug="kindash", dispatchable=dispatchable,
                             skipped={"held": len(held)}, held=tuple(held),
                             starving=starving, oldest_hold_hours=oldest)


def test_decide_truth_table():
    quiet = decide(_esc(held=[_item("o/k#1")]), T)
    assert quiet.escalate is False and quiet.reasons == ()
    assert decide(_esc(dispatchable=0, held=[_item("o/k#1")], starving=True), T).reasons == ("starving",)
    assert decide(_esc(held=[_item("o/k#1", age=49.0)], oldest=49.0), T).reasons == ("aged",)
    assert decide(_esc(held=[_item("o/k#1", downstream=3)]), T).reasons == ("downstream",)
    both = decide(_esc(dispatchable=0, held=[_item("o/k#1", downstream=9, age=100)],
                       starving=True, oldest=100.0), T)
    assert both.escalate and both.reasons == ("starving", "aged", "downstream")
    # nothing held: never, whatever the queue looks like
    assert decide(_esc(dispatchable=0), T).escalate is False
    # a new proposal on a healthy product changes the fingerprint but not the verdict
    assert decide(_esc(held=[_item("o/k#1"), _item("o/k#2")]), T).escalate is False


def test_fingerprint_changes_with_held_set_starvation_or_impact_only():
    a = fingerprint(_esc(held=[_item("o/k#1")]))
    assert a == fingerprint(_esc(held=[_item("o/k#1", age=99.0)])), "age alone is not a change"
    assert a != fingerprint(_esc(held=[_item("o/k#1"), _item("o/k#2")]))
    assert a != fingerprint(_esc(held=[_item("o/k#1")], starving=True, dispatchable=0))
    assert a != fingerprint(_esc(held=[_item("o/k#1", downstream=4)]))
    assert fingerprint(_esc(held=[_item("o/k#2"), _item("o/k#1")])) == \
        fingerprint(_esc(held=[_item("o/k#1"), _item("o/k#2")]))


def test_should_send_dedupes_on_fingerprint_and_repeats_after_repeat_hours():
    v = decide(_esc(dispatchable=0, held=[_item("o/k#1")], starving=True), T)
    assert should_send(v, None, now=NOW, t=T) is True
    same = (v.fingerprint, NOW - timedelta(hours=2))
    assert should_send(v, same, now=NOW, t=T) is False
    old = (v.fingerprint, NOW - timedelta(hours=25))
    assert should_send(v, old, now=NOW, t=T) is True
    other = ("different", NOW - timedelta(minutes=1))
    assert should_send(v, other, now=NOW, t=T) is True
    calm = decide(_esc(held=[_item("o/k#1")]), T)
    assert should_send(calm, None, now=NOW, t=T) is False, "no verdict, no send"


def test_digest_text_names_counts_and_the_top_three_loudest():
    held = [_item(f"o/k#{i}", downstream=10 - i, age=float(i)) for i in range(1, 6)]
    held[4] = _item("o/k#5", kind="owner_action", downstream=0, age=200.0)
    esc = _esc(dispatchable=3, held=held, starving=False, oldest=200.0)
    v = decide(esc, T)
    title, body = digest_text(esc, v)
    assert title == "kindash: 5 held (4 decisions), 3 dispatchable"
    lines = body.splitlines()
    assert lines[0] == "#1 Issue o/k#1 — blocks 9 (recommended: b)"
    assert lines[1].startswith("#2 Issue o/k#2 — blocks 8")
    assert lines[2].startswith("#3 Issue o/k#3 — blocks 7")
    assert lines[3] == "2 more · oldest 8d · why: aged, downstream"
```

- [ ] **Step 2: Run** — Expected: FAIL `ModuleNotFoundError`.

- [ ] **Step 3: Implement (pure half)**

```python
"""Decision Escalation (spec 2026-09-30 §5): one digest per product when its
decisions are starving dispatch, ageing, or blocking a lot of work.

Pure half first — ``decide``/``fingerprint``/``should_send``/``digest_text`` take
the read model (``server/pm/escalation.py``) and answer without I/O — then the
automation class, the loop and the action provider, in the ``DeployMonitor``
mould (``server/automations/deploy_monitor.py``).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from server.pm.escalation import HeldItem, ProductEscalation

AUTOMATION_ID = "decision-escalation"
EVENT_CLASS = "decision_needed"
#: The dedup record: one audit row per digest sent, read back on the next tick.
DIGEST_AUDIT_TYPE = "pm.escalation.digest"
REASONS = ("starving", "aged", "downstream")


@dataclass(frozen=True)
class Thresholds:
    starving_max: int = 1
    hold_age_hours: float = 48.0
    downstream_threshold: int = 3
    repeat_hours: float = 24.0


@dataclass(frozen=True)
class Verdict:
    slug: str
    escalate: bool
    reasons: tuple[str, ...]
    fingerprint: str


def fingerprint(esc: ProductEscalation) -> str:
    """What "the situation changed" means: the held SET, starvation, and the
    loudest item's reach. Age is deliberately not in it — time passing is what
    ``repeat_hours`` handles, not a new situation."""
    payload = {"slug": esc.slug, "starving": esc.starving,
               "held": sorted(i.ref for i in esc.held),
               "max_downstream": max((i.downstream for i in esc.held), default=0)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def decide(esc: ProductEscalation, t: Thresholds) -> Verdict:
    reasons: list[str] = []
    if esc.held:
        if esc.starving:
            reasons.append("starving")
        if esc.oldest_hold_hours is not None and esc.oldest_hold_hours >= t.hold_age_hours:
            reasons.append("aged")
        if any(i.downstream >= t.downstream_threshold for i in esc.held):
            reasons.append("downstream")
    return Verdict(slug=esc.slug, escalate=bool(reasons), reasons=tuple(reasons),
                   fingerprint=fingerprint(esc))


def should_send(verdict: Verdict, last: tuple[str, datetime] | None, *, now: datetime,
                t: Thresholds) -> bool:
    """Send when there is a verdict AND (nothing sent before, the situation changed,
    or the last digest is older than ``repeat_hours``)."""
    if not verdict.escalate:
        return False
    if last is None:
        return True
    last_fp, last_at = last
    if last_fp != verdict.fingerprint:
        return True
    return now - last_at >= timedelta(hours=t.repeat_hours)


def _age_label(hours: float | None) -> str:
    if hours is None:
        return "?"
    return f"{int(hours // 24)}d" if hours >= 48 else f"{int(hours)}h"


def digest_text(esc: ProductEscalation, verdict: Verdict, *, top: int = 3) -> tuple[str, str]:
    decisions = sum(1 for i in esc.held if i.hold_kind == "decision")
    title = (f"{esc.slug}: {len(esc.held)} held ({decisions} decision"
             f"{'' if decisions == 1 else 's'}), {esc.dispatchable} dispatchable")
    lines: list[str] = []
    for n, item in enumerate(esc.held[:top], start=1):
        rec = f" (recommended: {item.recommended_key})" if item.recommended_key else ""
        lines.append(f"#{n} {item.title} — blocks {item.downstream}{rec}")
    rest = len(esc.held) - min(top, len(esc.held))
    tail = [f"{rest} more"] if rest else []
    tail.append(f"oldest {_age_label(esc.oldest_hold_hours)}")
    tail.append("why: " + ", ".join(verdict.reasons))
    lines.append(" · ".join(tail))
    return title, "\n".join(lines)
```

(`_age_label(200.0)` → `"8d"`; the test expects exactly that.)

- [ ] **Step 4: Run** — Expected: PASS. **Step 5: Commit** — `feat(automations): decision-escalation rule — decide, fingerprint, digest text`.

---

### Task 2: The loop, dedup via audit, quiet-hours deferral — `decision_escalation.py` (part 2)

**Files:**
- Modify: `server/automations/decision_escalation.py` (add `DecisionEscalation` class, `DecisionEscalationLoop`)
- Test: `server/tests/test_decision_escalation_loop.py` (new)

**Interfaces:**
- Produces:

```python
class DecisionEscalation:  # registry object; id/name/work_class/description/modes/engine_policy/config_schema/status/on_state_change
class DecisionEscalationLoop:
    def __init__(self, ctx, *, read_config, load_escalations, quiet_window, last_digest,
                 record_digest, notify, now=None)
    async def tick(self) -> None
    async def actionable_items(self, limit: int = 50) -> list[ActionableItem]
```
Seams (all async callables, injected so the tests use fakes and the orchestrator wires the real ones):
- `read_config() -> (mode, config)`
- `load_escalations(config) -> list[ProductEscalation]` (the orchestrator's implementation: `db.run_sync` of `runner_claims` + `load_queue_rows` + `policies_by_slug`, then `decisions.pending_list`, then `product_escalation` per product with dispatch on, filtered by `config["products"]` when non-empty; `starving_max` from config)
- `quiet_window() -> (start, end)` (the two `AppSettings` strings)
- `last_digest(slug) -> (fingerprint, at) | None` (latest `pm.escalation.digest` audit row for the slug)
- `record_digest(slug, detail) -> None`
- `notify(title, body, *, repo, url) -> None` (wraps `push.notify_event("decision_needed", …)`)

- [ ] **Step 1: Write the failing tests** (mold: `server/tests/test_deploy_monitor_loop.py` — read it for the fake-seam idiom)

```python
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
    def __init__(self, escs, *, mode="notify", quiet=(None, None), last=None):
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

    async def last_digest(self, slug):
        return self.last.get(slug)

    async def record_digest(self, slug, detail):
        self.recorded.append((slug, detail))
        self.last[slug] = (detail["fingerprint"], self.now.replace(tzinfo=None))

    async def notify(self, title, body, *, repo, url):
        self.sent.append((title, body, {"repo": repo, "url": url}))

    def loop(self):
        return DecisionEscalationLoop(AutomationContext("decision-escalation"),
                                      read_config=self.read_config,
                                      load_escalations=self.load_escalations,
                                      quiet_window=self.quiet_window,
                                      last_digest=self.last_digest,
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
                           "reasons", "topRefs"}


def test_unchanged_situation_is_silent_until_repeat_hours():
    f = Fakes([_esc("kindash", held=[_item("o/k#1")])])
    asyncio.run(f.loop().tick())
    asyncio.run(f.loop().tick())
    assert len(f.sent) == 1, "same fingerprint inside repeat_hours: silent (survives a restart — the row is the memory)"
    f.now = NOW + timedelta(hours=25)
    asyncio.run(f.loop().tick())
    assert len(f.sent) == 2


def test_quiet_hours_defer_without_recording():
    f = Fakes([_esc("kindash", held=[_item("o/k#1")])], quiet=("00:00", "23:59"))
    asyncio.run(f.loop().tick())
    assert f.sent == [] and f.recorded == []
    f.quiet = (None, None)
    asyncio.run(f.loop().tick())
    assert len(f.sent) == 1


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
```

- [ ] **Step 2: Run** — Expected: FAIL (`DecisionEscalationLoop` missing).

- [ ] **Step 3: Implement (loop half)** — append to the module:

```python
from server.automations.base import ActionableItem, ActionSpec, AutomationStatus, ConfigField
from server.automations.context import observation_failed, publish_observation
from server.automations.engine_policy import EnginePolicy
from server.sessions.notify_rules import in_quiet_hours


class DecisionEscalation:
    id = AUTOMATION_ID
    name = "Decision Escalation"
    work_class = "maintenance"
    description = ("Watches every product's held items. When decisions are starving the "
                   "runner, ageing, or blocking a lot of work, sends one digest per product "
                   "through the notification rules — never a ping per item, never in quiet "
                   "hours. Never resolves a decision itself.")
    modes: tuple[str, ...] = ("off", "notify")
    engine_policy: EnginePolicy = EnginePolicy.CLAUDE_ONLY
    config_schema = [
        ConfigField(key="tick_min", label="Check every (minutes)", type="int", default=30, min=5),
        ConfigField(key="starving_max_dispatchable", label="Starving at or below (dispatchable)",
                    type="int", default=1, min=0,
                    help="A product with this many or fewer dispatchable items while anything "
                         "is held is starving."),
        ConfigField(key="hold_age_hours", label="Escalate a hold older than (hours)", type="int",
                    default=48, min=1),
        ConfigField(key="downstream_threshold", label="Escalate when a hold blocks at least",
                    type="int", default=3, min=1),
        ConfigField(key="repeat_hours", label="Repeat an unchanged digest after (hours)",
                    type="int", default=24, min=1),
        ConfigField(key="products", label="Products", type="string-list", default=[],
                    help="Limit to these product slugs (empty = every product with dispatch on)."),
    ]

    def status(self, ctx: Any) -> AutomationStatus:
        counts = getattr(ctx, "decision_escalation_counts", None) if ctx is not None else None
        if not counts:
            return AutomationStatus(text="idle", level="idle")
        products = list(getattr(ctx, "decision_escalation_products", None) or [])
        held, starving = int(counts.get("held", 0)), int(counts.get("starving_products", 0))
        n = len(products)
        text = (f"{n} product{'' if n == 1 else 's'} · {held} held · "
                f"{starving} starving")
        level = "warn" if starving else ("active" if held else "idle")
        return AutomationStatus(text=text, level=level, counts=dict(counts), products=products)

    async def on_state_change(self, *, enabled: bool, mode: str | None, config: dict,
                              ctx: Any) -> None:
        ...  # the loop reads mode/config each tick


def thresholds_from(config: dict) -> Thresholds:
    return Thresholds(starving_max=int(config.get("starving_max_dispatchable", 1)),
                      hold_age_hours=float(config.get("hold_age_hours", 48)),
                      downstream_threshold=int(config.get("downstream_threshold", 3)),
                      repeat_hours=float(config.get("repeat_hours", 24)))


def _item_url(item: HeldItem) -> str:
    if item.hold_kind == "decision" and item.proposal_id:
        return f"/roadmap?scope=inbox&proposal={item.proposal_id}"
    return f"/roadmap?scope=inbox&product={item.product}"


def actionable_from(held: list[HeldItem], *, limit: int = 50) -> list[ActionableItem]:
    """The ranked held list as Needs-you rows (decision 7): one tap, by a person."""
    items: list[ActionableItem] = []
    for item in held[:limit]:
        if item.hold_kind == "decision" and item.proposal_id and item.recommended_key:
            actions = [ActionSpec(kind="accept_recommended", label="Accept recommendation")]
        elif item.hold_kind == "owner_action":
            actions = [ActionSpec(kind="retry", label="Retry")]
        else:
            actions = []
        subtitle = (f"{item.hold_kind.replace('_', ' ')} · blocks {item.downstream} · "
                    f"{_age_label(item.age_hours)} · {item.released_by}")
        items.append(ActionableItem(id=item.ref, title=item.title or item.ref, subtitle=subtitle,
                                    url=_item_url(item), actions=actions))
    return items


class DecisionEscalationLoop:
    def __init__(self, ctx: Any, *, read_config, load_escalations, quiet_window, last_digest,
                 record_digest, notify, now=None) -> None:
        self._ctx = ctx
        self._read_config = read_config
        self._load_escalations = load_escalations
        self._quiet_window = quiet_window
        self._last_digest = last_digest
        self._record_digest = record_digest
        self._notify = notify
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = asyncio.Lock()
        self._held: list[HeldItem] = []

    async def tick(self) -> None:
        async with self._lock:
            try:
                await self._run_once()
            except Exception:
                observation_failed(self._ctx)
                log.warning("decision_escalation.tick_failed", exc_info=True)

    async def _run_once(self) -> None:
        mode, config = await self._read_config()
        if mode == "off":
            return
        now = self._now()
        start, end = await self._quiet_window()
        if in_quiet_hours(start, end, now):
            # Decision 4: defer, don't store — the next tick outside the window
            # re-evaluates from live state, so nothing is lost and nothing leaks.
            return
        t = thresholds_from(config)
        escs = await self._load_escalations(config)
        held_all: list[HeldItem] = []
        products: list[dict] = []
        for esc in escs:
            held_all.extend(esc.held)
            products.append({"slug": esc.slug, "dispatchable": esc.dispatchable,
                             "held": len(esc.held), "starving": esc.starving,
                             "oldestHoldHours": esc.oldest_hold_hours})
            verdict = decide(esc, t)
            last = await self._last_digest(esc.slug)
            if not should_send(verdict, last, now=now.replace(tzinfo=None), t=t):
                continue
            title, body = digest_text(esc, verdict)
            await self._notify(title, body, repo=None,
                               url=f"/roadmap?scope=inbox&product={esc.slug}")
            await self._record_digest(esc.slug, {
                "slug": esc.slug, "fingerprint": verdict.fingerprint,
                "heldCount": len(esc.held), "dispatchable": esc.dispatchable,
                "starving": esc.starving, "reasons": list(verdict.reasons),
                "topRefs": [i.ref for i in esc.held[:3]]})
        from server.pm.escalation import rank_held
        self._held = rank_held(held_all)
        publish_observation(self._ctx, "decision_escalation_counts", {
            "held": len(held_all),
            "decisions": sum(1 for i in held_all if i.hold_kind == "decision"),
            "starving_products": sum(1 for p in products if p["starving"]),
            "dispatchable": sum(int(p["dispatchable"]) for p in products)})
        setattr(self._ctx, "decision_escalation_products", products)

    async def actionable_items(self, limit: int = 50) -> list[ActionableItem]:
        return actionable_from(self._held, limit=limit)
```

Note the naive/aware handling: `should_send` compares with the audit row's naive UTC `at`, so `now.replace(tzinfo=None)` is passed there; the test's fake `record_digest` stores a naive time for the same reason. Add `import asyncio, logging` and `log = logging.getLogger(__name__)`; `from datetime import timezone`.

The `repo` argument stays `None` in the loop (the digest is product-scoped); the orchestrator's `notify` wrapper resolves the product's primary repo (Task 3) so `scope_repo` rules can match.

- [ ] **Step 4: Run** — `pytest server/tests/test_decision_escalation_loop.py server/tests/test_decision_escalation_policy.py -q`; Expected: PASS; ruff + mypy on the module.
- [ ] **Step 5: Commit** — `feat(automations): decision-escalation loop — per-product digest, audit-row dedup, quiet-hours deferral`.

---

### Task 3: Provider, orchestrator wiring, registry, event class, watch, web label, wiring test

**Files:**
- Modify: `server/automations/decision_escalation.py` (`DecisionEscalationActionProvider`)
- Modify: `server/automations/registry.py` (import near `:135`; `registry.register(...)` before `validate_oversight`)
- Modify: `server/sessions/orchestrator.py` (`_PRIMARY_ONLY_LOOP_ATTRS` ~733; `__init__` provider dict ~1118 and loop block ~1547; `start()` ~2497; `stop()` tuple ~3136; loop methods beside `_deploy_monitor_tick_loop` ~12262; `automation_statuses` ~16080)
- Modify: `server/sessions/notify_rules.py:26` (`"decision_needed"`), `server/db/models.py:589` (comment), `server/assistant/watches.py` (`CONDITION_MAP["product"]`), `web/src/features/settings/sections/notificationsApi.ts:63-72` (two labels)
- Modify: `server/tests/test_work_class.py`, `server/tests/test_automation_engine_policy.py`
- Test: `server/tests/test_decision_escalation_wiring.py` (new, clone of `test_deploy_monitor_wiring.py`), `server/tests/test_decision_escalation_provider.py` (new)

**Interfaces:**
- Produces: `DecisionEscalationActionProvider(orch)` with `valid_kinds = frozenset({"accept_recommended", "retry"})`; `trigger(item_id, kind)` resolves the ref → work item and calls `decisions.accept(...)` / `holds.release_via_api(...)` **as the operator identity the API attached** — read `server/api/automations.py:337` (`POST /{automation_id}/trigger`) and `orchestrator.automation_trigger(...)`: if neither passes an identity to the provider, add an optional keyword `identity: str | None = None` threaded from the route's `current_identity` through `automation_trigger` to `provider.trigger(item_id, kind, identity=…)` (other providers ignore the kwarg via `**_`), and use `holds.actor_for_request`-equivalent semantics: a verified person → their login; the assistant → `assistant:<identity>`; missing → refuse with `TriggerResult(status="forbidden", level="warn", message="Only a person can take a decision")`. `decisions.may_resolve` remains the gate.

- [ ] **Step 1: Write the failing tests**

`server/tests/test_decision_escalation_wiring.py` (clone; every string is what the orchestrator must contain):

```python
from __future__ import annotations

import inspect


def test_decision_escalation_is_registered():
    from server.automations.registry import registry
    a = registry.get("decision-escalation")
    assert a.id == "decision-escalation" and a.modes == ("off", "notify")
    assert [f.key for f in a.config_schema] == ["tick_min", "starving_max_dispatchable",
                                                "hold_age_hours", "downstream_threshold",
                                                "repeat_hours", "products"]


def test_decision_escalation_loop_is_primary_only_and_torn_down():
    from server.sessions import orchestrator as mod
    assert "_decision_escalation_task" in mod._PRIMARY_ONLY_LOOP_ATTRS
    src = inspect.getsource(mod.SessionOrchestrator.stop)
    assert '"_decision_escalation_task"' in src, "stop() must reap the decision-escalation loop"
    start_src = inspect.getsource(mod.SessionOrchestrator.start)
    assert "decision-escalation-loop" in start_src


def test_decision_escalation_has_action_provider_and_status_ctx():
    from server.sessions.orchestrator import SessionOrchestrator
    init_src = inspect.getsource(SessionOrchestrator.__init__)
    assert '"decision-escalation": DecisionEscalationActionProvider(self)' in init_src
    statuses_src = inspect.getsource(SessionOrchestrator.automation_statuses)
    assert ('"decision-escalation": getattr(self, "_decision_escalation_ctx", None)'
            in statuses_src)


def test_decision_needed_is_a_notification_class_with_an_assistant_watch():
    from server.assistant.watches import CONDITION_MAP
    from server.sessions.notify_rules import EVENT_CLASSES
    assert "decision_needed" in EVENT_CLASSES
    assert CONDITION_MAP["product"]["needs_decision"] == ("decision_needed", "scope_automation")


def test_read_config_resolves_enabled_row_without_mode_to_notify(monkeypatch):
    import asyncio
    from server.sessions.orchestrator import SessionOrchestrator
    seen = {}

    async def fake_read(automation, *, modes, default_mode=None, log_key=None):
        seen.update(modes=modes, default_mode=default_mode, log_key=log_key)
        return default_mode, {}

    from server.automations import config_store
    monkeypatch.setattr(config_store, "read_automation_config", fake_read)
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    mode, _ = asyncio.run(SessionOrchestrator._decision_escalation_read_config(orch))
    assert mode == "notify" and seen["modes"] == ("notify",)


def test_web_notification_labels_include_decision_needed_and_account_limited():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    src = (root / "web/src/features/settings/sections/notificationsApi.ts").read_text()
    assert "{ value: 'decision_needed', label: 'Decision needed' }" in src
    assert "{ value: 'account_limited', label: 'Account limited' }" in src


def test_tick_interval_comes_from_config(monkeypatch):
    """``tick_min`` is config, not a start() literal: the loop sleeps what the row says."""
    import asyncio
    from server.sessions.orchestrator import SessionOrchestrator
    orch = SessionOrchestrator.__new__(SessionOrchestrator)

    async def fake_read():
        return "notify", {"tick_min": 7}
    orch._decision_escalation_read_config = fake_read  # type: ignore[attr-defined]
    assert asyncio.run(SessionOrchestrator._decision_escalation_interval(orch)) == 7 * 60.0
```

`server/tests/test_decision_escalation_provider.py`:

```python
"""The provider's triggers are one tap BY A PERSON (spec decision 7): the
existing decision/hold services with their actor gate intact."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from server.automations.decision_escalation import DecisionEscalationActionProvider


def _provider(held):
    loop = SimpleNamespace(_held=held, actionable_items=lambda limit=50: _items(held))

    async def _items(h):
        from server.automations.decision_escalation import actionable_from
        return actionable_from(h)
    return DecisionEscalationActionProvider(SimpleNamespace(_decision_escalation_loop=loop))


def test_invalid_kind_and_unknown_item():
    p = _provider([])
    assert p.valid_kinds == frozenset({"accept_recommended", "retry"})
    r = asyncio.run(p.trigger("o/k#404", "retry", identity="tony@example.com"))
    assert r.status == "not_found" and r.level == "warn"


def test_accept_without_a_person_is_refused():
    from server.tests.test_decision_escalation_policy import _item
    p = _provider([_item("o/k#1")])
    r = asyncio.run(p.trigger("o/k#1", "accept_recommended", identity=None))
    assert r.status == "forbidden" and "person" in r.message.lower()
```

Plus an API-level test appended to `server/tests/test_pm_decisions_api.py` (it has `_client`, `_register`, `_seed_one_pending`, `_propose`, `_stub_announce`): register a product, seed + propose, then `POST /api/automations/decision-escalation/trigger` with `{"item_id": "o/r#1", "kind": "accept_recommended"}` and `IDENTITY_HEADERS` → 200 with `status == "accepted"`, and `_hold_of(c, wid) == (None, None, None)`; the same call with `LAN_OPEN` env (unattributed loopback) → the body's `status == "forbidden"`. For this test the loop's `_held` must contain the item: call the orchestrator's tick seam directly via `c.app.state.sessions._decision_escalation_loop.tick()` through `c.portal.call` after seeding (the product needs a board — reuse `test_pm_holds_api._seed` from PR 2 instead of `_seed_one_pending` if a board is required; the PR 2 seed gives `o/r#1` a decision hold with a proposal).

Also: `test_work_class.py` gains `"decision-escalation": "maintenance"`; `test_automation_engine_policy.py` gains `"decision-escalation": EnginePolicy.CLAUDE_ONLY` (with the one-line comment the neighbours have).

- [ ] **Step 2: Run** — `pytest server/tests/test_decision_escalation_wiring.py server/tests/test_decision_escalation_provider.py server/tests/test_work_class.py server/tests/test_automation_engine_policy.py -q`; Expected: FAIL.

- [ ] **Step 3: Implement**

Provider (append to the module):

```python
class DecisionEscalationActionProvider:
    valid_kinds = frozenset({"accept_recommended", "retry"})

    def __init__(self, orch: Any) -> None:
        self._orch = orch

    @property
    def _loop(self) -> "DecisionEscalationLoop":
        return self._orch._decision_escalation_loop

    async def actionable(self) -> list[ActionableItem]:
        return await self._loop.actionable_items()

    async def trigger(self, item_id: str, kind: str, *, identity: str | None = None,
                      **_: Any) -> TriggerResult:
        item = next((i for i in self._loop._held if i.ref == item_id), None)
        if item is None:
            return TriggerResult(status="not_found", level="warn",
                                 message="That item is no longer held")
        if not identity:
            return TriggerResult(status="forbidden", level="warn",
                                 message="Only a person can take a decision")
        from datetime import datetime, timezone

        from server.db import db_session
        from server.pm import decisions, holds
        now = datetime.now(timezone.utc)
        async with db_session() as db:
            wi = await holds.work_item_for_ref(db, item.ref)
            if kind == "accept_recommended":
                if not decisions.may_resolve(identity):
                    return TriggerResult(status="forbidden", level="warn",
                                         message="Only a person can take a decision")
                proposal = await _pending_proposal(db, wi.id)
                if proposal is None or not proposal.recommended_key:
                    return TriggerResult(status="not_found", level="warn",
                                         message="No pending proposal with a recommendation")
                await decisions.accept(db, proposal, chosen_key=proposal.recommended_key,
                                       actor=identity, now=now, via="automation")
                await db.commit()
                return TriggerResult(status="accepted", level="ok",
                                     message=f"Accepted {proposal.recommended_key} on {item.ref}")
            released = await holds.release_via_api(db, wi, actor=identity, identity=identity,
                                                   signal="retry", only_kind="owner_action")
            await db.commit()
            return TriggerResult(status="released" if released else "noop", level="ok",
                                 message=f"Released the hold on {item.ref}" if released
                                 else "Nothing to release")
```

`_pending_proposal(db, work_item_id)` = `select(DecisionProposal).where(work_item_id == …, status == "pending")` → `scalar_one_or_none()`. Map `HoldPermissionError`/`DecisionHoldError`/`DecisionError` to `TriggerResult(status="refused", level="warn", message=str(e))`. The accept path also posts the GitHub comment in the API route (`backlog_github.announce_decision`) — call the same helper the route calls after commit, best-effort, so the automation path is not silently weaker than the inbox's; read `server/api/pm.py:2337-2396` and reuse its function.

Identity threading: in `server/api/automations.py` (`POST /{automation_id}/trigger`) pass `identity=` (the route's `current_identity` dependency; for the assistant it is the drawer's bound identity — mirror `holds.actor_for_request(request)` so it becomes `assistant:<id>`) into `orch.automation_trigger(automation_id, body.item_id, body.kind, identity=actor)`; in `orchestrator.automation_trigger` accept `identity: str | None = None` and call `provider.trigger(item_id, kind, identity=identity)`. Every other provider's `trigger` gains `**_: Any` (or the call site inspects the signature) — choose the smallest change that keeps `test_automations_api.py` green.

Orchestrator wiring (copy the deploy-monitor lines and rename):
- `_PRIMARY_ONLY_LOOP_ATTRS` += `"_decision_escalation_task"`.
- `__init__` providers dict: `"decision-escalation": DecisionEscalationActionProvider(self),` with the import beside the deploy-monitor one.
- `__init__` loop block:
  ```python
        from server.automations.decision_escalation import DecisionEscalationLoop
        self._decision_escalation_ctx = AutomationContext("decision-escalation")
        self._decision_escalation_loop = DecisionEscalationLoop(
            self._decision_escalation_ctx,
            read_config=self._decision_escalation_read_config,
            load_escalations=self._decision_escalation_load,
            quiet_window=self._decision_escalation_quiet_window,
            last_digest=self._decision_escalation_last_digest,
            record_digest=self._decision_escalation_record_digest,
            notify=self._decision_escalation_notify)
        self._decision_escalation_task: asyncio.Task | None = None
  ```
- `start()` (inside the primary block): `self._decision_escalation_task = asyncio.create_task(self._decision_escalation_tick_loop(), name="decision-escalation-loop")`.
- `stop()` tuple += `"_decision_escalation_task"`.
- Methods beside `_deploy_monitor_tick_loop`:
  ```python
    async def _decision_escalation_tick_loop(self) -> None:
        while True:
            await self._decision_escalation_tick_once()
            await asyncio.sleep(await self._decision_escalation_interval())

    async def _decision_escalation_interval(self) -> float:
        """``tick_min`` from the row (spec §5), floored at the schema's ``min``."""
        try:
            _mode, config = await self._decision_escalation_read_config()
            return max(5, int(config.get("tick_min", 30))) * 60.0
        except Exception:
            return 30 * 60.0

    async def _decision_escalation_tick_once(self) -> None:
        if not await self._schedule_allows_tick("decision-escalation"):
            return
        await self._decision_escalation_loop.tick()
        try:
            mode, _ = await self._decision_escalation_read_config()
            if mode != "off":
                await self._bump_automation_active("decision-escalation")
        except Exception:
            log.debug("decision_escalation.heartbeat_failed", exc_info=True)

    async def _decision_escalation_read_config(self) -> tuple[str, dict]:
        from server.automations import config_store
        from server.automations.decision_escalation import DecisionEscalation
        active = tuple(m for m in DecisionEscalation.modes if m != "off")
        return await config_store.read_automation_config(
            DecisionEscalation, modes=active, default_mode=active[0],
            log_key="decision_escalation")

    async def _decision_escalation_load(self, config: dict) -> list:
        from datetime import datetime, timezone

        from server.automations.product_policy import effective_policy, is_on, policies_by_slug
        from server.pm import decisions
        from server.pm.escalation import product_escalation
        from server.pm.queue_store import load_queue_rows, runner_claims
        now = datetime.now(timezone.utc)
        only = {str(s) for s in (config.get("products") or [])}

        def _rows(s):
            return load_queue_rows(s, now=now, claimed_refs=runner_claims(s)), policies_by_slug(s)
        async with db_session() as db:
            rows, policies = await db.run_sync(_rows)
            proposals_by_ref = {f"{w.repo}#{w.number}": p for p, w in await decisions.pending_list(db)
                                if w is not None and w.repo and w.number}
        by_product: dict[str, list] = {}
        for r in rows:
            by_product.setdefault(r.product, []).append(r)
        out = []
        for slug in sorted(by_product):
            if only and slug not in only:
                continue
            policy = policies.get(slug) or effective_policy(None)
            if not is_on(policy):
                continue
            out.append(product_escalation(
                slug, by_product[slug], proposals_by_ref, policy, now=now,
                starving_max=int(config.get("starving_max_dispatchable", 1))))
        return out

    async def _decision_escalation_quiet_window(self) -> tuple[str | None, str | None]:
        from server.db.models import AppSettings
        async with db_session() as db:
            settings = (await db.execute(select(AppSettings).where(
                AppSettings.id == "singleton"))).scalar_one_or_none()
        return ((settings.quiet_hours_start, settings.quiet_hours_end) if settings
                else (None, None))

    async def _decision_escalation_last_digest(self, slug: str):
        from server.automations.decision_escalation import DIGEST_AUDIT_TYPE
        from server.db.models import AuditEvent
        async with db_session() as db:
            rows = (await db.execute(
                select(AuditEvent.detail, AuditEvent.at)
                .where(AuditEvent.type == DIGEST_AUDIT_TYPE)
                .order_by(AuditEvent.at.desc()).limit(200))).all()
        for detail, at in rows:
            if isinstance(detail, dict) and detail.get("slug") == slug and detail.get("fingerprint"):
                return str(detail["fingerprint"]), at
        return None

    async def _decision_escalation_record_digest(self, slug: str, detail: dict) -> None:
        from server.audit import record_audit
        from server.automations.decision_escalation import DIGEST_AUDIT_TYPE
        async with db_session() as db:
            await record_audit(db, DIGEST_AUDIT_TYPE, SYSTEM_IDENTITY, detail, via="automation")
            await db.commit()

    async def _decision_escalation_notify(self, title: str, body: str, *, repo: str | None,
                                          url: str | None) -> None:
        slug = url.rsplit("product=", 1)[-1] if url and "product=" in url else None
        repo = repo or (await self._primary_repo_for_slug(slug) if slug else None)
        await push_mod.notify_event("decision_needed", title, body, repo=repo,
                                    automation="decision-escalation", url=url,
                                    fanout_when_unconfigured=True, orch=self)
  ```
  `_primary_repo_for_slug`: one `select(ProjectRepo.repo).join(Project).where(Project.slug == slug, ProjectRepo.is_primary.is_(True))` (read `orchestrator.py:7764-7782` for the existing batch map and reuse it if a helper already exists there — do not add a second query if one is present). Use whatever name the orchestrator already imports `push` under (`push_mod` per `test_orchestrator_push.py:43`) and `SYSTEM_IDENTITY` as used at `orchestrator.py:5602`.
- `automation_statuses`: `"decision-escalation": getattr(self, "_decision_escalation_ctx", None),`.

Registry:
```python
registry.register(DecisionEscalation(), oversight=AutomationOversight(
    Responsibility.EXECUTION,
    'Held items across every onboarded product: which decisions are starving dispatch, ageing, or blocking work.',
    {
        'off': 'Disable escalation; the Inbox badge is the only signal.',
        'notify': 'Send one decision_needed digest per product when it is starving, a hold is aged, '
                  'or a hold blocks enough work; repeat no more than every repeat_hours; never in quiet hours. '
                  'Never resolves a decision.',
    },
    (Relationship('uses', 'backlog-runner', 'Reads the same queue the runner dispatches from.'),
     Relationship('uses', 'pm-sync', 'Reads mirrored holds and decision proposals.')),
))
```
`notify_rules.EVENT_CLASSES` += `"decision_needed"`; `models.py:589` comment; `watches.CONDITION_MAP["product"] = {"needs_decision": ("decision_needed", "scope_automation")}` with a comment (target is a product slug; the rule scope is the automation, so the target is informational — `create_watch` stores it; read `server/assistant/api.py:433` to see whether a `product` kind needs a target transform and add the minimal one); web labels (two entries).

- [ ] **Step 4: Run** — the two new test files, `test_work_class.py`, `test_automation_engine_policy.py`, `test_automation_oversight.py`, `test_primary_lease.py`, `test_shutdown_bounded.py`, `test_assistant_watches_api.py`, `test_automations_api.py`, `test_pm_decisions_api.py`, `test_orchestrator_push.py`; Expected: PASS. ruff + mypy on the touched files. `(cd web && npm run typecheck)`.
- [ ] **Step 5: Commit** — `feat(automations): decision-escalation — registry, loop wiring, decision_needed event class, watch, provider`.

---

### Task 4: Docs, gate, PR

- [ ] `docs/product/features/decision-escalation.md` cloned from `deploy-monitor.md`'s front matter (`slug: decision-escalation`, same `parent`, `state: live`, `links.code: server/automations/decision_escalation.py`) with three short sections: what it watches, the three triggers and the digest, what it never does. Add a "Decision escalation" paragraph to `docs/product/features/product-management-layer.md`'s Holds section pointing at it. Update the spec's Status line to note PR 4 shipped.
- [ ] Gate: `ruff check src/ server/`; `mypy server/ --ignore-missing-imports`; `pytest tests/ -q`; full `server/tests/ -n auto --dist loadfile --timeout=300 -m "not slow"` → `/tmp/pr4-full.log`; `(cd web && npm run typecheck && npm run lint)`.
- [ ] In-flight PR check for `server/sessions/orchestrator.py|server/automations/registry.py|notify_rules.py|watches.py|notificationsApi.ts`.
- [ ] Push `feat/decision-escalation-automation`; `gh pr create --title "feat(automations): Decision Escalation — one decision_needed digest per starving product" --body-file /tmp/pr4-body.md` (`Refs …decision-escalation-design.md §5 (PR 4 of 6)`; trailer); `gh pr merge <#> --squash --auto`.
