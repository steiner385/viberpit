# Session-Recommended Model Switching — Design Spec

**Date:** 2026-09-02
**Status:** Approved design, pending implementation plan
**Feature:** Let a running session recommend its own model change; ccswitch auto-applies
downgrades, parks upgrades for human approval, and lets the human counter-offer a third model.

## Problem

ccswitch picks a session's model **once, at spawn**, either from explicit config or from
`automations/model_pick.assess_model()` — a Haiku complexity assessment of the *job summary*,
made before any work has happened. That estimate is frequently wrong in both directions: a
"simple" backlog item turns out to be a cross-module refactor, or an "architectural" job
reduces to a config bump once the code is read.

The session doing the work is the best-informed judge of the work's difficulty, and it holds
that judgement precisely when the spawn-time assessor could not: *after* reading the code. Today
it has no way to say so. A human must notice and switch manually, which does not happen for
unattended automation sessions at all.

This feature gives the session a voice — not a free hand.

## Decisions (locked)

Confirmed with the user during brainstorming:

- **Trust model:** *auto-apply downgrades, propose upgrades.* A request for a cheaper tier
  applies on the next turn; a request for a more expensive tier parks for human approval.
  Rationale: the asymmetry aligns the gate with cost/quota risk, which is the axis ccswitch's
  usage-aware rotation already cares about.
- **Scope:** **sessions only in v1.** Sub-agent model steering is a documented follow-up
  (see *Out of scope*) because it requires entirely separate machinery.
- **Signal shape:** extend the existing **HTML-comment marker contract**, not an LLM classifier
  and not a new tool surface.
- **Override:** on a parked upgrade the human may **approve at a different model** than the one
  requested, chosen from the **full model catalog**, defaulted to the requested model.

## Relationship to the model-approval gate spec

This feature is a strict extension of
`docs/superpowers/specs/2026-08-31-fable-approval-gate-design.md`, which built
`model_policy.py`, the `pending_model` columns, `resolve_model_approval()`, the REST
`model-approval` route, and `PendingModelApprovalPanel`. **All of that is reused as-is.**

### Amendment to one locked constraint

That spec locked: *"Deny of a launch-time hold is terminal; the requester must start a new
session to pick a different model (**no silent model substitution**)."*

The override in §5 substitutes a model. This is a **deliberate, narrow amendment**, and the
distinguishing word in the original is *silent*:

- The original constraint forbids ccswitch **silently downgrading on the machine's own
  initiative** — the failure mode where a human asks for fable, is refused, and quietly gets
  sonnet without being told. That remains forbidden.
- The override is the opposite: a human is **explicitly shown** the requested model and
  **explicitly names** the substitute. Nothing is inferred and nothing is hidden.

It also directly relieves the pain the original constraint created — "the requester must start a
new session to pick a different model" — without weakening the guarantee that motivated it.

**Unchanged:** no code path may substitute a model without a human naming it. `block` still
refuses, for humans and sessions alike.

## Chosen approach

**Ride the existing turn-end side-channel.** The `TurnEnd` event already carries
`pr_resolution` — a value the Translator derives from turn *text*, the driver latches
(`_last_pr_resolution`), and the orchestrator consumes once, post-turn. A model recommendation
is structurally identical: text-derived, turn-scoped, consumed once. It therefore needs **no new
event type and no new plumbing** — one more optional field on an existing rail.

### Rejected alternatives

- **Haiku classifier over each turn's text.** Would catch natural phrasing with no system-prompt
  change, but costs a model call per turn and can hallucinate a recommendation the session never
  made. Rejected: a self-directed model switch must never rest on an inference about intent when
  a deterministic declaration is available.
- **Marker with classifier fallback.** Best coverage, but two code paths and two failure modes
  for one signal — the classic drift shape constitution VI exists to prevent.
- **An MCP tool exposed to driven sessions.** Most unambiguous, but `sdk_factory` currently
  passes sessions only a `PreCompact` hook and no MCP server; this would mean building a whole
  channel for one boolean-ish signal. Documented extension point if ccswitch ever needs a
  richer session→host protocol.

## Design

### 1. Signal — the marker contract

New module `server/sessions/model_request.py`, following `completion.py`'s pattern exactly: one
module owns the literal, the instruction text, and the parser, so the prompt half and the scan
half cannot drift.

```
<!-- ccswitch:model=fable reason="cross-module refactor, subtle concurrency" -->
```

- `MODEL_REQUEST_RE` parses alias or pinned id plus an optional quoted reason.
- `MODEL_REQUEST_INSTRUCTION` is appended to every session's system prompt via
  `sdk_factory._build_append_string()`, which becomes the single composition point for all three
  instructions (completion, no-PR, model request).
- The instruction **reuses the tier rubric already written and validated in
  `model_pick._ASSESS_SYSTEM`** (haiku = mechanical → fable = hardest architectural), so a
  session's self-assessment and the spawn-time assessor share one vocabulary rather than
  drifting into two.
- `completion.strip_markers()` is extended to strip this marker too. **This is load-bearing, not
  cosmetic:** that helper exists so a trailing marker cannot defeat the unanswered-question prose
  heuristic. A third unstripped marker would silently re-introduce that bug.

Parsing happens in `translator.py` at `ResultMessage` — the same place `COMPLETION_MARKER` and
`resolution_from_text()` are already read.

### 2. Tier ordering — consolidate, don't add

The direction rule needs a cheapest-first ordering. One already exists, in the wrong place, and
the tier list is currently duplicated **four** times:

| Site | Form |
|---|---|
| `server/sessions/model_catalog.py` `_TIER_ALIASES` | unordered `frozenset` — documented as the canonical server definition |
| `server/automations/model_pick.py` `AUTO_MODEL_POOL` | **ordered**, cheapest-first |
| `web/src/session/modelName.ts` `TIERS` | tier → label |
| `web/src/components/session/PendingModelApprovalPanel.tsx` `TIER_LABELS` | a second web copy of `TIERS` |

(`model_catalog._LABELS` is adjacent but **not** a duplicate — it labels every *alias*, including
untiered ones like `default`, `best`, and `opusplan`. It is left alone.)

**Do not add a fifth.** Instead:

- Promote `TIER_ORDER: list[str] = ["haiku", "sonnet", "opus", "fable"]` into
  `model_catalog.py` (the documented tier SoT) and derive `_TIER_ALIASES` from it.
- Add `tier_rank(model) -> int | None` there, returning `None` for untiered models
  (`default`, `best`, unknown ids).
- Point `model_pick.AUTO_MODEL_POOL` at `TIER_ORDER` instead of redeclaring the list.
- Collapse `PendingModelApprovalPanel.TIER_LABELS` into `modelName.ts`'s exported `TIERS` — that
  component is being modified for the override UI anyway, so this is fixing it in place rather
  than working around it (CLAUDE.md: *"If it exists but is wrong, fix it in place"*).

A test asserts the web and server tier lists agree, locking the de-duplication in.

### 3. Direction rule and how it composes with existing policy

The direction rule **does not replace** `model_policy.resolve_model_policy()`. Both apply, and
**the stricter outcome wins**:

| Existing tier policy | Direction (vs. session's current model) | Result |
|---|---|---|
| `block` | either | **refused**, audited, no change |
| `require_approval` | either | **park** for approval (unchanged behavior) |
| `allow` | downgrade (`tier_rank` decreases) | **auto-apply** on the next turn |
| `allow` | upgrade (`tier_rank` increases) | **park** for approval |
| `allow` | same rank | no-op, dropped silently |

Consequences worth stating explicitly, because they are the cases a naive implementation gets
wrong:

- **A blocked tier stays blocked even as a "downgrade."** The denylist keeps exactly the meaning
  it has today; this feature cannot dissolve an ops guardrail.
- **Indeterminate direction parks.** If **either** side of the comparison is untiered —
  the session's current model *or* the requested one is `default`, `best`, or an unrecognised
  id, so `tier_rank()` returns `None` — no honest comparison exists. Park; never guess a
  direction.

Implemented as **one new function in `model_policy.py`** (the existing policy SoT) that composes
the two gates — *not* a second policy module. This is the point the "second policy concept"
risk was flagged at, and colocation is the mitigation.

### 4. Guards against a session driving itself in circles

A session that can move its own model can oscillate or nag. Four cheap guards, all evaluated in
the orchestrator before anything is applied:

1. **No-op dedupe** — a request for the tier already in force is dropped silently.
2. **Cap on auto-applied switches** — default 3 per session
   (`CCSWITCH_SELF_MODEL_SWITCH_CAP`). Past the cap the capability **degrades to parking**
   rather than dying: downgrades stop auto-applying and start proposing. The cap counts
   **only auto-applied switches**; a parked upgrade a human later approved does not consume it,
   since a human already reviewed that one.
3. **Flip-flop halt** — once a session has moved away from a tier and back, further
   self-directed requests park. Detectable from the audit trail already being written.
4. **Don't re-litigate a settled question** — once a human has *amended* a request (§5),
   further self-directed requests for that same originally-requested tier are dropped for the
   life of the session, logged and not surfaced. The human already ruled.

Parked upgrades need no rate limit (a human gates them), but re-parking an identical
`pending_model` is a no-op.

### 5. Override — "approve as…"

A parked upgrade becomes a negotiation: the session proposes, the human disposes, possibly at a
third model. *Session on sonnet asks for fable; human approves at opus.*

- `ModelApprovalBody` gains `model: str | None = None`. **Absent means approve exactly what was
  requested**, so existing behavior and existing clients are byte-identical.
- `resolve_model_approval(session_id, decision, *, model=None, identity=...)` substitutes the
  counter-offer for `rec.pending_model` before applying.
- Works for **both hold kinds** that function already branches on: launch-time
  (`status == "pending_approval"` → spawn with the counter-offered model) and mid-session
  (`driver.set_model` + promote `record.model`). Launch-time support is nearly free and is what
  relieves the "start a new session to pick a different model" pain noted above.

**Policy on a counter-offer — only `block` can refuse:**

| Tier policy on the human's chosen model | Result |
|---|---|
| `block` | **refused** (409), nothing applied |
| `require_approval` | **applied** — this action *is* the approval; must not re-park |
| `allow` | **applied** |

The §3 direction rule does **not** apply here. Downgrade-vs-upgrade gates a session moving
*itself*; a human naming a model is an ordinary user-initiated switch.

**Loop-safety (the critical implementation constraint).** `resolve_model_approval()` is
**already** loop-safe on both branches, and the override must not regress that:

- launch-time approve calls `_spawn_session` / `_enqueue_session` directly with
  `model=approved_model`, bypassing `create_session`'s gate;
- mid-session approve calls `driver.set_model(pending)` directly, below
  `set_model_for_session()`.

So the override is implemented by **substituting the counter-offered model into those two
existing variables** (`approved_model`, `pending`) and inherits the existing safety for free.

The failure mode to guard against is an implementer reasoning *"the human picked a new model, so
I should re-check policy on it"* and reaching for the gated `set_model_for_session()`. That would
hit `resolve_model_policy` → `require_approval` → park again, making **every approval re-ask for
approval** — an infinite loop the user experiences as a card that will not go away. The `block`
re-check in the table above must therefore be a **direct `resolve_model_policy` call**, not a
side effect of routing through the gated method. This mirrors the exemption the predecessor spec
documented for rotation, and gets the same kind of pinning test.

### 6. Data model

One **nullable** column on the session record, auto-applied by `_reconcile_columns` on both
SQLite and Postgres:

- `pending_model_reason TEXT NULL` — the session's stated reason, shown on the approval card.

Provenance reuses the existing `pending_model_requested_by`, written with a new distinct
identity **`session:self`** (not `SYSTEM_IDENTITY`), so audit rows and the UI can say *the
session asked for this* rather than conflating it with rotation, automation, or a human.

Per CLAUDE.md: nullable column only, so `_reconcile_columns` picks it up on both dialects; **no
new model**, so the Postgres-compat lists (`_REQUIRED_EXTRAS` in `test_postgres_compat.py`,
`RECONCILE_EXEMPT` in `test_schema_reconcile_coverage.py`) do **not** change. An Alembic
revision is still added, chained off the newest, as the reviewable record.

### 7. API

- `POST /api/sessions/{id}/model-approval` body gains optional `model`. Validated against
  `model_catalog.catalog()` plus the pinned-id pattern the catalog documents as still-accepted;
  unknown values → 422. Blocked tier → 409.
- The session payload (`_record_to_dict`, `api/sessions.py`) gains `pendingModelReason` alongside
  the existing `pendingModel`.
- Audit: `model_approval.requested` gains `detail.reason` and `detail.source="session"`;
  `model_approval.approve` gains `detail={"requested": ..., "applied": ...}`. **Requested ≠
  applied is the metric that matters** — it is the direct evidence of whether a session's
  self-assessment is trustworthy, which is what should decide whether this feature earns more
  autonomy later.

### 8. Frontend

- `PendingModelApprovalPanel` gains: the session's stated **reason**, an attribution line
  distinguishing a session-originated request from a human/automation one, and an
  **"Approve as…"** select seeded from the models query already in `web/src/api/hooks.ts`,
  defaulted to the requested model. Plain Approve is unchanged when the select is untouched.
- Full catalog in the picker — no range constraint. Rationale: no new constraint concept to
  define, test, and keep in sync as aliases appear, and it covers the case a "half-way only"
  rule would block (*session asks for fable; human decides haiku is plenty*).
- `TIER_LABELS` in that file is removed in favour of `modelName.ts` `TIERS` (§2).

### 9. Config

- `CCSWITCH_SELF_MODEL_SWITCH_ENABLED` (default **true**) — kill switch. When false, the
  instruction is not appended and any marker found is ignored. *Flagged for review: this defaults
  the feature on, which adds ~4 lines to every session's system prompt.*
- `CCSWITCH_SELF_MODEL_SWITCH_CAP` (default `3`) — §4 guard 2.

## Error handling

**Fail-open throughout**, matching `model_pick`'s stated invariant that the assessor "can only
ever REFINE a spawn, never block or fail one":

- An unparseable marker, an unknown alias, a policy-lookup failure, or a `set_model` error must
  never fail the turn. Worst case the recommendation is dropped and logged at `warning`.
- A marker requesting a blocked tier is dropped and audited; the session is not interrupted.
- Copilot sessions ignore the marker entirely (third-party model; `_pre_record` already skips
  them for suggestions and summaries on the same reasoning).
- Daemon restart mid-negotiation: `pending_model` / `pending_model_reason` persist on the row, so
  a parked proposal re-surfaces after restart or adoption — the guard counters in §4 are
  in-memory and reset, which is acceptable (they bound nagging, not correctness).

## Testing

TDD per constitution V. Seams already exist (`sdk_contract` for the driver, `_query_fn`
injection for helpers), so no test calls a real model. All four `lint-and-test` steps (`ruff`,
`mypy`, core `pytest tests/`, `pytest server/tests/`) plus web (`vitest`, `tsc --noEmit`,
`eslint`).

- **Parser:** valid / malformed / absent markers; alias vs. pinned id; reason containing quotes
  and newlines; `strip_markers` removes it (regression: an unstripped model marker must not
  defeat the trailing-question heuristic).
- **Direction + policy composition:** the §3 table exhaustively, including `block`-beats-
  downgrade and `tier_rank is None` → park.
- **Guards:** dedupe; cap boundary and its degrade-to-parking behavior; flip-flop halt; the
  settled-question drop after an amendment.
- **Orchestrator:** downgrade applies with `identity="session:self"`; upgrade parks and is
  resolvable through the existing `resolve_model_approval` path.
- **Override:** counter-offer applies on both hold kinds; blocked counter-offer 409s;
  **an approved override clears `pending_model` and does NOT re-park** (the infinite-approval-
  loop regression); audit records requested ≠ applied.
- **Consistency:** `TIER_ORDER` vs. `web/src/session/modelName.ts` `TIERS` vs.
  `AUTO_MODEL_POOL` all agree.
- **Schema:** new nullable column reconciles clean on both dialects; drift assert green; Alembic
  revision added.
- **Web:** panel renders reason + attribution; "Approve as…" defaults to the requested model and
  posts the chosen one; plain approve still posts no `model`.

## Out of scope

- **Sub-agent model steering** — deferred by decision, with the investigation captured so the
  follow-up starts from it rather than rediscovering it. A sub-agent is not a session: it
  surfaces to ccswitch as a `Task`/`Agent` tool call (`server/sessions/subagents.py`) and
  `driver.set_model()` cannot touch it. The available lever is that ccswitch already owns the
  `can_use_tool` permission callback, the installed SDK supports
  `PermissionResultAllow(updated_input=...)` (`claude_agent_sdk/types.py:234`, threaded at
  `_internal/query.py:419`), and the `Task`/`Agent` tool accepts a `model` parameter — so the
  callback could rewrite a sub-agent's model in flight. Deferred because that callback gates
  **every tool call in every session**: a bug there breaks tool execution, not just model choice.
- **Telling the session it was overridden.** If it asks for fable and gets opus, it simply runs
  on opus. A feedback channel is a larger protocol question.
- **A generic multi-kind approval subsystem** (Approach B in the predecessor spec). This feature
  adds a second approval *dimension* but still only one approval *kind*; the extension point
  remains documented, not triggered.
