# MCP auth overhaul — Phase 3: heal + alert — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:**
- A successful refresh heals sessions that were stuck on that server.
- Each grant's refresh health is recorded durably.
- When a grant dies, or expires because refresh keeps failing, Tony gets **one** push naming the server.
- The MCP page shows `dead`, and shows the reason a refresh is failing.

**Architecture:**
- `run_once` returns per-key outcomes.
- Each edge tick runs four steps in order:
  1. reconcile, which also computes the superseded refresh-bearing keys the refresher must skip;
  2. refresh;
  3. record outcomes in a new `mcp_grant_health` table;
  4. heal and alert.

  Heal reuses `after_login`, extended to clear the CLI's needs-auth cache. Alerts go through
  `notify_event` with a new event class `mcp_auth_needed`. Dedupe lives in the health row.
- Status reads the health row to tell `dead` apart from `never`, and to show the failure reason.

**Tech Stack:** Python, FastAPI, SQLAlchemy + Alembic; React + TS (small).

**Spec:** `docs/superpowers/specs/2026-11-04-mcp-auth-overhaul-design.md`. This covers §4,
the push half of §5, and designer call 5. It also closes Phase 2's parked note: superseded
refresh-bearing bundles become inert, so they are never refreshed.

**Precondition:** #3266 (Phase 2) has merged. Branch off `origin/master` after it lands.

## Global Constraints

- **Event class.** It is `mcp_auth_needed`, added to `server/sessions/notify_rules.py`
  `EVENT_CLASSES`, with the web label `"MCP sign-in needed"` in
  `web/src/features/settings/sections/notificationsApi.ts`. The `notify_event("mcp_auth_needed", …)`
  call passes the literal string directly (`test_assistant_watches_api.py` source-walks it)
  with `fanout_when_unconfigured=True`.
- **Push copy, exactly:**
  - Title: `"MCP sign-in needed: {server}"`.
  - Body for `dead`: `"{server}'s sign-in was revoked or expired. Sign in once to restore it for every account and runner."`
  - Body for `expired`: `"{server}'s sign-in expired — background refresh is failing: {reason}. Sign in again."`

  `{reason}` is truncated to 160 chars.
- **Deep link.** Use `url="/settings/mcp"`. Phase 4 swaps it for the one-tap start URL.
- **Dedupe.** At most one push per (store key, grant generation). The generation is the
  `last_ok_at` value at alert time; null means never-ok. The health row's `alerted_for`
  holds `f"{outcome}:{last_ok_at_iso or 'never'}"`. A later successful refresh, or a
  sign-in, makes a new generation.
- **Expired-while-failing.** The grant counts as expired when the bundle's `expiresAt` is
  in the past **and** `consecutive_failures >= 1`. It alerts once, as above.
- **Healing.**
  - "Healed" means a key whose outcome is `refreshed`, where the health row before the tick
    had `consecutive_failures > 0` or `state in {"dead","expired"}`.
  - Heal runs `after_login` for each healed server, but probes **once** per tick at most.
  - Never heal on a routine refresh of a healthy grant.
- **Superseded keys.** These are keys holding a `refreshToken` that are not any wanted
  key's. The refresher never refreshes them, and they are passed as
  `run_once(skip_keys=…)`. They are computed purely from the store and the wanted set,
  each tick.
- **Health table.**
  - Table `mcp_grant_health`. Primary key `key: str`.
  - Every other column is nullable: `server`, `state`, `last_attempt_at`, `last_ok_at`,
    `last_outcome`, `last_error`, `consecutive_failures`, `alerted_for`, `alerted_at`,
    `updated_at`.
  - Datetimes are naive UTC from `server/db/clock.py:db_utc_now()`. Never use `func.now()`.
  - Alembic revision `0101_mcp_grant_health`. Before committing, chain `down_revision` to
    the newest revision on `origin/master`. #3263 holds 0099 and 0100; re-check at build
    time, and renumber if master moved.
- `last_error` is the refresh exception text, trimmed to 500 chars. It must never contain
  a token. The refresher's errors carry HTTP status and OAuth error codes only; assert this
  in a test.
- **Gate.** Run the four lint-and-test steps, the full web gate, **and the Postgres suite**
  (`VIBERPIT_TEST_PG_URL`, per the memory note on running it locally), because this adds a
  model.

## Review Focus

1. **A transient failure that recovers before the token expires.** It must produce no push,
   then one heal on recovery. Task 3 covers this with
   `test_transient_then_refreshed_heals_without_alert`.
2. **The same dead grant seen on many ticks.** Exactly one push. Task 4 covers this with
   `test_dead_grant_alerts_once_across_ticks`.
3. **Re-sign-in after a dead grant, which then dies again.** A second push. Task 4 covers
   this with `test_new_generation_alerts_again`.
4. **A notification backend that raises.** The tick continues, and `alerted_for` is **not**
   set, so the next tick retries. Task 4 covers this with `test_alert_failure_retries_next_tick`.
5. **A superseded refresh-bearing bundle at an old key.** It is never refreshed. Task 1
   covers this with `test_run_once_skips_superseded_keys`.

---

### Task 1: Per-key refresh outcomes and skip keys

**Files:**
- Modify: `src/viberpit/mcp_oauth_refresh.py`. `_refresh_due` and `run_once` gain
  `skip_keys: frozenset[str] = frozenset()`, and the summary gains `"outcomes"`.
- Modify: `server/mcp_health/grants.py`. Add `superseded_refresh_keys(store: dict, wanted: list[WantedServer]) -> frozenset[str]`
  (pure), and make `reconcile_grants` return it alongside the drop set:
  `ReconcileResult(drop: frozenset[str], skip: frozenset[str])`.
- Modify: `server/app.py` `_mcp_oauth_tick`. Pass `drop_keys` and `skip_keys`.
- Test: `tests/test_mcp_oauth_refresh.py`, `server/tests/test_mcp_grants.py`

**Interfaces:**
- Produces:
  - `summary["outcomes"]: list[dict]`, each
    `{"key": str, "label": str, "outcome": "refreshed"|"dead"|"transient", "error": str|None}`.
    The existing label lists stay.
  - `ReconcileResult`.

- [ ] **Step 1: Write the failing tests.**
  - `test_run_once_reports_per_key_outcomes`: one key of each outcome, with `error` set only
    for transient, trimmed to 500.
  - `test_run_once_skips_superseded_keys`
  - `test_superseded_refresh_keys`: refresh-bearing and unwanted is included; wanted, or
    without a refresh token, is excluded.
  - `test_transient_error_text_has_no_token`: the fake `post_form` raises with a body that
    contains the refresh token, and the recorded error must not contain it. If the current
    exception text could carry a body, sanitise it at the raise site.
  - Update the existing tick test for `ReconcileResult`.
- [ ] **Step 2: Run them and check they fail.**
- [ ] **Step 3: Implement it.**
- [ ] **Step 4: Run the tests and check they pass**, together with `tests/test_mcp_rekey.py`,
  `tests/test_creds_mcp_refresh.py` and `server/tests/test_mcp_grants.py`.
- [ ] **Step 5: Commit:** `feat(mcp): per-key refresh outcomes; never refresh superseded grants`

---

### Task 2: `mcp_grant_health`: the model, the migration and the store

**Files:**
- Modify: `server/db/models.py` (`McpGrantHealth`)
- Create: `server/db/migrations/versions/0101_mcp_grant_health.py`
- Create: `server/mcp_health/grant_health.py`
- Modify, if the sweep requires it: `server/tests/test_postgres_compat.py` `_REQUIRED_EXTRAS`
  and `server/tests/test_schema_reconcile_coverage.py`
- Test: `server/tests/test_mcp_grant_health.py`

**Interfaces:**
- Produces:
  - `async def load(keys: Iterable[str] | None = None) -> dict[str, dict]`. Rows are
    wire-shaped dicts, and datetimes are ISO strings with a `Z` suffix.
  - `async def record(outcomes: list[dict], *, expired_keys: frozenset[str]) -> list[Transition]`:
    - It upserts one row per outcome:
      - `last_attempt_at` is always set to now.
      - `refreshed` sets `last_ok_at` to now, sets `consecutive_failures` to 0 and sets
        `state` to `"live"`.
      - `dead` sets `state` to `"dead"`.
      - `transient` increments `consecutive_failures`. It sets `state` to `"expired"` if the
        key is in `expired_keys`, and otherwise leaves `state` unchanged.
      - Every outcome stores `last_outcome` and `last_error`.
    - It returns transitions:
      `@dataclass(frozen=True) class Transition: key: str; server: str; kind: Literal["healed","alert_dead","alert_expired"]; reason: str | None; generation: str`.
    - Alert transitions are emitted only when `alerted_for != f"{kind_outcome}:{generation}"`.
  - `async def mark_alerted(key: str, alerted_for: str) -> None`

- [ ] **Step 1: Write the failing tests.** Use `isolated_db`.
  - Exercise the upsert semantics above.
  - Two consecutive `dead` records yield one `alert_dead`, provided `mark_alerted` was called
    after the first.
  - Use `refreshed` after a transient to get `healed`.
  - Use `transient` with the key in `expired_keys` to get `alert_expired`.
- [ ] **Step 2: Run them and check they fail.**
- [ ] **Step 3: Implement it.**
  - Add the model, with every non-PK column nullable.
  - Add the migration, chained to the current newest revision on `origin/master`.
  - Upsert with `dialect_insert(...).on_conflict_do_update`, as in
    `server/mcp_health/store.py`.
- [ ] **Step 4: Run the tests and check they pass**, together with
  `server/tests/test_schema_reconcile_coverage.py`, `server/tests/test_postgres_compat.py`
  (with `VIBERPIT_TEST_PG_URL` set) and `server/tests/test_migrations*.py`, if present.
- [ ] **Step 5: Commit:** `feat(mcp): durable per-grant refresh health`

---

### Task 3: Heal on refresh

**Files:**
- Modify: `server/mcp_health/after_login.py`. Call `server.mcp_auth.clear_needs_auth_cache(server)`
  in a thread, before the reconnect. Add the keyword `probe: bool = True`.
- Modify: `server/mcp_auth.py`. Remove its own `clear_needs_auth_cache` call before
  `on_success` (around :200), so the clearing happens once, in `after_login`.
- Modify: `server/app.py` `_mcp_oauth_tick`. After `record`, for each `healed` transition,
  run `after_login(app.state.sessions, server, accounts, probe=False)`, read lazily. Then
  probe once, if any server healed.
- Test: `server/tests/test_mcp_after_login.py`, `server/tests/test_mcp_grants.py` (tick)

- [ ] **Step 1: Write the failing tests.**
  - `test_after_login_clears_needs_auth_cache`
  - `test_transient_then_refreshed_heals_without_alert`: tick 1 is transient, so there is no
    heal and no push. Tick 2 is refreshed, so `after_login` is called once and there is no
    push.
  - `test_routine_refresh_does_not_heal`
  - `test_heal_probes_once_per_tick`: two healed servers produce one probe.
- [ ] **Steps 2–4:** run the tests and check they fail; implement; run them and check they
  pass, together with `server/tests/test_mcp_auth_manager.py`.
- [ ] **Step 5: Commit:** `feat(mcp): a successful refresh heals sessions stuck on that server`

---

### Task 4: Alert once when a grant dies

**Files:**
- Modify: `server/sessions/notify_rules.py` (`EVENT_CLASSES`)
- Modify: `web/src/features/settings/sections/notificationsApi.ts` (label)
- Modify: `server/app.py` `_mcp_oauth_tick`. For each alert transition, call `notify_event`
  with the Global Constraints copy, then `mark_alerted`.
- Test: `server/tests/test_mcp_grants.py`, `server/tests/test_notify_rules.py`, plus the
  web test for the label list, if one exists.

- [ ] **Step 1: Write the failing tests.**
  - `test_dead_grant_alerts_once_across_ticks`
  - `test_new_generation_alerts_again`
  - `test_expired_while_failing_alerts_once`, which also checks the body contains the reason.
  - `test_alert_failure_retries_next_tick`. `notify_event` never raises by contract, so
    simulate the failure by making the alert helper raise around it. `mark_alerted` must not
    run.
  - `test_mcp_auth_needed_is_an_event_class`
- [ ] **Steps 2–4:** run the tests and check they fail; implement; run them and check they
  pass.
- [ ] **Step 5: Commit:** `feat(mcp): one push when a shared sign-in dies`

---

### Task 5: Status shows `dead` and the refresh failure reason

**Files:**
- Modify: `server/mcp_health/overall.py`
  - `grant_state(store, *, key, now_ms, health: dict | None = None)`: a cleared bundle plus
    `health["state"] == "dead"` gives `"dead"`.
  - `derive_overall(..., refresh_error: str | None = None)`: the `expired` copy becomes
    `"Sign-in expired {when} UTC — background refresh is failing: {refresh_error}; sign in again."`
    when `refresh_error` is set, and stays as it is otherwise. `dead` uses
    `"Sign-in revoked or expired — one sign-in restores it for every account and runner."`
- Modify: `server/api/library.py` `get_mcp`. Load health rows once with `grant_health.load()`,
  then pass `health` and `refresh_error` through.
- Modify: `web/src/api/types.ts` only if a new wire field is added. Prefer reusing
  `overall.error`, so the web needs no change.
- Test: `server/tests/test_mcp_health_overall.py`, `server/tests/test_library_mcp_status.py`

- [ ] **Steps 1–5:** write the tests (`test_dead_from_health_row`,
  `test_expired_copy_includes_refresh_error`, `test_get_mcp_reads_health_once`), run them and
  check they fail, implement, run them and check they pass, then commit
  `feat(mcp): status says when a sign-in died and why refresh is failing`.

---

### Task 6: Docs, the full gate, and the PR

- [ ] **Step 1: Edit the docs.**
  - `docs/product/features/mcp-status.md`: heal on refresh, alert copy and dedupe, the
    health table, and that superseded grants are inert.
  - The spec's Delivery order item 3: link to this plan.
- [ ] **Step 2: Run the full gate.** Run the server suite as a `systemd-run --user` unit,
  with `web/dist` absent. Also run the Postgres suite and the web gate. Show any failures
  are pre-existing with a run on a detached `origin/master`.
- [ ] **Step 3: Open a ready PR** that says "Phase 3 of 5", and arm `--squash --auto`.
