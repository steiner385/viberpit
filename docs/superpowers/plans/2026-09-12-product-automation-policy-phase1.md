# Product automation policy — Phase 1 (policy + runner scoping) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every product an automation policy (a `ConfigField` schema stored as JSON on `Project`, edited from the product page) and make the Backlog Runner read it per product — which products it dispatches for, how far down their boards it reaches, their in-flight caps and weights, their attempt caps and model — retiring the runner's global repo knobs with a one-shot seed so nothing changes behaviour at the flip.

**Architecture:** One new module `server/automations/product_policy.py` owns the schema (`PRODUCT_POLICY_SCHEMA`), the validated read (`effective_policy`) and the phase-1 vocabulary (`DISPATCH_MODES`). The runner's orchestrator seams (`_backlog_query_queue`, `_backlog_select_batch`, `_backlog_spawn`, `_backlog_count_candidates`) read `policies_by_slug` from the DB in the same session as the queue and translate product knobs into the repo-keyed shapes the pure selection code already takes — `backlog_selection.py`/`backlog_grouping.py` do not change. The runner's global modes become `off | on`; per product, `dispatch` is `off | propose | auto`. The web gets `ConfigInput` as an exported shared component and an **Automation** card on the product Overview saving through `PATCH /api/pm/projects/{slug}/policy`.

**Tech Stack:** FastAPI + SQLAlchemy async (SQLite dev, Postgres prod), the `ConfigField`/`validate_config` machinery in `server/automations/base.py`, React 19 + react-query + Tailwind v4 tokens, pytest, vitest.

**Spec:** `docs/superpowers/specs/2026-09-12-product-automation-policy-design.md` — D1, D3, D4, D10 (this phase), §4 (the policy; phase-1 fields only — `give_up_after_bounces` and `triage_auto_accept` arrive with phases 3 and 2), §5 (the runner), §9 (product Overview card, status lines), §10, §11 phase 1, §12, §13.

## Global Constraints

- New columns nullable (`_reconcile_columns` adds them on both dialects); migration `0042_product_automation_policy` chained off `0041_initiative_nesting_route_labels` (master gained its own 0041 in #1597), recorded anyway. No new table in this phase → no `_FK_LINKS`/`_REQUIRED_EXTRAS`/`RECONCILE_EXEMPT` changes; state so in the report.
- DB clock: never `func.now()`; the models' `_utcnow` default / `server/db/clock.py`.
- Every mutation audits (`record_audit`) with the acting identity; the runner's seed audits with identity `backlog-runner`.
- Constitution VI: ONE schema (`PRODUCT_POLICY_SCHEMA`), ONE validator (`validate_config`), ONE web renderer (`ConfigInput`), ONE "which products are on" reading (`effective_policy(...)["dispatch"] != "off"`).
- Server gate before every commit: `/home/tony/GitHub/viberpit/.venv/bin/ruff check src/ server/`; `/home/tony/GitHub/viberpit/.venv/bin/python -m mypy server/ --ignore-missing-imports`; the named test files via `env -u CCSWITCH_SESSIOND_ENDPOINT -u CCSWITCH_SESSIOND_CLIENT_CERT -u CCSWITCH_SESSIOND_CLIENT_KEY -u CCSWITCH_SESSIOND_CA -u CCSWITCH_DAEMON_ROLE -u CCSWITCH_TAILNET_ALLOWLIST -u VIBERPIT_SESSIOND_ENDPOINT -u VIBERPIT_SESSIOND_CLIENT_CERT -u VIBERPIT_SESSIOND_CLIENT_KEY -u VIBERPIT_SESSIOND_CA -u VIBERPIT_DAEMON_ROLE -u VIBERPIT_TAILNET_ALLOWLIST CCSWITCH_DEV=1 /home/tony/GitHub/viberpit/.venv/bin/python -m pytest <files> -q`. The full server and core suites run once on the final head, by the controller.
- Web gate on the web task, every rc checked, never `| tail`: `(cd web && npm run typecheck; echo rc=$?; npm run lint; echo rc=$?; npm run test > /tmp/pap1-tN-vitest.log 2>&1; echo rc=$?; grep -E "Test Files|Tests " /tmp/pap1-tN-vitest.log; grep -c Unhandled /tmp/pap1-tN-vitest.log; npm run build > /tmp/pap1-tN-build.log 2>&1; echo rc=$?); rm -rf web/dist`.
- Commits: one per task, `<type>(<scope>): policy phase 1 task N — <what>` / blank / paragraph / blank / `Refs #1521` / blank / `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`; via `git commit -F /tmp/pap1-tN.msg`. Never push, never open a PR, never rebase, never `git stash`.
- Run from the worktree root with absolute paths or `(cd web && …)`.

## File structure

| File | Responsibility |
|---|---|
| `server/automations/product_policy.py` | `DISPATCH_MODES`, `PRODUCT_POLICY_SCHEMA`, `effective_policy(raw) -> dict`, `policies_by_slug(db) -> dict[str, dict]` (sync-session helper for the queue loader), `load_policies(db_async)` |
| `server/db/models.py` | `Project.automation_policy` (JSON), `Project.automation_policy_updated_at` |
| `server/db/migrations/versions/0042_product_automation_policy.py` | the record |
| `server/api/pm.py` | `automationPolicy` in `_project_to_dict`; `GET /pm/policy-schema`; `PATCH /pm/projects/{slug}/policy` |
| `server/automations/backlog_runner.py` | modes `off | on`; retired keys; `status()` per-product lines; loop `seed_policies` seam called once per process at the first tick |
| `server/sessions/orchestrator.py` | `_backlog_query_queue` scopes and ranks per product; `_backlog_select_batch` translates policies to repo-keyed weights/caps/order; `_backlog_spawn` reads the product's `default_model` and stores `product` in the claim; `_sweep_one` reads `max_attempts` per product via the claim's `product`; `_backlog_count_candidates` returns per-product counts; `_backlog_seed_policies` |
| `web/src/features/delivery/automation/ConfigInput.tsx` | `ConfigInput` (and its repo sub-inputs) moved out of `ConfigPanel.tsx`, exported |
| `web/src/api/pm.ts`, `web/src/api/hooks.ts` | `Project.automationPolicy`, `fetchPolicySchema`, `updateProductPolicy`, `usePolicySchema`, `useUpdateProductPolicy` |
| `web/src/features/roadmap/detail/AutomationPolicyCard.tsx` | the product Overview card |
| `docs/product/features/product-management-layer.md`, `docs/product/features/backlog-runner.md` (or the runner's doc — `grep -l "Backlog Runner" docs/product/features/*.md`) | the policy, the retired keys, the seed |

Task order: 1 (schema, model, API) → 2 (runner) → 3 (web) → 4 (docs + retired-key cleanup of the runner doc). 3 depends on 1 only, so 2 and 3 may run in parallel once 1 lands (disjoint files).

---

### Task 1: The policy schema, the column, the API

**Files:**
- Create: `server/automations/product_policy.py`
- Modify: `server/db/models.py` (`Project`, ~:1406-1425)
- Create: `server/db/migrations/versions/0042_product_automation_policy.py`
- Modify: `server/api/pm.py` (`_project_to_dict` :96-107; routes after `get_project` :271)
- Test: `server/tests/test_product_policy.py` (new), `server/tests/test_pm_api.py` (append)

**Interfaces:**
- Consumes: `ConfigField`, `validate_config(schema, config, *, retired=())` (`server/automations/base.py:15-28, :105`), `MIN_STATUS_OPTIONS` (`server/pm/queue.py:46`), `STATUS_READY` (`server/pm/desired_state.py`), `AUTOMATION_MODEL_CHOICES` (grep `server/automations/` for its definition — the same list the runner's `default_model` field uses), `_load_project`, `record_audit`.
- Produces:

```python
# server/automations/product_policy.py
DISPATCH_MODES: tuple[str, ...] = ("off", "propose", "auto")

PRODUCT_POLICY_SCHEMA: list[ConfigField] = [
    ConfigField(key="dispatch", label="Dispatch", type="enum", default="off",
                choices=list(DISPATCH_MODES),
                help="Whether the Backlog Runner works this product's board: 'propose' announces a batch without spawning, 'auto' spawns."),
    ConfigField(key="queue_min_status", label="Minimum board status", type="enum",
                default=STATUS_READY, choices=list(MIN_STATUS_OPTIONS),
                help="How far down the board the runner may reach for this product."),
    ConfigField(key="max_in_flight", label="Max in flight", type="int", default=1,
                help="This product's concurrent runner sessions."),
    ConfigField(key="weight", label="Weight", type="int", default=1,
                help="This product's share when a freed slot is contested (the 'weighted' strategy)."),
    ConfigField(key="max_attempts", label="Max attempts per issue", type="int", default=2),
    ConfigField(key="default_model", label="Session model", type="enum", default="auto",
                choices=AUTOMATION_MODEL_CHOICES,
                help="Model for this product's sessions; 'auto' assesses each job."),
    ConfigField(key="respect_dependencies", label="Respect issue dependencies", type="bool", default=True),
]

def effective_policy(raw: dict | None) -> dict:
    """The validated policy with every key present. ``None`` (never opted in) and an
    unparseable blob both read as the defaults — dispatch ``off`` — and the latter logs once."""

def policies_by_slug(db: Session) -> dict[str, dict]:
    """slug → effective policy for every Project (sync session; the queue loader's session)."""

async def load_policies(db) -> dict[str, dict]:
    """Async twin for the API/orchestrator paths — same reading, no second rule."""
```

(`weight` is an `int` because `ConfigField.type` has no `number`; the selection code takes `dict[str, float]` and an int coerces.)

- `Project.automation_policy: Mapped[dict | None] = mapped_column(JSON, nullable=True)`, `Project.automation_policy_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)`.
- `_project_to_dict` gains `"automationPolicy": p.automation_policy` (the RAW blob or null — the web renders it through the schema's defaults; `null` means never opted in).
- `GET /api/pm/policy-schema` → `[f.to_dict() for f in PRODUCT_POLICY_SCHEMA]`.
- `PATCH /api/pm/projects/{slug}/policy` body `{policy: dict}` → `validate_config(PRODUCT_POLICY_SCHEMA, body.policy)` (422 `bad_policy` with the ValueError text on failure; unknown keys are an error, not dropped), stores the VALIDATED dict, stamps `automation_policy_updated_at`, audits `pm.project.policy` `{slug, policy}`, returns `_project_to_dict(p)` with repos/edges like `get_project`. `{policy: null}` clears both columns (opt-out entirely) and audits `{slug, policy: null}`.

- [ ] **Step 1: Write the failing tests**

`server/tests/test_product_policy.py`:

```python
"""Product automation policy (spec 2026-09-12 §4): one schema, one validated reading."""
from __future__ import annotations

from server.automations.product_policy import (
    DISPATCH_MODES, PRODUCT_POLICY_SCHEMA, effective_policy,
)


def test_schema_keys_and_defaults():
    keys = [f.key for f in PRODUCT_POLICY_SCHEMA]
    assert keys == ["dispatch", "queue_min_status", "max_in_flight", "weight",
                    "max_attempts", "default_model", "respect_dependencies"]
    assert DISPATCH_MODES == ("off", "propose", "auto")
    assert effective_policy(None) == {
        "dispatch": "off", "queue_min_status": "Ready", "max_in_flight": 1, "weight": 1,
        "max_attempts": 2, "default_model": "auto", "respect_dependencies": True}


def test_effective_policy_fills_defaults_and_falls_back_on_garbage():
    assert effective_policy({"dispatch": "auto", "max_in_flight": 3})["max_in_flight"] == 3
    assert effective_policy({"dispatch": "auto"})["queue_min_status"] == "Ready"
    # An unknown key or a bad enum never takes a product OFF the safe default.
    assert effective_policy({"dispatch": "turbo"})["dispatch"] == "off"
    assert effective_policy({"nope": 1})["dispatch"] == "off"
    assert effective_policy("not a dict")["dispatch"] == "off"  # type: ignore[arg-type]


def test_queue_min_status_choices_are_the_queue_vocabulary():
    from server.pm.queue import MIN_STATUS_OPTIONS
    field = next(f for f in PRODUCT_POLICY_SCHEMA if f.key == "queue_min_status")
    assert field.choices == list(MIN_STATUS_OPTIONS)
```

Append to `server/tests/test_pm_api.py` (it has `_client()` and `IDENTITY_HEADERS`; read `test_project_lifecycle` at `:50` for how a project is created through the API):

```python
def test_policy_schema_and_project_policy_round_trip():
    with _client() as c:
        schema = c.get("/api/pm/policy-schema", headers=IDENTITY_HEADERS)
        assert schema.status_code == 200
        assert [f["key"] for f in schema.json()][:2] == ["dispatch", "queue_min_status"]

        r = c.post("/api/pm/projects", json={"name": "KinDash", "slug": "kindash",
                                             "repos": [{"repo": "cairnea/KinDash", "isPrimary": True}]},
                   headers=IDENTITY_HEADERS)
        assert r.status_code == 201, r.text
        assert r.json()["automationPolicy"] is None

        r = c.patch("/api/pm/projects/kindash/policy",
                    json={"policy": {"dispatch": "auto", "max_in_flight": 2}}, headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        pol = r.json()["automationPolicy"]
        assert pol["dispatch"] == "auto" and pol["max_in_flight"] == 2
        assert pol["queue_min_status"] == "Ready"          # defaults are stored, not implied
        assert c.get("/api/pm/projects/kindash", headers=IDENTITY_HEADERS).json()["automationPolicy"] == pol

        r = c.patch("/api/pm/projects/kindash/policy", json={"policy": {"dispatch": "turbo"}},
                    headers=IDENTITY_HEADERS)
        assert r.status_code == 422 and r.json()["detail"]["error"] == "bad_policy"
        r = c.patch("/api/pm/projects/kindash/policy", json={"policy": {"nope": 1}}, headers=IDENTITY_HEADERS)
        assert r.status_code == 422

        r = c.patch("/api/pm/projects/kindash/policy", json={"policy": None}, headers=IDENTITY_HEADERS)
        assert r.status_code == 200 and r.json()["automationPolicy"] is None
        assert c.patch("/api/pm/projects/nope/policy", json={"policy": {}},
                       headers=IDENTITY_HEADERS).status_code == 404
```

Check `test_pm_api.py` for an `_audits`-style helper; if one exists, assert two `pm.project.policy` rows (`{slug, policy}` then `{slug, policy: None}`).

- [ ] **Step 2: Run to verify they fail** — `… pytest server/tests/test_product_policy.py server/tests/test_pm_api.py -q -k "policy"` → ImportError / 404s.

- [ ] **Step 3: Implement** the module, the columns, the migration (mirror `0040_feature_map_changes.py`'s header: "Nothing runs `alembic upgrade` at deploy time; reconcile adds both nullable columns"), the serializer field, and the two routes (idioms: `get_project` for the response, `create_edge` for the body class + audit; the 422 shape `{"error": "bad_policy", "message": str(e)}`).

- [ ] **Step 4: Run to verify they pass** — the two files plus `server/tests/test_schema_reconcile_coverage.py` and `server/tests/test_postgres_compat.py` (the pure list checks); `ruff`, `mypy`.

- [ ] **Step 5: Commit** — `feat(pm): policy phase 1 task 1 — product automation policy schema, column and API`.

---

### Task 2: The runner reads policies

**Files:**
- Modify: `server/automations/backlog_runner.py` (`BacklogRunner.modes` :157, `config_schema` :160-245, `retired_config_keys` :241, `status()` :243-262, `BacklogRunnerLoop.__init__` :367-, `tick` :445, `_refill` :730, `_sweep_one` ~:1061, `_cache_counts` ~:527-565)
- Modify: `server/sessions/orchestrator.py` (`_backlog_read_config` :4886, `_backlog_query_queue` :5072-5125, `_backlog_select_batch` :5135-5162, `_backlog_repo_load` :5200, `_backlog_count_candidates` :5322, `_backlog_spawn` :5345-5395, the `BacklogRunnerLoop(...)` wiring :1000-1020)
- Test: `server/tests/test_backlog_runner_automation.py`, `server/tests/test_backlog_runner_loop.py`, `server/tests/test_pm_orchestrator_seams.py` or a new `server/tests/test_backlog_policy_seams.py`

**Interfaces (each a precise change; read the current code first):**

1. **Modes and schema.** `BacklogRunner.modes = ("off", "on")`. Remove from `config_schema`: `enabled_repos`, `repo_weights`, `repo_caps`, `queue_min_status`, `max_attempts`, `default_model`, `respect_dependencies`; keep `selection_strategy`, `max_concurrent`, `max_spawns_per_day`, `max_issues_per_pr`, `grouping_interval_minutes`, `needs_human_retry_days`, the engine/codex fields. `retired_config_keys = ("eligible_label", "enabled_repos", "repo_weights", "repo_caps", "queue_min_status", "max_attempts", "default_model", "respect_dependencies")`. `_backlog_read_config` passes `modes=("on",)` — and ALSO returns the raw stored blob's retired values for the seed (add a second seam `_backlog_read_raw_config() -> dict` that returns the row's `config` JSON unvalidated, used only by the seed). A stored mode of `propose`/`auto` (pre-flip rows) reads as `on` — put that fold in `_backlog_read_config` with a comment; a test pins it.
2. **Seed (D10).** `_backlog_seed_policies(raw: dict, mode_before: str) -> int` in the orchestrator: when the raw blob carries `enabled_repos` (a non-empty list) AND no `Project` has a policy yet, for every product owning one of those repos (`owning_projects_by_repo`, `server/pm/repo_project.py:31`) write `effective_policy({"dispatch": "auto" if mode_before == "auto" else "propose", "queue_min_status": raw.get("queue_min_status") or "Ready", "max_in_flight": raw.get("repo_caps", {}).get(<its primary repo>, 1), "weight": int(raw.get("repo_weights", {}).get(<its primary repo>, 1)), "max_attempts": raw.get("max_attempts", 2), "default_model": raw.get("default_model", "auto"), "respect_dependencies": raw.get("respect_dependencies", True)})`, stamp `automation_policy_updated_at`, audit `pm.project.policy` with identity `backlog-runner` and detail `{slug, policy, seeded_from: "enabled_repos"}`, emit ONE `AutomationEvent` kind `policies_seeded` `{products: [...]}`; return the count. When `enabled_repos` is empty (= "all products") seed every product with a linked board. The loop calls a new `seed_policies` seam ONCE per process at the start of the first `tick` (a `self._seeded = True` flag), best-effort (a failure logs and does not stop the tick). `mode_before` = the row's stored mode string before the fold in (1).
3. **Queue scoping and ranking.** `_backlog_query_queue`: inside `_rows(s)`, also `policies = policies_by_slug(s)`; drop the `enabled_repos` filter; keep rows whose `r.product` (the `QueueRow.product` slug — confirm the attribute name in `queue.py:50-76`) has `policies[slug]["dispatch"] != "off"`; then rank PER PRODUCT: group rows by product, call `rank(rows_p, min_status=policy["queue_min_status"], respect_dependencies=policy["respect_dependencies"], needs_human_retry_days=<global as today>, now=now)`, merge the entries by `score` desc (stable) and sum the skip dicts. Cache `self._backlog_last_policies = policies` for the other seams in the same tick.
4. **Selection.** `_backlog_select_batch`: build repo-keyed dicts from the policies via the products' repos (`ProductSyncView.repos`, from `list_product_views` in the same sync read — extend `_rows` to return `(rows, policies, repos_by_slug)`): `weights = {repo: policy["weight"] for each repo of each ON product}`, `caps = {repo: policy["max_in_flight"] …}`, `repo_order = repos of ON products in `Project.created_at` order`; pass them to `select_batch(config, …)` — extend `select_batch`'s signature with explicit `weights=`, `caps=`, `repo_order=` kwargs (defaulting to the config reads so existing tests pass) and prefer the kwargs. `plan_to_batch`/`order_live_groups` unchanged.
5. **Per-product propose vs auto.** In `_refill(mode, config)`: the loop's `mode` is now `on`; for the selected batch, `dispatch = policies[batch.product]["dispatch"]` (`Batch` gains `product: str | None` from the candidates' `product` field; confirm `Batch`'s definition in `backlog_grouping.py`) — `propose` → the existing announce path (what global `propose` did), `auto` → spawn. Store `product` in the claim's target state at claim time (`new_st["product"] = batch.product`).
6. **Attempts and model.** `_sweep_one` reads `max_attempts` from the product's policy via `state.get("product")` (fallback: the global default 2 when the claim predates the flip); `_backlog_spawn` resolves the model from `policies[batch.product]["default_model"]` (the `config` passed to `resolve_model` becomes `{**config, "default_model": policy["default_model"]}` so `resolve_model`'s signature stays).
7. **Counts and card.** `_backlog_count_candidates` returns `(eligible, blocked, untriaged, per_product)` where `per_product = {slug: {"dispatch", "eligible", "blocked", "untriaged", "in_flight", "cap"}}` (`in_flight` from the target states' `product`); `_cache_counts` stores `backlog_products` on the ctx (a dict, separate from the int-only `backlog_counts`); `status()` appends one line per ON product: `"kwgt-cairn: auto · 1/1 in flight · 12 eligible"` after the summary text (newline-joined; check the web's `AutomationStatus.text` rendering handles `\n` — `grep -n "whitespace-pre" web/src/features/delivery` — if not, join with ` · ` on one line). When no product is on: text `"idle — no product has dispatch on"`.

- [ ] **Step 1: Tests first** — `test_backlog_runner_automation.py`: modes `("off","on")`; schema keys exclude the retired ones and the retired tuple names them; `status()` renders per-product lines from `ctx.backlog_products`. `test_backlog_runner_loop.py`: (a) with two products `on`/`off`, only the ON product's candidates are queried/selected (through the `select_batch` seam fed by a fake `query` returning rows tagged with `product`); (b) a `propose` product's batch is announced, not spawned; an `auto` product's is spawned; (c) `max_attempts` per product: a claim tagged `product=p1` with policy `max_attempts=1` gives up after one attempt while `p2` (2) does not; (d) the seed runs once: the `seed_policies` seam is called on the first tick only. Seams tests (`test_backlog_policy_seams.py`, the `_seed_initiative`-style DB seeding idiom from `test_sdlc_api.py`): `_backlog_query_queue` ranks per product with each product's `queue_min_status`; `_backlog_select_batch` translates weights/caps/order; `_backlog_seed_policies` writes policies for the owning products of `enabled_repos`, audits, emits once, and is a no-op when a policy already exists; the `propose`/`auto`→`on` mode fold.

- [ ] **Step 2: Implement** per the seven points, in that order, running the named tests as you go.

- [ ] **Step 3: Gate** — `… pytest server/tests/test_backlog_runner_automation.py server/tests/test_backlog_runner_loop.py server/tests/test_backlog_runner_e2e.py server/tests/test_backlog_runner_codex_acceptance.py server/tests/test_backlog_policy_seams.py server/tests/test_pm_queue*.py -q`; `ruff`; `mypy`. Then `grep -rn "enabled_repos\|repo_weights\|repo_caps" server/ web/src --include=*.py --include=*.ts --include=*.tsx | grep -v test | grep -v retired` → only the seed and the autopilot's own `enabled_repos` (a different automation — leave it; note it).

- [ ] **Step 4: Commit** — `feat(runner): policy phase 1 task 2 — the Backlog Runner reads product policies; global repo knobs retire; one-shot seed`.

---

### Task 3: The product page's Automation card

**Files:**
- Create: `web/src/features/delivery/automation/ConfigInput.tsx` (moved from `ConfigPanel.tsx:14-153`, exported `ConfigInput` + `FieldValueProps`); modify `ConfigPanel.tsx` to import it (its tests unchanged)
- Modify: `web/src/api/pm.ts` (`Project.automationPolicy: Record<string, unknown> | null`; `fetchPolicySchema(): Promise<ConfigField[]>`; `updateProductPolicy(slug, policy: Record<string, unknown> | null): Promise<Project>`), `web/src/api/hooks.ts` (`usePolicySchema()` — `staleTime: Infinity`; `useUpdateProductPolicy(slug)` → invalidates `QK.project(slug)` and `QK.roadmapMap`; toast on error)
- Create: `web/src/features/roadmap/detail/AutomationPolicyCard.tsx` (+ test)
- Modify: `web/src/features/roadmap/detail/ProductDetail.tsx` (Overview, below the repos and above `<OnboardingWizard>`), its test, `RoadmapPage.test.tsx` mocks; every `Project` fixture gains `automationPolicy: null` (`typecheck` lists them)

**Interfaces:**
- `AutomationPolicyCard({ product, project })`: heading "Automation"; when `project.onboardingState !== 'live'`: the fields rendered DISABLED with the note "Automation starts once the product is live." (the wizard above explains how); otherwise each `PRODUCT_POLICY_SCHEMA` field through `ConfigInput` with `value = policy[key] ?? field.default` where `policy = project.automationPolicy ?? {}`; a change calls `useUpdateProductPolicy(slug).mutate({...effective, [key]: val})` (whole-object replace, as `ConfigPanel` does per change; `effective` = every schema key with its current or default value so the server never sees a partial blob); a first change on a product with `automationPolicy === null` therefore opts it in at once — a one-line note above the fields says so ("Not opted in — the first change turns automation on for this product with these values.") and the `dispatch` field is listed first. No repo-typed fields exist in the phase-1 schema, so `ConfigInput`'s `enabledRepos` prop is not needed. An `IconButton`-free card; every control is a labelled input.
- Tests: `AutomationPolicyCard.test.tsx` — renders every schema field with the default when the policy is null and the note; a `dispatch` change sends the full effective blob with `dispatch: 'auto'`; disabled + note when not live; `ProductDetail.test.tsx` — the card appears on the Overview with `usePolicySchema`/`useUpdateProductPolicy` mocked.

- [ ] **Step 1: Move `ConfigInput`** into its own file (verbatim), re-export nothing else; run `(cd web && npx vitest run src/features/delivery)` → unchanged.
- [ ] **Step 2: Failing tests** for the card and the page; `useProject`'s `Project` mock gains `automationPolicy`.
- [ ] **Step 3: Implement** fetchers, hooks, card, mount, fixtures.
- [ ] **Step 4: Web gate; commit** — `feat(roadmap): policy phase 1 task 3 — the product page's Automation card over the shared ConfigInput`.

---

### Task 4: Docs

**Files:**
- Modify: `docs/product/features/product-management-layer.md` (a "Product automation policy" section: what it governs in phase 1, where it is edited, the seed at the flip, what stays global), the Backlog Runner's feature doc (find it: `grep -l "Backlog Runner" docs/product/features/*.md`; document the retired keys and the `on` mode), `deploy/README.md` only if it names `enabled_repos` as an operator step (grep first).

- [ ] **Step 1: Write the sections** against the code as landed (read `product_policy.py` and the runner's `status()`), not the spec's future tense.
- [ ] **Step 2: Commit** — `docs: policy phase 1 task 4 — product automation policy and the runner's retired keys`.

---

## Self-review

**Spec coverage (phase 1 per §11):** D1/D3 (schema on `Project`, one machinery) — Task 1; D4 (precedence + retired keys) — Task 2 points 1, 3–6; D10 (seed) — Task 2 point 2; §5 scope/ranking/batch/propose-vs-auto/spawn/status — Task 2 points 3–7; §9 product Overview card — Task 3; §10 columns, `automationPolicy`, `PATCH …/policy`, schema endpoint — Task 1; the Delivery per-product table (§9) is deferred to phase 3 with the outcome counts it needs — noted, not silently dropped. `give_up_after_bounces`/`triage_auto_accept` fields arrive with phases 3/2 (§4 note).

**Placeholder scan:** Task 2 is written as seven precise changes with names and the tests that pin each; the implementer reads the cited code. No "handle errors" / "TBD".

**Type consistency:** `effective_policy` keys (Task 1) are exactly what Task 2 reads (`dispatch`, `queue_min_status`, `max_in_flight`, `weight`, `max_attempts`, `default_model`, `respect_dependencies`) and what Task 3 renders from `GET /pm/policy-schema`; `Project.automationPolicy` (web) mirrors `_project_to_dict`'s raw blob; `Batch.product` (Task 2) comes from `to_candidate`'s `product` field (`queue.py:178-189`).
