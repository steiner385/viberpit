# Holds and the escalation ladder: replacing "Needs human" — Design

**Status:** approved design, 2026-09-18. Supersedes the single `needs_human` triage state and
the board Status value "Needs human".

**Principle:** a human is the last rung of an escalation ladder, not the first. The system may
hold an item for evidence, for a decision, or for a stronger model, and each of those holds
exits on its own. Only an owner-only action renders as "Needs you".

## 1. Problem

On 2026-09-17 the ViberPit board carried 45 open "Needs human" cards. The audit trail showed:

| Writer | Mechanism | Count |
|---|---|---|
| Backlog Runner give-up | max attempts, PR closed unmerged, PR Doctor gave up, close-out gate | 1 ever |
| Classification proposal rejected | `triage_state = needs_human` | 5 ever |
| Overlay PATCH `triageState=needs_human` | any caller, no reason field | 39 open items |
| Session in a human-wait status | question, permission, plan review, failed turn | transient |

The 39 overlay holds were written in two seconds on 2026-09-12 by a sub-agent of a Codex
"5S the hierarchy" session, as a side effect of a RICE scoring pass
(`docs/audits/2026-09-12-viberpit-backlog-priorities.md`, PR #1606). The API stamped every one
`classified_by = human`, so the reconciler treated them as human decisions it must never
overwrite, and no cooldown applied because the 7-day `needs_human_retry_days` window covers only
the runner's `gave_up` rows. Reading the 37 hold reasons: about 25 asked for evidence
("profile", "correlate", "gather a blocking stack"), about 6 asked for a design decision, one was
an epic, and three or four were genuinely owner-only. The label was right for about one item in
ten.

Three structural defects made this possible and will recur without this design:

1. The overlay API records any write as a human decision.
2. Triage-state holds never expire.
3. One state carries four meanings: needs evidence, needs a decision, needs a stronger attempt,
   needs an owner-only action.

## 2. Decisions taken

| # | Question | Decision |
|---|---|---|
| 1 | The 35 existing AI-written holds | Release the evidence-class items now (cleanup script, §10); keep decision and owner-only items held until the ladder exists |
| 2 | Model ladder | Climb one tier per bounce; park only after a bounce above the top allowed tier (fable by default) |
| 3 | Decision-class holds | Agent drafts options and a recommendation into the inbox; nothing runs until a person taps Accept |
| 4 | Vocabulary | Hold fields on the work item; board value renamed "Needs human" → "Held" in the same series |
| 5 | Provenance | Record the real actor; agents may set evidence and stronger-model holds only |
| 6 | Owner-action expiry | No timer. Releases only on a human signal: board edit, issue body edit, or Retry tap |
| A | Storage | Hold columns on the work item (not a ledger table, not proposal-only) |

## 3. Data model

New nullable overlay columns on `work_items` (picked up by reconcile on both dialects):

| Column | Type | Meaning |
|---|---|---|
| `hold_kind` | `evidence` \| `stronger_model` \| `decision` \| `owner_action` \| null | why the item is held; null = no hold |
| `hold_reason` | text | mandatory whenever `hold_kind` is set |
| `hold_by` | actor string (§8) | who set it |
| `hold_set_at` | naive UTC (`db_utc_now()` rule) | when |
| `hold_fingerprint` | text | `body_fingerprint` at hold time; a differing current fingerprint is the "issue body edit" release signal (same rule as `needs_spec`) |

`WORK_ITEM_TRIAGE_STATES` shrinks to `unclassified`, `proposed`, `classified`. The value
`needs_human` is removed. Rejecting a classification proposal writes an owner-action hold by
the rejecting person, reason "classification proposal rejected", and leaves triage state as it
was.

New table `decision_proposals`, shaped like `triage_proposals` so the inbox lists both:

| Column | Meaning |
|---|---|
| `id`, `work_item_id`, `project_id` | keys |
| `options` (JSON) | list of `{key, title, summary}` |
| `recommended_key` | the agent's recommendation |
| `rationale` | why |
| `status` | `pending` \| `accepted` \| `rejected` |
| `chosen_key` | set on accept |
| `session_id` | proposing session |
| `created_at`, `resolved_at`, `resolved_by` | audit |

`dispatch_outcomes` gains `dispatch_kind` (`implement` \| `investigate`, default `implement`).
The bounce count (`bounces_stmt`) reads implement rows only and excludes rows whose cause is
`decision_proposed`.

The runner's `automation_target_state` row keeps `gave_up` for slot accounting only. The hold
on the item is the single handoff signal; `needs_human_at` and the `needs_human_retry_days`
config are removed.

**Migration 0049** (renumber off master's newest at implementation time; verify the chain is
linear): adds the columns and the table; converts every remaining `triage_state = needs_human`
row to `hold_kind = owner_action`, `hold_by = migration`, `hold_reason = "pre-ladder
needs_human"`, and sets triage state to `classified` where all four RICE inputs are present,
else `unclassified`. Add the new table to `_REQUIRED_EXTRAS` in `test_postgres_compat.py`.

## 4. The model ladder

Every bounce writes a stronger-model hold. That is the one escalation mechanism, whether the
runner or a session asks for it.

- **Bounce** = a session ended with no mergeable PR (`no_pr`), or PR Doctor gave up
  (`pr_gave_up`). Both climb. A PR closed unmerged (`abandoned`) is a human signal: it writes an
  owner-action hold by `runner` with the PR link as the reason.
- **Rung** = one tier above the previous attempt's recorded model, never below the product
  default or the capability floor (`tier_floor_for_dispatch`). The previous model comes from the
  dispatch outcome row, so an attempt that ran on `auto` climbs from the tier it resolved to.
  Unresolvable → climb from `sonnet`. The rung function lives in `server/pm/outcomes.py` beside
  the floor rule, ranked by `model_catalog.TIER_ORDER` (one tier ordering, constitution VI).
- **Top** = the highest tier the product's denylist allows (fable by default). A bounce at the
  top writes an owner-action hold by `runner`, reason "bounced at <tier>". The merged-PR
  close-out human gate is unchanged.
- **`max_attempts` is removed.** The ladder bounds attempts by construction.
- **A session may ask.** `set_hold(stronger_model, reason)` (§8) from a coding session; the next
  dispatch climbs from that session's tier.
- **Clearing.** The dispatch that consumes a stronger-model hold clears it. The outcome row
  records requested and applied tier; `model_approval.approve`'s requested ≠ applied metric
  extends to ladder dispatches.
- **No approval step.** Ladder dispatches spawn at the rung; the product automation policy is
  the authorization. The self-directed model-switch flow is untouched.

## 5. Evidence dispatch

An evidence hold is queue-eligible, not parked.

- **Written by** the error-triage automation at filing time (every auto-error issue is born as
  `hold_kind = evidence`, `hold_by = automation:error-triage`), by sessions, or by people.
- **Dispatched** by the runner as an ordinary candidate (same capacity, ranking, policy) with an
  investigation prompt instead of the implement prompt. The prompt asks for exactly one of four
  exits, using existing session tools:
  1. **Close as noise** — comment why, close.
  2. **Consolidate** — close siblings as duplicates of one primary.
  3. **Convert to a fix** — write findings into the body, clear the hold (`set_hold(null)`);
     the item re-enters as an implement candidate at its score.
  4. **Escalate** — owner-action hold with the specific thing only the owner can supply.
- **Grouping** adds the error-triage fingerprint family as a key so sibling reports become one
  investigation; the per-dispatch issue cap still applies.
- **Not a bounce.** `dispatch_kind = investigate` rows are excluded from the bounce count.
- **Read-only on code.** No PR expected; runs at the product default tier.
- **Consequence copy** ("next run investigates this") comes from `server/pm/explain.py`.

## 6. Decision proposals

- **Create:** `POST /api/pm/work-items/{id}/decisions` with `options`, `recommended_key`,
  `rationale`. Writes the proposal and a decision hold by `session:<id>` in one transaction.
  Claude sessions get a `propose_decision` session tool whose handler calls exactly this
  endpoint; Codex sessions call it over loopback. The session then ends with `mark_no_pr`,
  resolution "awaiting decision"; the outcome row's cause is `decision_proposed`.
- **Inbox:** pending decision proposals list with classification proposals, same ordering spec
  and keys. The card shows options as a single-choice group, recommendation preselected,
  rationale beneath, Accept and Reject. Accept takes the selected option.
- **Accept** (`POST …/decisions/{pid}/accept` with `chosen_key`): clears the hold, records the
  key, item is queue-eligible. The next implement prompt includes the accepted decision verbatim
  from the proposal row. A comment is posted on the issue for the GitHub-side record.
- **Reject:** owner-action hold by the person, reason "decision proposal rejected".
- **Explainer:** two-column consequence guidance as for classification proposals.
- **Assistant:** one `resolve_decision` tool mirroring `resolve_triage`; the policy allowlist
  stays deny-by-default.
- **Notification:** reuses the pending-proposal channel. No new kind.

## 7. Board and tier derivation

Single derivation in `server/pm/desired_state.py` and `server/pm/tree.py`.

- **Board Status precedence:** Done → session waiting on a person → owner-action or
  pending-decision hold → Blocked → Ready → Backlog. Evidence and stronger-model holds are not
  in the chain: such an item reads Ready/Backlog until dispatched, then In progress. The value
  written for a held item is `Held` (§9).
- **Session set correction:** `needs_attention` (failed turn) leaves
  `HUMAN_WAIT_SESSION_STATUSES`; a failed runner turn is a bounce, not a human wait. Question,
  permission, and plan review stay.
- **Roadmap tier:** needs-you iff owner-action hold, pending decision proposal, session in a
  human-wait status, or the existing `needs_spec` reason. Evidence and stronger-model holds
  render as a leaf reason chip ("Investigating next", "Retrying on <tier>") with the tier the
  item would otherwise have; labels in `web/src/features/products/vocab.ts`.
- **Queue skip:** `needs_human_cooldown` → `held` (owner-action or pending-decision). The queue
  entry carries `hold_kind` so the spawn picks the prompt and the rung. `needs_human_at` is
  removed from `QueueRow`.
- **Releasing an owner-action hold** — three human signals, one implementation in the
  reconcile pass: a human board edit to Ready/Backlog newer than `hold_set_at`, a body
  fingerprint change, or `POST /api/pm/work-items/{id}/hold/release` (audited). Release
  re-enters at the product default tier. Closes #1540.
- **Review-and-decide dialog:** shows kind, reason, author, time. Owner-action: adds **Retry**
  and **Implement now** (existing manual dispatch endpoint). Evidence: "Investigating next run"
  plus **Investigate now**. Decision: links to the proposal card. All button copy from the
  explainer.

## 8. Provenance on the API

- **Actor resolution:** one helper in `server/auth/` maps a request to an actor string: a
  verified Tailscale identity for a person; `assistant:<identity>`; `session:<id>` via a session
  tool; `runner` / `automation:<id>`; `lan@local` for an unattributed loopback caller. The
  string goes into `classified_by` and `hold_by`. The reconciler's human-ownership flag is true
  only for a verified person, so an agent-written score no longer outranks a person's later
  GitHub edit.
- **Permissions by class:** a person may set or clear any kind. Session, assistant, loopback:
  `evidence` and `stronger_model` only; anything else → 422 with reason. Decision holds only via
  the decisions endpoint. Owner-action from a person, the runner (top-tier bounce, close-out
  gate, PR closed unmerged), or the two reject flows. An actor may clear only a kind it may set.
- **Reason is mandatory** when setting any hold (422 otherwise).
- **Session tools:** `set_hold(kind | null, reason)` and `propose_decision(...)` in
  `server/sessions/tools/`, one handler → one endpoint each, `ToolSpec` + eval case per the
  session-tools doc. Codex uses the endpoints over loopback.
- **Audit:** `pm.hold.set`, `pm.hold.release` (item, kind, reason, actor class); the overlay
  audit gains the changed-field list. A `False` regression row per new mutating route in
  `test_assistant_policy.py`.

## 9. The rename

"Needs human" → "Held" wherever it is a machine value. "Needs you" stays as the roadmap tier.

- **Board option:** renamed in place via the Projects API (`updateProjectV2Field`), which keeps
  every card's assignment. Rollout tolerates mixed boards: the reconciler reads both spellings as
  Held and writes whichever spelling the board's field cache has. A one-time script
  (`scripts/rename_board_held.py`, dry-run first) renames each board; once all report "Held", a
  follow-up PR drops the read alias.
- **Code:** `STATUS_HELD = "Held"` in `desired_state.py`; the nine production and six test
  modules follow. The SDLC initiative payload's `needsHuman` count becomes `held`, with its three
  web consumers in the same change.
- **Web:** the five files rendering the board value show "Held"; never bare — the chip/dialog
  says the kind.
- **Docs:** the fifteen docs naming the old value; `docs/product/features/product-management-layer.md`
  gains a "Holds" section as the single description of the ladder.
- **Dead code:** `scripts/retire_claim_labels.py`, `server/pm/label_retirement.py`, and their
  test are removed (the one-time sweep ran 2026-09-11; the runbook keeps the record).
- **Ordering:** the rename is the last PR of the series.

## 10. Cleanup of the existing holds (decision 1)

Before any code lands, a one-off audited script on dobby (loopback, `lan@local`) re-dispositions
the 35 AI-written holds listed in the 2026-09-12 audit:

- The evidence-class rows (the 25 auto-error reports plus the "verify" holds #1477 and #1495)
  → `triage_state = classified`, scores kept, so the runner's existing grouping batches them.
  Once §5 lands the error-triage automation re-marks any still-open auto-error issue as an
  evidence hold.
- Decision-class rows (#1502, #1517, #1524, #1540, #1545) and owner-only rows (#1592, #1594,
  #1579) stay held; migration 0049 converts them to owner-action holds by `migration`.
- #997 (epic) → `classified`; the `epic_open` skip already excludes it from dispatch.

The script writes a before/after JSON journal to `/tmp` and an entry in `docs/audits/`.

## 11. Testing

Every rule lands in a pure module with a pure test, as the tier, desired-state, and queue
modules are tested today.

- **Derivation:** table-driven tests for `derive_status` and `issue_tier` across hold kind ×
  session status × blockers × closed; the `needs_attention` change; evidence and stronger-model
  never yield Held or needs-you.
- **Queue:** `skip_reason` for `held`, eligibility of the two auto-exiting kinds, cooldown gone;
  hold kind carried through `rank`.
- **Ladder:** rung tests beside the floor rule (recorded model, `auto`, unknown → sonnet,
  denylist cap, top-tier bounce → owner-action). A sweep test walks one target: no-PR bounce →
  stronger-model hold → spawn at the next rung → top-tier bounce → hold by `runner`, outcome rows
  recording requested and applied tier. PR-closed-unmerged → owner-action, not a climb.
  `decision_proposed` end → no bounce.
- **Evidence:** grouping by fingerprint family; prompt selection by dispatch kind; bounce query
  ignores investigate rows.
- **API:** overlay per actor class (person any kind; session/loopback refused owner-action with
  422; reason required; clear limited to settable kinds; `classified_by` never human for a
  non-person). Decision create / accept with non-recommended key / reject. Release endpoint
  audit. `False` rows in the assistant policy test.
- **Schema:** migration in the linear-chain test; table in `_REQUIRED_EXTRAS`; data-conversion
  test over seeded `needs_human` rows.
- **Rename:** script over canned GraphQL (as the retirement planner was); reconciler reads both
  spellings, writes the cached one.
- **Session tools and explainer:** an eval case per new tool; explainer tests for the new
  consequence strings, mirroring loop behaviour.
- **Web:** card, dialog, chip tests; the full vitest run.

No absolute date literals in any test (date-literal time-bomb rule); use injected clocks.

## 12. PR series

1. Cleanup script run + audit entry (§10; no code).
2. Data model + migration + actor resolution + overlay permissions + audit (§3, §8).
3. Derivation + queue + release signals + dialog buttons (§7).
4. Ladder (§4) + `dispatch_kind` + evidence dispatch and grouping (§5).
5. Decision proposals: endpoint, inbox card, session tool, assistant tool, explainer (§6).
6. Rename + board script + docs + dead-code removal (§9); read-alias removal as a follow-up.

## 13. Non-goals

- A per-issue "risk tolerance" veto window for decisions (decision 3 chose wait-for-accept).
- Renaming the "Needs you" roadmap tier.
- New board options per rung.
- Changing the merged-PR close-out human gate.
- Changing the self-directed model-switch approval flow.

## 14. Open follow-ups

- Read-alias removal after all boards are renamed (§9).
- Whether the evidence investigation prompt should run at a lower tier than the product default
  to save spend; start at the default and let outcome data decide.
- A sub-agent's `set_hold` call takes effect in the handler (no `parent_tool_use_id` is available
  in-process), so its transcript card reads like the other SIGNAL tools' inert misuse case while
  the hold is real. Bounded to the two agent-settable kinds and audited with the session id; gate
  or mark it when the tool context gains the caller's agent id.
- `actor_for_request` can return a system-class string if a request presents an identity literally
  named `runner`/`migration` (dev or LAN-open only); take the system class only from in-process
  callers.
- §7's "newer than `hold_set_at`" guard on the board-edit signal is not implemented:
  `HumanEdit` carries no timestamp and the mirror fetch time is not threaded into
  `release_by_signals`. A hold written during the fetch that follows a human's move to Ready can
  therefore be released by that pre-hold edit. Thread the edit's (or the fetch's) time through and
  compare it against `hold_set_at`.
