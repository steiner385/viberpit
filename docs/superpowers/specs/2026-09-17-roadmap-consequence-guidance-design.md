# Roadmap consequence guidance — design

**Date:** 2026-09-17
**Status:** approved in chat (decisions D1–D6 below), spec written for the plan
**Builds on:** `2026-09-14-roadmap-auto-execution-design.md` (gates, tolerance, §11 UI),
`2026-09-07-roadmap-drilldown-design.md` (initiative page, inbox), `2026-09-12-product-automation-policy-design.md`

## 1. Problem

The roadmap, initiative page, product automation card and triage inbox all carry controls
whose consequences are invisible from the control itself. The confusion is specific and
recurring: *does tapping this change a status, or does it start work?* Today the honest
answer is "both, later":

- **Advance** and **Step back** write a stage column and nothing else
  (`server/api/sdlc.py:advance_initiative`). About a minute later the synthesizer loop
  (`server/automations/sdlc_synthesizer.py:_run`, ticked every 60 s) sees an initiative with
  no artifact for its stage and spawns a billed model session whose prompt embeds the whole
  upstream document. The confirm dialog says only "the stage engine acts on the new stage on
  its next tick".
- **Accept materialization** is immediate and heavy: it creates the epic and sub-issues on
  GitHub, pre-classifies them so the Backlog Runner can dispatch them, and advances the stage
  itself (`server/pm/materialize_apply.py:apply`). **Reject** looks harmless but frees the
  loop to spawn a fresh decompose session on the next tick.
- **Unblock** only flips status to `awaiting_human`; it never dispatches.
- **Inbox Accept** copies the classification onto the work item. Whether that leads to a
  coding session depends on the product's `dispatch` policy, which the inbox never mentions.
- **Risk tolerance** decides which of these gates the machine taps for you. The policy card
  shows a dropdown with a one-line help string; the table of gates it governs (owed by the
  2026-09-14 spec §11) was never built.

The guidance the user needs already exists server-side and never reaches the browser:
`server/sdlc/gates.py:GATES` carries, per gate, an `action` sentence whose field comment says
"shown in the UI" and a `reverse` sentence; no endpoint exposes it. `server/sdlc/engine.py:
next_action` is pure and takes `tolerance` as a parameter, so it can answer "what would the
loop do after this tap" without executing — but the detail endpoint calls it with tolerance
hardcoded to `cautious` (`server/api/sdlc.py:467`), so `nextAction` shows the human's next
step rather than what the product's actual policy will do.

Principle (2026-09-14 spec, restated): idle time waiting on a user is a failure mode. A user
who cannot tell whether a tap starts work either taps nothing or taps and then watches.

## 2. Decisions

- **D1 — one source of truth.** Every consequence sentence is computed on the server by a pure
  module that *runs the real engine on the hypothetical post-tap state* and describes the
  result. Nothing is hand-written per surface on the client. The client's parallel sentence
  map (`NEXT_ACTION_LABEL` in `web/src/features/products/vocab.ts`) is deleted in the same
  change (working norm: purge replaced code).
- **D2 — scope.** Three surfaces in one feature: the initiative page (Advance, Step back,
  Unblock, materialization Accept/Reject, plus a "What happens next" card), the product
  automation card (the gate table), and the inbox detail pane (what Accept and Reject do).
- **D3 — real tolerance.** `nextAction` on the detail endpoint is computed under the product's
  *effective* tolerance (policy value, folded through the kill switch), not `cautious`. The
  guidance separately states what the human can do and what the machine will do.
- **D4 — cost said plainly.** Every consequence that spawns a session says so, naming the
  model the product's policy would use, and every consequence that writes to GitHub says so.
- **D5 — the gate registry gets a read endpoint**, `GET /api/sdlc/gates`, and the gate table is
  composed from it on the client. This deviates from the 2026-09-14 spec §11, which pointed the
  table at `GET /api/pm/policy-schema`; that endpoint lists policy *fields*, not gates.
- **D6 — no magic numbers in copy.** The synthesizer tick interval travels in the payload
  (`tickSeconds`); the client renders "in about a minute" from it, never from a literal.

## 3. The consequence model

New pure module `server/sdlc/explain.py`. Same purity rule as `engine.py` and `gates.py`: no
I/O, no imports from `server.db` or `server.automations`; total over plain dataclass inputs.

### 3.1 Input

```python
@dataclass(frozen=True)
class ExplainContext:
    initiative: InitiativeView          # engine view, incl. epic / epic_total / epic_done
    artifacts: list[ArtifactView]
    mode: str                           # synthesizer automation mode: "on" | "off"
    tolerance: str                      # EFFECTIVE (after the kill switch) — gates.effective_tolerance
    configured_tolerance: str           # the policy value, so "balanced but switched off" can be said
    autoresolve_enabled: bool           # VIBERPIT_GATE_AUTORESOLVE_ENABLED
    has_children: bool                  # a program: no gate of its own
    proposal_pending: bool              # a materialization proposal awaits a decision
    proposal_tasks: int                 # sub-issues that Accept would create
    dispatch: str                       # product policy `dispatch`: off | propose | auto
    session_model: str                  # product policy `default_model` (the cost line)
    tick_seconds: int
```

### 3.2 Output

```python
@dataclass(frozen=True)
class Step:
    text: str
    spawns_session: bool = False        # the cost flag (D4)
    writes_github: bool = False

@dataclass(frozen=True)
class Consequence:                      # one human control
    available: bool
    unavailable_reason: str | None      # why the control is disabled, when it is
    immediate: tuple[Step, ...]         # what the tap itself changes
    then: tuple[Step, ...]              # what the loop does afterwards
    when: str                           # "now" | "next_tick" | "never"
    reversal: str | None                # GateSpec.reverse where the tap resolves a gate
    gate: str | None                    # gate id the tap resolves, for the registry link

@dataclass(frozen=True)
class MachineOutlook:                   # what happens with NO human action
    actor: str                          # "machine" | "you" | "nobody"
    when: str
    summary: str
    steps: tuple[Step, ...]
    because: str                        # the tolerance / kill-switch / mode explanation

@dataclass(frozen=True)
class Guidance:
    machine: MachineOutlook | None      # None for a program
    actions: dict[str, Consequence]     # advance, step_back, unblock, materialize_accept, materialize_reject
    tolerance: dict                     # {effective, configured, autoresolveEnabled, synthesizerMode}
    tick_seconds: int
```

### 3.3 How consequences are computed — simulate, do not narrate

The core is one function, `describe(action, ctx) -> tuple[Step, ...]`, that turns a
`StageAction` into steps. Every `then` list is produced by applying the tap to a *copy* of
the view and running the real `next_action` on it:

| Control | Post-tap view | `immediate` | `then` |
|---|---|---|---|
| advance | `stage=advance_stage(stage)`, `status=active` | "Moves the gate to *{next}*." | `describe(next_action(view', arts, mode, tol))` |
| step_back | `stage=step_back_stage(stage)`, `status=active` | "Moves the gate back to *{prev}*." | same |
| unblock | `status=awaiting_human` | "Clears the block; the gate stays at *{stage}*." | same |
| materialize_accept | `stage=build`, `status=active`, `epic="pending"` | "Creates the epic and *N* sub-issues on GitHub (cannot be undone here)." (writes_github) · "Moves the gate to Build." · "Sub-issues land on the board already classified." | same |
| materialize_reject | unchanged view | "Rejects this decomposition; nothing is created." | `describe(next_action(view, …))` — which is `Materialize` again, i.e. a new decompose session |

`describe` covers the closed union in one place:

- `Propose(kind)` → one step, `spawns_session=True`: "Spawns a *{kind}* session on
  *{session_model}* that reads the merged *{upstream}* and opens a PR adding
  `docs/sdlc/<slug>/{kind}.md`." (`upstream` from `STAGE_ARTIFACT` order; path from
  `artifact_path`, moved to a pure `server/sdlc/paths.py` because `prompts.py` imports
  session code; `prompts.py` re-exports it.)
- `AwaitApproval(kind)` → PR open: "The *{kind}* PR is waiting for you to review and merge it;
  under *{tol}* the loop does not merge it." Merged, not advanced: "The *{kind}* is merged;
  tap Advance to move to *{next}*."
- `MergeArtifact(kind, pr)` → `writes_github=True`: "Arms GitHub auto-merge on the *{kind}* PR
  #{pr}; CI stays the gate." reversal from `GATE_BY_ID["artifact.merge"].reverse`.
- `Advance(to)` → "Advances to *{to}* automatically." followed by **one** level of lookahead:
  `describe(next_action(advanced view))`, so "advances to plan, then spawns a plan session" reads
  as one story. Lookahead is bounded at one to keep the output finite and the copy short.
- `Materialize()` → pending proposal: actor *you*: "A decomposition is waiting for your Accept
  or Reject." No proposal: `spawns_session=True`: "Spawns a decompose session that proposes an
  epic and sub-issues for you to accept or reject."
- `HandoffToBuild(issue)` → the synthesizer loop takes no action here (its `_run` has no branch
  for it); the board does. "Nothing more from the roadmap loop. The epic's sub-issues are on the
  board; the Backlog Runner *{dispatch sentence}*." where the dispatch sentence is the same one
  §5 uses for the inbox.
- `Done()` → "Nothing left to do."
- `NeedsHuman(reason)` → mapped, not echoed: blocked → "Blocked: unblock or step back.";
  mode off → "SDLC automation is off: nothing moves until it is turned on under Delivery →
  Automation."; deploy/maintain → "Deploy and maintain are carried by the pipeline and control
  bands, not the roadmap loop."; build without issue → the engine's reason verbatim.

`MachineOutlook.actor` is `machine` when the first step spawns or writes, `you` when the
outlook is an `AwaitApproval`/pending-proposal/`NeedsHuman(blocked)`, `nobody` for `Done` and
the mode-off case. `when` is `next_tick` for any machine step, `never` otherwise.

`because` is one sentence: when `configured_tolerance != tolerance`, "Risk tolerance is
*{configured}*, but auto-resolve is switched off for this deployment, so every gate waits
for you."; otherwise "Risk tolerance *{tol}*: {the gates it covers, from
`gates.auto_resolves`, as labels}." or "…: every gate waits for you." under cautious.

### 3.4 Availability

`Consequence.available` and `unavailable_reason` reproduce the endpoint's 409 rules so the
button tooltip and the server agree by construction: advance/step_back refuse a program
("A program is driven by its phases; advance each phase."), a blocked row ("Clear the block
first."), `done` / first stage; unblock refuses a non-blocked row and a pending proposal
("Accept or reject the pending materialization first."); materialize controls exist only while
a proposal is pending. The existing hand-written `title=` strings on the page are replaced by
these.

## 4. API

### 4.1 `GET /api/sdlc/initiatives/{slug}` (changed)

- `nextAction` is computed under the effective tolerance (D3). The kill switch is read via
  `get_settings().gate_autoresolve_enabled`; the synthesizer mode through **one** module-level
  reader in `server/automations/sdlc_synthesizer.py` (extracted from the orchestrator's
  `_sdlc_synth_read_config` so the API and the loop fold the mode identically; the orchestrator
  method becomes a thin call to it). The product policy is `effective_policy(project.
  automation_policy)`; an unlinked initiative reads as defaults.
- New key `guidance`: the `Guidance` object, camelCased. `machine` is `null` and every action is
  unavailable for a program, mirroring the existing `nextAction = None` rule.
- `InitiativeView.epic_total/epic_done` are filled from the same `_epic_progress_batch` the
  loop uses, so the `stage.advance_built` gate explains identically in both places.

### 4.2 `GET /api/sdlc/gates` (new)

```json
{
  "gates": [{"id": "artifact.merge", "label": "…", "risk": "low", "action": "…", "reverse": "…",
             "autoResolvesUnder": {"cautious": false, "balanced": true, "bold": true}}, …],
  "tolerances": ["cautious", "balanced", "bold"],
  "autoresolveEnabled": true
}
```

Built from `GATES` and `auto_resolves`; `risk: null` rows (dispatch, triage) carry all-false
and their `action` already names the policy knob that owns them.

### 4.3 PM triage proposals (changed)

`GET /api/pm/triage` rows gain `guidance: {accept: Consequence, reject: Consequence}` from a
pure `server/pm/explain.py` over the proposal and the owning product's effective policy
(policies loaded once per request through `load_policies`). Accept's `immediate` names the
capability and score it files under (or "with no capability" for a no-fit accept); its `then`
is the **dispatch sentence**, the single phrasing shared with §3.3's `HandoffToBuild`:

- `off` → "Nothing is dispatched: this product's Dispatch is off."
- `propose` → "The Backlog Runner may include it in a proposed batch; you approve each batch."
- `auto` → "The Backlog Runner may dispatch it to a coding session once it is at or above
  *{queue_min_status}* on the board." (`spawns_session=True`)

Reject: "Marks it needs-human; it leaves the inbox and nothing is dispatched." `reversal` on
accept is the undo window (`GATE_BY_ID["triage.accept"].reverse`).

## 5. Frontend

- **Types and fetchers** (`web/src/api/sdlc.ts`, `triage.ts`): `Guidance`, `Consequence`,
  `Step`, `fetchGates`; hook `useSdlcGates` in `hooks.ts`. `guidance` is optional on
  `Initiative` and `TriageProposal` so a client ahead of its server still renders.
- **One rendering component**, `web/src/features/roadmap/guidance/ConsequenceList.tsx`: renders
  a `Consequence` as "Now:" steps and "Then:" steps, a `Badge` per flag ("starts a session",
  "writes to GitHub"), the `when` rendered from `tickSeconds` ("in about a minute"), and the
  reversal line. Used by every dialog and the inbox pane; nothing else formats a consequence.
- **Initiative page** (`InitiativePage.tsx`): the four confirm dialogs (Advance, Step back,
  Unblock, Materialize) render `ConsequenceList` as their body; the static `description`
  strings remain only as the fallback when `guidance` is absent. Button `title=` tooltips read
  `unavailableReason`. The Track tab's `nextActionLabel` paragraph becomes a **"What happens
  next"** card: an actor chip (You / Automatic / Nothing), the `summary`, the `because` line,
  the steps with their flags. `NEXT_ACTION_LABEL` / `nextActionLabel` and their tests are
  deleted.
- **Product automation card** (`AutomationPolicyCard.tsx`): under the fields, a **Gates**
  section listing every gate with its risk and, for the product's *effective* tolerance,
  "Automatic" / "Waits for you" / "Policy-owned" (from `autoResolvesUnder` and the row's
  `risk`), plus the gate's `action` sentence. When `autoresolveEnabled` is false and the
  configured tolerance is not cautious, a `Banner tone="info"` says every gate waits for you
  regardless. Loading/error follow the card's existing "Checking… / unavailable" idiom.
- **Inbox detail pane** (`ProposalDetail.tsx`): between the score fields and the buttons, two
  short `ConsequenceList`s headed "Accept" and "Reject". Keyboard accepts (`a`, `A`) are not
  gated behind a confirm (the inbox's speed is the point); the pane's text is what makes them
  informed.

## 6. Error handling

- The explain modules are total: unknown stages or statuses yield a `NeedsHuman`-shaped
  outlook with the engine's reason, never an exception; `effective_policy` never raises.
- The detail endpoint treats a failed synthesizer-mode read as `off` (the loop's own rule) and
  says so in `because`.
- Client: a missing `guidance` falls back to today's copy; a failed `/api/sdlc/gates` fetch
  shows the card's "unavailable" line.

## 7. Testing

- `server/tests/test_sdlc_explain.py`: table-driven over (stage, artifact state, tolerance,
  mode, kill switch, proposal pending) asserting actor / when / spawns / writes per control;
  the **simulation property** — for every control, `then` equals `describe(next_action(post-tap
  view))`; lookahead bounded at one; program and blocked availability; `because` wording when
  configured ≠ effective.
- `server/tests/test_sdlc_api.py`: detail carries `guidance`; `nextAction` under a `balanced`
  product returns `advance` where the old code returned `await_approval`; the kill switch off
  surfaces `configured=balanced, effective=cautious`; `GET /api/sdlc/gates` shape; a program's
  actions all unavailable.
- PM triage API tests: `guidance` per dispatch mode; no-fit accept wording.
- Web (`vitest`, full run — many files mock `@/api/hooks`): `InitiativePage.test.tsx` dialog
  bodies and tooltips from guidance plus the no-guidance fallback; `ConsequenceList` flags and
  the tick-derived "about a minute"; `AutomationPolicyCard` gate rows and the kill-switch
  banner; `ProposalDetail` accept/reject lines; `vocab.test.ts` updated for the purge.
- Gates: `ruff`, `mypy server/`, both pytest suites, `typecheck`/`lint`/`test`/`build` in
  `web/`, brand ratchet.

## 8. Out of scope (follow-ups, filed as issues by the plan)

- **Front-chain cap occupancy.** "Queued behind the product's cap" is a real "why did nothing
  happen" case, but explaining it needs the loop's live-session count. v1 states the cap's
  value only.
- **Assistant drawer "explain this page" tool** — reads the same `guidance`; layered later.
- **Bulk-accept bar** in the inbox and the Delivery synthesizer card.
