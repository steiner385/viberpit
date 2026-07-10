# Per-Automation Session Model Selection (with intelligent "auto")

**Date:** 2026-07-10
**Status:** Approved
**Scope:** backlog-runner, pr-doctor (the only automations that spawn sessions)

## Problem

Automation-spawned sessions always run on the account's default model. The user
wants each spawning automation to carry a **Session model** dropdown, including
an **auto** option that assesses the complexity of the job and picks the model
tier intelligently — trivial chores on cheap models, hard work on top-tier ones.

## Decisions (user-confirmed)

- **Which automations:** backlog-runner and pr-doctor only. Autopilot, CI/CD
  Doctor, and Archive Audit never spawn sessions; giving them the setting would
  be dead config (constitution Principle VI). They inherit the pattern if/when
  they grow spawn paths.
- **Auto tier pool:** `haiku`, `sonnet`, `opus`, `fable`.
- **Default value:** `auto` — automations begin assessing and picking models as
  soon as this ships.

## Design

### 1. Config field (no new frontend code, no DB migration)

Each of the two automations adds one field to its `config_schema`:

```python
ConfigField(key="default_model", label="Session model", type="enum",
            default="auto", choices=model_choices(),
            help="Model for sessions this automation spawns. 'auto' assesses "
                 "job complexity and picks a tier; 'default' uses the "
                 "account's default model.")
```

`model_choices()` lives in `server/sessions/model_catalog.py` and returns
`["auto"] + ALIASES` — i.e. `auto, default, sonnet, opus, haiku, fable, best,
sonnet[1m], opus[1m], fable[1m], opusplan`. The catalog stays the single source
of truth; a future alias added there appears in every automation dropdown
automatically.

- Frontend: `AutomationCard.tsx` already renders `type="enum"` fields as a
  native `<select>`; nothing to add.
- Persistence: `Automation.config` is a JSON blob; `PATCH
  /api/automations/{id}` already validates enum values via
  `base.validate_config`. No migration.

**Semantics of the value at spawn time:**

| value | behavior |
|---|---|
| `"default"` | pass `model=None` → account default (pre-feature behavior) |
| any catalog alias | pass through as `model=<alias>` |
| `"auto"` | run the assessor (below); pass its pick, or `None` if it fails |

### 2. Auto assessor — `server/automations/model_pick.py`

One shared module, modeled on the proven `rank_issues` pattern
(`backlog_runner.py`): a one-shot, tool-less **haiku** query via
`claude_agent_sdk.query`, with an injected `_query_fn` seam so tests never call
the real model.

```python
AUTO_MODEL_POOL = ["haiku", "sonnet", "opus", "fable"]

async def assess_model(task_summary: str, *, _query_fn=None,
                       _config_dir=None, _cwd=None) -> str | None: ...
```

- **Prompt:** system prompt defines the tiers — haiku = trivial/mechanical
  (typo fixes, config bumps, single-line changes), sonnet = typical
  single-concern change, opus = hard multi-file work, fable = hardest
  architectural / cross-cutting work — and instructs the model to reply with
  EXACTLY one tier name, nothing else.
- **Input:** a short caller-built task summary (see wiring).
- **Parsing:** strict — the (stripped, lowercased) reply must be a member of
  `AUTO_MODEL_POOL`.
- **Fail-open invariant:** any exception, empty reply, or non-pool reply
  returns `None`; the spawn proceeds with the account default. Auto can never
  block or fail a spawn — it can only refine it.

### 3. Wiring — three spawn sites in `server/sessions/orchestrator.py`

A small orchestrator helper resolves the effective model:

```python
async def _resolve_automation_model(self, config: dict, task_summary: str) -> str | None
```

- `_backlog_spawn` (~line 1394): summary = the batch's issue refs + titles
  (already available from the batch/candidates; bodies not fetched — titles
  are what `rank_issues` already works from). Pass `model=` into
  `self._backlog_ctx.spawn_session(...)`. Reads config via
  `_backlog_read_config()`.
- PR-doctor fix spawn (~line 2286): summary = fix kind (branch-update /
  CI-fix / conflict) + PR title + failing check names where available. Reads
  config via `_pr_doctor_config()`.
- PR-doctor consolidation spawn (~line 2399): summary = "consolidate N PRs" +
  PR titles.

`spawn_session(**create_kwargs)` already forwards `model` to
`create_session`, which already accepts and applies it (`driver.set_model`).

**Observability:** the chosen model and its provenance are added to the
`spawned` event detail: `{"model": "opus", "model_source": "auto"}` (or
`"pinned"`; omitted entirely when the account default is used). The automation
activity feed then shows why a session got the model it did.

### 4. Error handling summary

- Assessor failure → `None` → account default (warn-level log, spawn proceeds).
- Invalid persisted config value (e.g. catalog alias removed later) →
  `validate_config` already rejects writes; reads treat an unknown value as
  `"default"` (defensive, logged).
- No timeout regression: the assessor call happens inside the existing spawn
  paths, which already tolerate multi-second work (clone resolution).

### 5. Testing

- `server/tests/test_model_pick.py`: canned `_query_fn` returning each tier;
  whitespace/case-mangled reply; garbage reply → `None`; raising fn → `None`.
- Spawn-site tests (extend existing backlog/pr-doctor orchestrator tests):
  config `"auto"` → assessor stub consulted and its pick passed as `model=`;
  `"default"` → `model=None` and assessor NOT called; pinned `"opus"` →
  passed through and assessor NOT called; assessor returning `None` →
  `model=None`.
- Schema tests: `default_model` present with `default="auto"` and choices =
  `["auto"] + ALIASES`; `validate_config` rejects a non-choice value.
- Frontend: enum rendering already covered by AutomationCard tests; no new
  web code anticipated.

## Alternatives rejected

- **Heuristics only (labels/diff size):** cheaper, but not the intelligent
  assessment requested; label conventions vary per repo.
- **Folding assessment into `rank_issues`:** saves one haiku call for the
  backlog path but couples ranking to model choice and leaves pr-doctor
  uncovered.
- **Dropdown on all five automations:** dead config on the three
  non-spawning automations.
