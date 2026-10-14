# "Needs you" means only a human can unblock it — design (2026-10-14)

Related: #2979 (shipped as #3014: resolved blockers stop blocking; part 2 — board
placement of triage-accepted items — did not ship), #2683, #2936.

## Problem

The Products map badge read ~92 on 2026-10-14; about 5 items needed a person. Measured
from the live `/api/pm/map` + `/api/pm/leaves` (viberpit + kindash):

| Bucket on the badge | n | Truly human? |
|---|---|---|
| issue leaves in the `blocked` tier (open dependency) | 31 | no — waiting on other work |
| issue leaves `blocked` by a board Blocked card with no open dependency | 5 | no — stale card |
| issue leaves `needs-you` only because the board card says Held (no hold, no session) | 13 | no — stale card / person parked it on the board |
| owner_action "merged PR left the issue open (Refs, not Fixes)" / unchecked acceptance | 6 | mostly no — an agent can assess |
| owner_action "session found no PR is needed (not-needed)" | 3 | no — the not-needed back-off already covers it |
| owner_action "bounced at X; Y needs approval" | 1 | yes, but it is a one-click model approval |
| other owner_action (PR closed unmerged, pre-ladder needs_human) | 2 | yes |
| initiatives `awaiting_human` (SDLC gates) | ~21 | yes — governance gates |
| programs counted again on top of their phases | 5 | double count |
| initiatives `blocked` (epic_blocked) | 4 | no — waiting |
| products with triage proposals | 1 | yes |

Two web-side causes dominate. `needsYou.ts` counts the `blocked` tier as attention.
`tree.issue_tier` treats a bare board `Held` card as needs-you.

## 1. Definition (PR 1)

An item **needs you** when only a human can unblock it:

* a `decision` hold, or an `owner_action` hold (PR 3 shrinks the owner_action set);
* a model approval ("bounced at X; Y needs approval") or a session waiting on a person
  (`HUMAN_WAIT_SESSION_STATUSES`: question, permission, plan review, hand-back);
* a bounced-out issue that needs a better spec (`needs_spec`);
* an initiative gate: `awaiting_human`, or `advance` (epic done, gate not moved);
* triage proposals awaiting a decision; owner-held items with no capability (`needsHome`);
* an open issue assigned on GitHub to the product owner: an assignee that is a human
  login (not `app/…`) in the product's `trusted_authors`. The policy has no separate
  owner field, and `trusted_authors` is the list of people allowed to direct agents.

Everything else that is not moving is **waiting**, with a reason, shown un-badged:

| waiting reason | source |
|---|---|
| `blocked` | open dependency edge (queue `blocked`, `open_blockers` non-empty) |
| `boardBlocked` | board Blocked card with no open dependency (stale; PR 2 resolves) |
| `boardHeld` | board Held card with no hold and no human-wait session |
| `parked` | `parked` label |
| `epicOpen` | an epic with open sub-issues |
| `inProgress` | board In progress, a live session, or a runner claim |
| `tracker` | self-closing tracker / generated digest |
| `notNeeded` | not-needed back-off |
| `assigned` | assigned to someone other than the owner |

Tier changes in `server/pm/tree.py`:

* `issue_tier`: a board `Held` status alone no longer yields `needs-you`. It yields
  `blocked` (the existing "waiting" tier) so the leaf still reads as stopped.
* initiative nodes carry `ownTier` (the tier before the program max over phases); a program
  counts toward needs-you only when its OWN tier is needs-you, never once more per phase.
* the epic `held` roll-up (`server/api/sdlc.py:_progress…`) counts OPEN children whose
  `hold_kind` is in `HELD_KINDS`, not bare board-Held cards, because it feeds
  `initiative_tier`'s `epic_needs_human`.
* `issue_tier` gains `assigned_to_owner` (→ `needs-you`). `_issue_facts` computes it from
  `WorkItem.assignees` against the product policy's human `trusted_authors`.

Web `needsYou.ts` (the canonical badge population) counts only the `needs-you` tier.
`blocked` moves to a separate `waitingRows`, so the Needs-you list, overview and badge
change together. The product map row shows the waiting counters as quiet text chips
("31 blocked · 28 parked · 8 epic open …"), never a Badge.

The server adds `counts.waiting: {reason: n}` per product from the queue's skip
reasons. That is the same `queue_inputs` → `rank` composition `GET /api/pm/queue` uses,
so the map and the queue cannot disagree about why something is not moving.

## 2. Mechanical auto-resolves (PR 2)

One automation, `needs-you-hygiene` (primary-only loop, 15 min tick, registry entry,
mode `off | dry_run | on`), runs pure rules in `server/pm/hygiene.py`. Each rule returns
`HygieneAction(rule, product, ref, action, detail, evidence)`, and the executor decides
what happens:

* **Per-product toggle:** the new policy key `needs_you_automation` (`off | dry_run | on`,
  default `on`). With the automation in `dry_run` or the product in `dry_run`, it only
  records what it would do.
* **Risk:** `risk_tolerance` `bold`/`balanced` → apply. `cautious` → write an
  `OwnerRecommendation` row (pending), cleared in one click from the digest.
* **Audit:** every applied action writes `record_audit(…, "pm.hygiene.<rule>",
  SYSTEM_IDENTITY, detail, via="automation")` plus an `AutomationContext.emit` line;
  dry-run writes the emit line only.

Rules:

1. **done_but_open**: board Done, issue open, card's Done stamp older than 24 h → close
   as completed with a one-line comment. Skips bot / tracker / generated digests (the
   queue's `is_bot_noise` and `is_self_closing_tracker` predicates, plus the Renovate
   Dependency Dashboard and daily auto-upserted reports by title/author).
2. **board_blocked_no_blockers**: board Blocked, no open dependency, no hold → set the
   board to the policy's `queue_min_status` via `pm.board_status.apply`.
3. **stale_tracker_hold**: an owner_action hold on an issue the tracker skip now covers,
   or a "session found no PR is needed (not-needed)" hold the not-needed back-off
   covers → release via `holds.release_via_api` semantics, signal `policy_skip`.
4. **resolved_edges**: active `WorkItemDep` edges whose blocker is closed/merged →
   prune through `stale_deps.classify_stale` + `dep_remove.remove`, part 1 of #2979 as
   hygiene.
5. **triage_off_board**: classified, open, triage-accepted items with no `GhProjectItem`
   on the product's board → add at `queue_min_status` (part 2 of #2979). Skips tracker,
   bot and PR artifacts.

**Metric:** each tick writes one `AttentionSample(project_id, sampled_at, needs_you,
waiting JSON)` per product, at most hourly, and sets an `obs.metrics` gauge
`viberpit_pm_needs_you{product}`. `GET /api/pm/attention/history?product=&days=` serves
the series.

## 3. Shrinking owner_action holds (PR 3)

* **Refs-not-Fixes close-out holds** (`closeout_hold_reason`) no longer park for a
  human by default. The hygiene loop dispatches one bounded **assessment session** per
  held issue (origin `needs-you-assess`, cheapest unattended model, read-mostly prompt).
  If the remaining acceptance items are agent-verifiable, it verifies them and closes
  the issue (or releases the hold so the runner continues). Otherwise it replaces the
  hold reason with a concise checklist ("Left for you: - [ ] …") and keeps the
  owner_action hold. One assessment per (issue, body fingerprint), so a re-hold does
  not loop.
* **"bounced at X; Y needs approval"** holds get `attentionKind: "model_approval"`
  with `{from, to}` parsed by `outcomes.needs_approval_suffix`. The Needs-you card
  renders one "Approve Y" button that calls the existing approve-escalation path.

## 4. Parked review (PR 4)

The `parked-review` rule runs weekly, or when parked > `parked_review_threshold`
(policy, default 20). It is a read-only checker: for each parked item, a cheap stateless
helper reads the issue, the merged PRs that reference it, and a code grep. It proposes
`close` (fixed/obsolete, with evidence links), `unpark`, or `keep`, written as
`OwnerRecommendation` rows. Nothing is ever applied without approval: a parked label a
person placed is only removed when the owner approves.

## 5. Daily owner digest (PR 4)

`GET /api/pm/digest?product=` groups every remaining needs-you item plus pending
`OwnerRecommendation`s, one recommended action each: approve model, advance gate,
release hold, close, unpark, accept triage. `POST /api/pm/digest/apply` takes
`{ids, confirm: true}` ("approve all" passes every id) and audits each action. Once a
day, when the digest is non-empty, the `decision_needed` push sends a single summary
line. The assistant gets `get_owner_digest` (read) and `apply_owner_digest`
(operator-tier, confirmation required), each one loopback call.

## Non-goals

* No auto-advance of SDLC gates: governance keeps the human as the gate (constitution).
  The digest only *recommends* "advance".
* No automatic unpark, ever.
