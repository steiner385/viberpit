# Desktop-parity D-14: fast mode, live effort, presets, PR auto-fix, worktree location — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the D-14 slice:
- a ⚡ fast-mode toggle in the prompt row, carrying a cost note;
- Claude effort that actually changes mid-session;
- presets you can pick from a new `+` menu in the dispatch bar and from ⌘K;
- a per-session PR auto-fix toggle in the Environment card;
- the worktree location surfaced in Settings › Sessions.

**Owner decisions (2026-11-04):**
- **Fast mode:** a toggle plus a cost note. It is off by default.
- **Sandbox:** skipped. The benchmark row is deferred, with this reason: "fails open on hosts without socat, auto-allows Bash past permission cards, and blocks the sockets gh/push need".
- **Branch prefix:** deferred. It stays the fixed `viberpit/`, which is also the safety check for deleting branches. Settings shows it read-only.

**Facts:** `docs/superpowers/specs/2026-11-04-d14-session-settings-verification.md` (CLI 2.1.318). It is binding.

**Tech Stack:** Python, FastAPI, SQLAlchemy, pytest; React + TS, vitest, Playwright.

**Spec:** `docs/superpowers/specs/2026-11-01-ui-desktop-parity-design.md` §3 (prompt row), §2 (Environment card), §8 (Presets, Worktrees, Sandbox, PR auto-fix), §10, and §11 D-14. Decisions are D14 and D16.

## Global Constraints

- **Live settings channel.** Add one driver method, `apply_flag_settings(patch: dict) -> dict`. It sends the CLI's `apply_flag_settings` control request through the same private control channel D-9 rewind uses (`_send_control_request`). Every reply is awaited before the next request is sent; the spike shows that back-to-back writes read stale values.
  - Errors map to a `SettingsError` with codes in one table. Expected refusals log at INFO.
  - It is Claude only. Codex keeps its existing paths.
- **Fast mode.**
  - **Support.** A model supports fast mode only if the `initialize` model list says `supportsFastMode` for it. Cache that per session from `initialize`, and expose it on the session payload as `fastModeAvailable`. Today that means Opus 4.8, 5 and 5.5.
  - **State.** Persist `fast_mode` (a nullable bool column) per session. Apply it at spawn through the SDK's `settings` / `--settings` JSON `{"fastMode": true}`; that is spawn-time only and never written to the shared `settings.json`. Apply it live through `apply_flag_settings({"fastMode": bool})`.
  - **Default.** Off. A model switch to one that doesn't support fast mode clears it, and the UI hides ⚡.
  - **API.** `POST /api/sessions/{id}/fast {enabled}` returns 200 `{enabled}`.
    - 409 `FAST_UNAVAILABLE` when the model or engine doesn't support it.
    - 409 `daemon_outdated` from an old daemon.
    - It runs per session through the RPC, remote, fleet and facade layers.
  - **UI.** A ⚡ toggle in the prompt row, shown only when `fastModeAvailable`. Its tooltip reads: `Fast mode — faster output on the same model. Bills usage credits at a higher rate, outside your plan.` The first time it is enabled in a browser, show that sentence as an inline note (localStorage key under the storage namespace).
  - **Command.** A `/fast` app command toggles it, gated by availability.
- **Live effort (Claude).**
  - **Bug.** Today the D-1 effort ▾ changes nothing on a live Claude session; `set_model` ignores effort.
  - **Fix.** The existing effort path applies `apply_flag_settings({"effortLevel": <level>})` on the live Claude driver. Reuse the D-1 endpoint and model; don't add a second effort route.
  - **Spawn.** Effort at spawn keeps today's mechanism.
  - **Test.** Changing effort mid-session on Claude sends the control request.
- **Presets.**
  - **Data.** Reuse `SessionPreset` and `PromptTemplate` and their existing APIs (read the spike). No new model.
  - **Dispatch bar.** Add a `+` menu with two entries. **Attach image** moves into it from the existing button, which is deleted. **Presets ›** lists the presets; choosing one sets `presetId` on the next `POST /api/sessions` and shows a removable chip in the bar. **Templates ›** inserts the rendered template text through the existing render endpoint.
  - **Session composer.** It gets a `+` menu with Attach and the slash-command palette only. Presets apply at creation, so they don't appear there.
  - **⌘K.** The palette gains a `New session from preset: <name>` action per preset.
  - **No chord.** Every digit chord is taken. Record this in spec §7/§8.
  - **Unused fields.** `branch_strategy` stays stored and unread. Document that, and don't wire it.
- **PR auto-fix.**
  - **Column.** Add a nullable bool, `sessions.pr_autofix_paused`. Migration `0099` is chained off `0098`; check origin/master first and renumber if a sibling has taken it.
  - **Check point.** Read the flag at the single PR Doctor decision point, `_pr_doctor_step`, from the DB row (the sweep can find PRs with no live session). When it is true, skip the session's PR and log INFO.
  - **Unchanged.** `autopilot_paused` stays as it is.
  - **API.** `POST /api/sessions/{id}/pr-autofix {enabled}`.
  - **Environment card.** Add a "PR auto-fix" switch, shown only when the session has a PR or `owns_pr`. Its copy reads `PR Doctor fixes failing checks and conflicts on this session's PRs`.
- **Worktree location.** Settings › Sessions shows the existing `repoRoot` setting (Settings › General; one setting, two places only if `settingsMeta` supports cross-linking; otherwise move it). Use this copy: `Where repos are cloned and session worktrees are created. Changing it doesn't move existing worktrees — they keep working but cleanup skips them.` Show the branch prefix read-only as `viberpit/`, noting `Fixed — it guards automatic branch cleanup.`
- **Gates.**
  - Server: ruff, mypy, focused pytest, and the RPC parity tests.
  - Postgres lists for the two new nullable columns (`fast_mode`, `pr_autofix_paused`); nullable usually needs nothing, but check.
  - Web: typecheck, lint, full vitest, budget (ceilings 107.8 / 302.7), and e2e.

## Review Focus

1. **Switching from a fast-capable model to an incapable one mid-session:** fast mode clears, ⚡ hides, and no control request errors. Pinned in Task 1.
2. **A daemon restart with fast mode on:** the respawn reapplies it from the row. Pinned in Task 1.
3. **A PR Doctor sweep for a session whose driver isn't live and whose auto-fix is paused:** it skips, because it reads the row. Pinned in Task 2.
4. **A preset that pins a model the account can't use:** the existing create-session refusal path surfaces it, with no new path. Pinned in Task 3.
5. **Effort changed while a turn is running:** it applies (or queues) without breaking the turn. Pinned in Task 1.

---

### Task 1: Server — `apply_flag_settings`, fast mode, live Claude effort

**Files:** the driver (`apply_flag_settings`, the initialize model cache), the SDK factory (spawn `settings`), the `fast_mode` column plus migration, the orchestrator `set_fast_mode`, the effort path, RPC/remote/fleet/facade, the route, the `fastModeAvailable` payload field, and `/fast` in the catalog and dispatcher. Tests: `server/tests/test_fast_mode.py`, plus a no-model CLI contract test for `apply_flag_settings` (`fastMode` and `effortLevel` round trips, following the D-9/D-13 harness).

- [ ] **Step 1: Failing tests**
  - Each spec bullet, plus Review Focus 1, 2 and 5.
  - The contract test: the CLI accepts both patches and `initialize` reports `supportsFastMode`.
- [ ] **Step 2–4.** Then run the focused tests, the parity tests, ruff and mypy.
- [ ] **Step 5: Commit** `feat(sessions): fast mode and live Claude effort via apply_flag_settings (D-14)`

### Task 2: Server — PR auto-fix flag

**Files:** the `pr_autofix_paused` column, migration (shared with Task 1 if it runs in the same window; otherwise a separate revision chained correctly), the `_pr_doctor_step` check, the orchestrator setter, RPC/remote/fleet/facade, the route, and the payload field `prAutofixPaused`. Test: `server/tests/test_pr_autofix_toggle.py`.

- [ ] **Step 1: Failing tests**
  - A paused session's PR is skipped by a sweep, with no live driver (Review Focus 3).
  - Unpaused behaves as before.
  - The toggle persists and survives rehydrate.
  - `autopilot_paused` is independent.
  - Logs are INFO.
- [ ] **Step 2–5.** Commit `feat(sessions): per-session PR auto-fix toggle on PR Doctor (D-14)`

### Task 3: Web — ⚡ toggle, `+` menu with presets, PR auto-fix switch, Settings › Sessions

**Files:** the prompt-row ⚡ (with the first-use note and `/fast` wiring), the DispatchBar `+` menu (Attach moved in, Presets/Templates, the preset chip), the composer `+` menu, ⌘K preset actions (`usePaletteEntries`), the EnvironmentCard switch, Settings › Sessions (repoRoot plus the read-only prefix), and hooks with mocks. Tests: unit tests, plus e2e `web/e2e/session-settings.spec.ts` (route-mocked).

- [ ] **Step 1: Failing tests**
  - ⚡ is hidden when unavailable; toggling it posts; the first-use note shows once.
  - The `+` menu: Attach still works; choosing a preset sends `presetId` on create and shows a chip; a template inserts text.
  - ⌘K lists the preset actions.
  - The PR auto-fix switch shows only with a PR and posts.
  - Settings › Sessions shows repoRoot (editable, with the copy) and the read-only prefix.
  - Review Focus 4 goes through the existing refusal UI.
- [ ] **Step 2–4.** Then the full web gates, budget, and e2e.
- [ ] **Step 5: Commit** `feat(web): fast-mode toggle, presets in the + menu, PR auto-fix switch, worktree location in Settings (D-14)`

### Task 4: Benchmark and docs

- **Benchmark.** Update these rows:
  - fast → Y;
  - presets → Y;
  - worktree settings → P (prefix deferred);
  - sandbox → deferred, with the owner reason;
  - PR auto-fix → Y;
  - effort row note (live on Claude).

  Then regenerate §3 and report the target (D-13: 89%).
- **Spec.** In §3/§7/§8/§10, record the `apply_flag_settings` channel, the fast-mode cost copy, presets with no chord, PR auto-fix as its own column, and the sandbox and prefix deferrals with reasons.
- **Product docs.**
- **Ceilings.** measured + 0.3.
- [ ] Commit `docs: D-14 benchmark rerun and session-settings docs`.
