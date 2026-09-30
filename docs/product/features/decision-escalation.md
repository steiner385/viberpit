---
slug: decision-escalation
parent: fleet-and-delivery-operations
title: Decision Escalation
state: live
links:
  code:
    - server/automations/decision_escalation.py
---

# Decision Escalation

Automation id `decision-escalation` (Delivery › Automations). Makes sure an operator hears that
decisions are starving dispatch instead of having to discover it.

**Default off.** Enable the row and set the mode to `notify` (modes: `off`, `notify`).

**What it watches.** Every product's held items (`decision` and `owner_action` holds) through the
escalation read model (`server/pm/escalation.py`), once per tick (`tick_min`, floored at 5 minutes).
A product whose rows cannot be loaded, or whose send fails, is skipped without affecting the others.

**Triggers.** A product escalates when any of these holds:

- *starving*: dispatchable items at or below `starving_max_dispatchable`;
- *aged*: its oldest hold is at least `hold_age_hours` old;
- *downstream*: a held item blocks at least `downstream_threshold` other items.

**The digest.** One `decision_needed` notification per escalating product. Title:
`"<slug>: N held (D decisions), M dispatchable"`. Body: the top three loudest holds, one line each
(`"#1 <title> — blocks 7 (recommended: b)"`), then a tail line such as
`"2 more · oldest 8d · why: aged, downstream"`.

**Re-send rule.** Each send writes a `pm.escalation.digest` audit row (this is the dedup state and
survives restarts). The digest is re-sent only when things got worse: a new held ref, starving
flipping on, or the maximum downstream count rising. The operator's own progress (resolving or
releasing holds) never re-pages them. An unchanged situation repeats after `repeat_hours`.

**Routing.** With no notification rules configured the digest goes to the default push/webhook
(`fanout_when_unconfigured`). Once any rule exists (including an assistant watch), only a
`decision_needed` or `*` rule delivers it; otherwise it is dropped, while dedup still records it as
sent. The assistant can subscribe with `watch(kind="product", condition="needs_decision")`, scoped
to this automation and the product's primary repo.

**Quiet hours defer.** During quiet hours the tick is skipped; nothing is queued. The window is
currently evaluated in UTC (#2562).

**It never resolves a decision.** The Needs-you card offers *Accept recommendation* (decision
holds) and *Retry* (owner-action holds). Both run as the tapping person through the existing
`decisions.accept` / `holds.release_via_api`, never a system actor. The accept is pinned to the
proposal the card showed, so a stale card cannot accept a newer proposal.

**Config keys (defaults).** `tick_min` 30, `starving_max_dispatchable` 1, `hold_age_hours` 48,
`downstream_threshold` 3, `repeat_hours` 24, `products` (empty = every product with dispatch on).

Spec: `docs/superpowers/specs/2026-09-30-decision-escalation-design.md` §5.
