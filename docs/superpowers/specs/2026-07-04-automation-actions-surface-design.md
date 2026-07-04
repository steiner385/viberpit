# Automation Actions Surface — Design

**Date:** 2026-07-04
**Status:** Approved design, pending implementation plan
**Scope:** Generalize the "detected item → manual trigger" pattern across automations, with visible trigger feedback.

## Problem

The Automation tab already lets a user manually trigger recovery actions on detected
items — but only for PR Doctor, and it does so through a **feedback vacuum**:

1. **Silent outcomes.** `orch.pr_doctor_fix()` returns a meaningful status string
   (`spawned` / `sent` / `failed` / `not_actionable` / `no_pr` / `updated`), but
   `usePrDoctorFix.onSuccess` (`web/src/api/hooks.ts:697`) discards it and only
   invalidates the query. When a click returns `failed` (e.g. *"spawn failed — all
   accounts at/above usage ceiling"*), the UI shows nothing; the PR stays in the list
   because it is still failing, so the button looks inert. → user perception:
   *"clicks aren't producing sessions."*
2. **No affordance.** No toast, no inline result, and `fix.isPending` disables **every**
   button at once (shared mutation state). → user perception: *"they don't feel like
   buttons."*
3. **Not reusable.** `ActionablePrsSection` renders only `if (a.id === 'pr-doctor')`.
   Meanwhile `backlog-runner` grew its own parallel `POST /backlog-runner/implement`
   and `cicd-doctor` grew its own `GET /cicd-doctor/findings`. Three divergent one-off
   surfaces for one concept — the exact anti-pattern the constitution's Principle VI
   (`eliminating-parallel-implementations`) forbids.

## Goal

One reusable **actionable-items → manual-trigger** surface that any automation can
expose, where the trigger's outcome is actually surfaced to the user. Ship it with
two live consumers (pr-doctor, backlog-runner) migrated onto it; leave cicd-doctor as
a natural next consumer.

Non-goals (explicitly deferred — YAGNI): action-audit log, optimistic UI, bulk
"fix all N", and usage-aware auto-deferral/queuing. This design *surfaces* the usage
ceiling as a message; it does not queue around it.

## Backend contract (`server/automations/base.py`)

Three new value types, uniform across every automation:

```python
@dataclass
class ActionSpec:
    kind: str                 # "failing_ci" | "resolve_conflicts" | "implement" | ...
    label: str                # "Fix CI"
    style: str = "default"    # "default" | "risky"   (risky → confirm + distinct color)

@dataclass
class ActionableItem:
    id: str                   # stable key, e.g. "cairnea/KinDash#11006"
    title: str
    subtitle: str | None = None
    url: str | None = None
    actions: list[ActionSpec] = field(default_factory=list)
    def to_dict(self) -> dict: ...

@dataclass
class TriggerResult:
    status: str               # machine-readable, unchanged from today's status strings
    level: str                # "ok" | "warn" | "error"
    message: str              # human-readable — surfaced in toast + inline row outcome
    def to_dict(self) -> dict: ...
```

`level`/`message` are the feedback fix. Mapping examples for PR Doctor:

| status          | level | message                                             |
|-----------------|-------|-----------------------------------------------------|
| `spawned`       | ok    | "Spawned fix session"                               |
| `sent`          | ok    | "Sent fix prompt to live session"                   |
| `updated`       | ok    | "Branch updated"                                    |
| `failed` (ceiling) | warn  | "All accounts at/above usage ceiling — try later" |
| `failed` (other)| error | "Fix action failed — see feed"                      |
| `not_actionable`| warn  | "PR is no longer actionable"                        |
| `no_pr`         | warn  | "PR not found in poll cache"                        |

The ceiling-vs-other distinction requires `pr_doctor_fix` / the shared session
executor to report *why* a spawn failed (currently it returns a bare `failed`). The
executor already knows — the "all accounts at/above usage ceiling" case is a distinct
branch that emits that feed line. Thread that reason out as part of the return so the
mapping above can pick `warn` + the specific message.

### Provider dispatch (`server/sessions/orchestrator.py`)

Logic stays in the orchestrator (it needs the poll cache, account state, and
`spawn_session`). Introduce a dispatch table keyed by automation id:

```python
async def automation_actionable(self, automation_id: str) -> list[dict]:
    provider = self._action_providers.get(automation_id)
    return [it.to_dict() for it in await provider.actionable()] if provider else []

async def automation_trigger(self, automation_id: str, item_id: str, kind: str) -> dict:
    provider = self._action_providers.get(automation_id)   # None → 404 at API layer
    return (await provider.trigger(item_id, kind)).to_dict()
```

- **pr-doctor provider** wraps the existing `pr_doctor_actionable` / `pr_doctor_fix`
  logic, now returning `ActionableItem` / `TriggerResult`. `item_id` is `"repo#number"`;
  the provider parses it back to `(repo, number)`. Kinds: `failing_ci`, `merge_conflict`
  (risky), `out_of_date`.
- **backlog-runner provider** wraps `backlog_runner_implement`. `item_id` = primary key;
  single action kind `implement`. Statuses map: `spawned`→ok, `exists`→warn
  ("Already running"), `claim_failed`→error, `no_account`→warn (ceiling-style message),
  `unknown`→ the API 404.

The old orchestrator methods `pr_doctor_actionable` / `pr_doctor_fix` /
`backlog_runner_implement` are **replaced** by provider methods and **deleted** in the
same PR (constitution VI — no half-dead code). `cicd_doctor_findings` is untouched
(read-only; not yet an action provider).

### API (`server/api/automations.py`)

Two generic routes replace the three one-offs:

- `GET  /api/automations/{id}/actionable` → `list[ActionableItem.to_dict()]`
- `POST /api/automations/{id}/trigger` — body `{item_id: str, kind: str}` → `TriggerResult.to_dict()`

Validation: unknown automation → 404 (existing `_automation_or_404`); an automation
with no provider → 404; an unknown `kind` for that provider → 422 (provider declares
its valid kinds). Auth unchanged (`current_identity`).

**Deleted in the same PR:** `GET /pr-doctor/actionable`, `POST /pr-doctor/fix`,
`POST /backlog-runner/implement`, and the `PrDoctorFixBody` / `BacklogImplementBody`
models. `GET /cicd-doctor/findings` stays.

## Frontend

### Types + API (`web/src/api/automations.ts`)

```ts
export interface ActionSpec   { kind: string; label: string; style: 'default' | 'risky' }
export interface ActionableItem { id: string; title: string; subtitle?: string; url?: string; actions: ActionSpec[] }
export interface TriggerResult  { status: string; level: 'ok' | 'warn' | 'error'; message: string }

export const fetchAutomationActionable = (id: string) => get<ActionableItem[]>(`/api/automations/${id}/actionable`)
export const postAutomationTrigger = (id: string, body: {item_id: string; kind: string}) =>
  post<TriggerResult>(`/api/automations/${id}/trigger`, body)
```

Remove `PrDoctorActionablePr`, `PrDoctorKind`, `postPrDoctorFix`, `fetchPrDoctorActionable`.

### Hooks (`web/src/api/hooks.ts`)

```ts
export function useAutomationActionable(id: string, enabled = true) {
  return useQuery<ActionableItem[]>({
    queryKey: QK.automationActionable(id), queryFn: () => fetchAutomationActionable(id),
    enabled, staleTime: 10_000 })
}
export function useAutomationTrigger(id: string) {
  const qc = useQueryClient()
  return useMutation<TriggerResult, ApiRequestError, {item_id: string; kind: string}>({
    mutationFn: (body) => postAutomationTrigger(id, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: QK.automationActionable(id) }),
  })
}
```

`QK.prDoctorActionable` → `QK.automationActionable(id)` (per-automation key). Remove
`usePrDoctorActionable` / `usePrDoctorFix`.

### Component (`web/src/components/AutomationCard.tsx`)

Replace `ActionablePrsSection` with a reusable `<AutomationActions automationId={a.id} />`,
rendered for **any** automation whose actionable list is non-empty (drop the hardcoded
`a.id === 'pr-doctor'` gate; also fold the backlog-runner implement UI into it). Behavior:

- One `<button>` per `ActionSpec`; label = `action.label`; class from `action.style`
  (`default` → neutral, hover-blue; `risky` → red).
- **Per-action pending** keyed by `(item.id, action.kind)` — only the clicked button
  shows a spinner and disables; siblings stay live. (Track a local `Set` / the mutation
  `variables`, not the shared `isPending`.)
- **On result:** show a toast **and** a transient inline outcome badge on the row,
  colored by `result.level` (ok green / warn amber / error red) with `result.message`.
  Inline badge auto-clears after a few seconds or on the next list refresh.
- **Risky confirm:** a `risky` action requires a one-step inline confirm — first click
  swaps the button to "Confirm <label>?"; second click fires. `default` actions fire on
  first click. (Decision: risky = `resolve_conflicts`/rebase, which mutate the branch.)

**Toast:** reuse the app's existing toast/notification mechanism — verify what
`web/src/` already provides during planning and route through it; do **not** add a
parallel toaster (constitution VI). If none exists, that discovery becomes an explicit
plan task, not an ad-hoc addition.

`<AutomationActions>` is self-contained within `AutomationCard` and unrelated to
`SessionList`, so the "~16 `@/api/hooks` vi.mock lists" hazard does not apply.

## Testing

- **Backend** `server/tests/test_automation_actions_api.py` (new): auth 401; unknown
  automation → 404; automation without provider → 404; unknown kind → 422; `actionable`
  returns the provider list; `trigger` returns a `TriggerResult` shape with correct
  `level`/`message` for ok / ceiling-warn / error.
- **Backend** rewrite `server/tests/test_pr_doctor_actions_api.py` onto the generic
  routes (keep the `_FakeOrch` dispatch-assertion style); update backlog-runner tests
  that hit `/backlog-runner/implement`.
- **Frontend** rewrite the "Actionable PRs" block in
  `web/src/components/AutomationCard.test.tsx` for `<AutomationActions>`: renders a
  button per action; per-button pending (sibling stays enabled); toast + inline outcome
  on ok/warn/error; risky action requires the confirm step; empty state; renders for a
  second automation id (proves generality).

## Files touched

| File | Change |
|------|--------|
| `server/automations/base.py` | + `ActionSpec`, `ActionableItem`, `TriggerResult` |
| `server/sessions/orchestrator.py` | + provider dispatch + pr-doctor/backlog providers; − old methods |
| `server/api/automations.py` | + generic `actionable`/`trigger` routes; − 3 one-off routes + 2 body models |
| `web/src/api/automations.ts` | + uniform types/fetchers; − pr-doctor-specific |
| `web/src/api/hooks.ts` | + `useAutomationActionable`/`useAutomationTrigger`; − pr-doctor-specific |
| `web/src/components/AutomationCard.tsx` | `ActionablePrsSection` → `<AutomationActions>`; drop id gate; fold backlog UI |
| `server/tests/test_automation_actions_api.py` | new |
| `server/tests/test_pr_doctor_actions_api.py` | rewrite onto generic path |
| `web/src/components/AutomationCard.test.tsx` | rewrite actionable block |
| backlog-runner API test(s) | update to generic path |

## Rollout / follow-ups

- **This PR:** shared contract + pr-doctor + backlog-runner migrated + feedback UI.
- **Follow-up:** make `cicd-doctor` an action provider (e.g. finding-level "open fix
  session" / "file issue now"), reusing this surface with zero new UI.
