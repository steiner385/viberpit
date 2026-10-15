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
  `OwnerRecommendation` row (pending), cleared in one click from the digest
  (`POST /api/pm/recommendations/apply` with `confirm: true` until PR 4's digest). A
  dismissed finding is never recommended again; a pending one whose condition is gone
  is `superseded`.
* **Audit:** every applied action writes `record_audit(…, "pm.hygiene.<rule>",
  SYSTEM_IDENTITY, detail, via="automation")` plus an `AutomationContext.emit` line;
  dry-run writes the emit line only.

Rules:

1. **done_but_open**: board Done, issue open, card untouched for 24 h (the card's
   `gh_updated_at` is its last change to any field — there is no Done stamp — so this is a
   conservative proxy) → close as completed with a one-line comment. Skips bot / tracker /
   generated digests (the queue's `is_bot_noise` and `is_self_closing_tracker` predicates;
   the Renovate Dependency Dashboard is a built-in bot title and the daily
   `traceability-drift` report a tracker label and title shape), open epics, and items with
   a session, claim or hold.
2. **board_blocked_no_blockers**: board Blocked, no open dependency, no hold → set the
   board to the policy's `queue_min_status` via `pm.board_status.apply`.
3. **stale_holds**: an owner_action hold a SYSTEM actor (runner, reconciler, migration,
   `automation:*`) wrote on an issue the queue's not-work rule now skips, or a "session
   found no PR is needed (not-needed)" hold the not-needed back-off covers → release,
   signal `policy_skip`. `policy_skip.stale_system_holds` is the one rule; the backlog
   tick's release calls it too. Never a person's, an agent's or a `decision` hold.
4. **resolved_edges**: prunable stale edges (`stale_deps.classify_stale`:
   `native_resolved`, `prose_vanished`) → `dep_prune.prune`, the same core as the prune
   route, part 1 of #2979 as hygiene.
5. **triage_off_board**: no rule — the reconciler already owns it. `reconcile_diff.membership`
   adds every open issue with a node id to its product's board, and once mirrored
   `desired_state.derive_status` places a classified item at Ready (pinned by
   `test_reconcile_convergence.py::test_a_classified_issue_missing_from_the_board_is_added_and_placed_at_ready`).

**Metric:** each tick writes one `AttentionSample(project_id, sampled_at, needs_you,
waiting JSON)` per product, at most hourly, and sets an `obs.metrics` gauge
`viberpit_pm_needs_you{product}`. `GET /api/pm/attention/history?product=&days=` serves
the series.

## 3. Shrinking owner_action holds (PR 3)

* **Refs-not-Fixes close-out holds** (`closeout_hold_reason`, and the pre-#2681
  `LEGACY_CLOSEOUT_HOLD_REASON`) no longer park for a human by default. The hygiene loop
  runs rule 5, `closeout_assess`: one bounded **stateless helper assessment** per held
  issue (`server/automations/closeout_assessor.py`, Sonnet, `stateless_helper_options()`,
  ≤ 3 per tick fleet-wide). It reads the issue body (the mirror) and the newest merged
  PRs that reference it (cross-reference timeline; body, changed files, a 12k-character
  diff excerpt) and classifies each unchecked acceptance item — or, with none, the rest
  of the issue — as `done` (with evidence), `agent_doable` or `human_only`
  (`server/pm/closeout_assess.py`, pure). `decide`: all done → **close** with a comment
  citing the evidence; no human-only item but some agent-doable → **release** the hold
  (signal `assessment`) so the runner continues; otherwise **rewrite** the hold reason
  to a concise checklist ("Left for you: - [ ] …", ≤ 5 items, ≤ 500 chars) and keep
  the owner_action hold (age and body fingerprint unchanged). An unusable reply decides
  nothing.
* *Why a helper call, not an assessment session* (the earlier wording): the server, not
  the model, applies the outcome — through the same executor as every other rule — so
  each action is `risk_tolerance`-gated (`cautious` → an `OwnerRecommendation`),
  dry-run-able and audited `pm.hygiene.closeout_assess`. A session's free-form tool
  calls would bypass all three.
* One assessment per (issue, body fingerprint), stored as the automation's
  `AutomationTargetState` (target = ref; dropped once the issue leaves every board).
  Once an outcome is applied it is stamped and never re-proposed for that body, so a
  re-hold does not loop. Every write re-checks the hold it judged
  (`holds.assessed_system_hold`: same system-written reason), so a person's park in
  between is never touched. Only system-written holds are assessed. The backlog tick's
  old legacy-reason re-judge pass is gone: this rule covers both spellings.
* **"bounced at X; Y needs approval"** holds get `attentionKind: "model_approval"`
  (`escalation.attention_kind`; the `{bouncedAt, needsApproval}` pair is the existing
  `escalation` wire object from `outcomes.escalation_for`). An assessed checklist is
  `"checklist"`, a decision `"decision"`, anything else `"owner_action"`. The Needs-you
  card labels each kind and renders a checklist as a list; the "Approve Y" button is the
  existing approve-escalation path.

## 4. Parked review (PR 4)

Rule 6 of the hygiene loop, `parked_review`, runs per product weekly, or daily while more
than `parked_review_threshold` (policy int, default 20, `0` = weekly only) issues are parked
(`parked_review.run_due`). A run covers up to 15 parked issues, the least recently reviewed
first, at ≤ 5 helper calls per tick fleet-wide; its pending refs persist in the automation's
target state `parked-review:<slug>`, so a run spans ticks. It is a read-only checker: for each
parked item a cheap stateless helper (`server/automations/parked_reviewer.py`, the close-out
assessor's `ask_helper` + `merged_prs`) reads the issue and the merged PRs that reference it
(cross-reference timeline: title, body, changed files, a diff excerpt) and answers ONE verdict
(`server/pm/parked_review.py`): `close_fixed` (must cite one of those merged PRs),
`close_obsolete` (with evidence), `unpark`, or `keep`. The verdict is stored per issue
(`parked:<ref>`, keyed to the body fingerprint) and turned into an `OwnerRecommendation` —
`close` (`completed` / `not_planned`, the evidence as the comment, the PRs as links) or
`unpark` (remove the label, with an explaining comment). Nothing is ever applied without
approval: `RECOMMEND_ONLY_RULES` keeps it a recommendation whatever `risk_tolerance` says,
the executor refuses it via `automation`, and every approved write re-checks the issue is still
open and parked. A dismissed recommendation is never re-proposed; one whose issue was unparked
or closed meanwhile is superseded.

*Deviation:* no code grep. The helper has no tools (`stateless_helper_options()`), and a GitHub
code search per item is rate-limited to 10/min; `close_obsolete` therefore rests on what the
merged PRs show (removed or replaced code), and the model is told to answer `keep` when unsure.

## 5. Daily owner digest (PR 4)

`GET /api/pm/digest?product=` groups every remaining needs-you item plus pending
`OwnerRecommendation`s, one recommended action each (`server/pm/digest.py`, composed by
`server/api/pm_digest.py:load_digest`): approve model (a `model_approval` hold), apply a
recommendation (close, unpark, release hold, …), accept triage (only a `high`-confidence
proposal onto a capability), and — since #3112 — accept a decision whose proposer named a
recommended option, release a system hand-off the queue skips anyway
(`policy_skip.stale_system_holds`, rule 3's own predicate, ahead of any approve button), and
advance an initiative whose build epic is done (`tree.initiative_reason == "advance"`). A
checklist, an `awaiting_human` gate (a plan or design to review), an `owner_evidence` ask
(#3113: a fact only the owner can supply, listed in its own group after everything a person
decides) and any other hand-off carry none. The human is still the gate: nothing moves until
they approve the recommendation, and every gate item links to the gate. An owner recommendation
targeting an item the digest already lists stands in for it. `POST /api/pm/digest/apply` takes
`{ids, confirm: true}` ("approve all" passes every id with a recommendation), re-derives each
action from current state and runs it through the existing path (`approve_escalation`,
`pm_hygiene.apply_pending`, `apply_accept`, `decisions.accept`,
`holds.release_policy_skipped_holds`, `sdlc.advance_initiative_row`), each with its own audit
plus one `pm.digest.apply` row per call. Once a day per product, when the digest is non-empty and `needs_you_automation`
is not off and outside quiet hours, the hygiene tick sends a single `decision_needed` push
("N items need you — M have a recommendation"), deduped on its `pm.digest.sent` audit row. The
assistant gets `get_owner_digest` (read) and `apply_owner_digest` (operator-tier, destructive
class: a confirmation card naming each item), each one loopback call. The web shows a "Today's
digest" card at the top of the Needs-you tab.

## Non-goals

* No auto-advance of SDLC gates: governance keeps the human as the gate (constitution).
  The digest only *recommends* "advance" (#3112: as a one-tap recommendation the person
  approves, never applied by the loop).
* No automatic unpark, ever.
* No automatic close on an `owner_evidence` reply (#3113): the mirror records that a human
  commented, not who — a session's own follow-up lands under the owner's login — so the
  `evidence_posted` rule is recommend-only and the owner's tap is the proof.
