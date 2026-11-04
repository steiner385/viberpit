# Desktop-parity D-16a: output styles, `/plan`, `/output-style` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the first slice of D-16:
- Output styles, chosen per session from the `+` menu, with a default and a list in Settings › Output styles.
- An app-handled `/plan [on|off]`.
- An app-handled `/output-style [style]`.

The CLI's own `/output-style` writes into the worktree, so it must never pass through.

**Facts (binding):** `docs/superpowers/specs/2026-11-04-d16-verification.md`, sections 3, 4 and 5 (CLI 2.1.318).

**Decision:** D15, "Adopt all". The spec rows are at `docs/superpowers/specs/2026-11-01-ui-desktop-parity-design.md` ~365–368.

**Controller rulings (2026-11-04)** — owner decisions D5 and D6 from the spike, ruled on the recommended option; low-risk and reversible:
- **D5 (a).** The default output style is a ViberPit `AppSettings` value. It is applied per session through the flag layer and never written to the shared `settings.json`, so the owner's own terminal `claude` is unaffected.
- **D6 (a).** `/plan` takes only `[on|off]`; a bare `/plan` toggles. It mirrors `/fast`.

**Tech Stack:** Python, FastAPI, SQLAlchemy, pytest; React + TypeScript, vitest, Playwright.

## Global Constraints

- **Engines.** Claude only. Codex keeps its own paths: the style picker and `/output-style` are hidden for Codex sessions, the same way the catalog gates `/fast` (`flagSettings`). `/plan` works on both engines through the existing `set_permission_mode`.
- **Output style: storage, spawn, live change.** This mirrors D-14's fast mode end to end; read `set_fast_mode`, `_spawn_flag_settings` (`orchestrator.py:~8082`) and `POST /sessions/{id}/fast` (`api/sessions.py:~2535`) and follow them.
  - **Storage.** Migration `0102`, chained off the newest revision (check origin/master first and renumber if a sibling took 0102), adds two nullable columns:
    - `sessions.output_style` (nullable string; `NULL` means use the default);
    - `app_settings.default_output_style` (nullable string; `NULL` means the CLI's `default`).
  - **Spawn.** `_spawn_flag_settings` emits `{"outputStyle": <session value, else app default>}` only when that value is non-null and not `default`.
  - **Live change.** Through `driver.apply_flag_settings({"outputStyle": name})`. As in D-14, await each reply before sending the next.
  - **Validation.** The CLI accepts unknown names silently, so validate every name server-side against the session's `initialize.available_output_styles`. Cache that list per session from `server_info`, as D-14 caches `supportsFastMode`.
    - An unknown name is a 409 `OUTPUT_STYLE_UNKNOWN`.
    - A Codex or non-flag engine is a 409 `OUTPUT_STYLE_UNAVAILABLE`.
    - An old daemon is a 409 `daemon_outdated`.
  - **API.** `POST /api/sessions/{id}/output-style {style: string | null}` returns 200 `{style}`; `null` resets to the default.
    - It runs per session through the RPC, remote, fleet and facade layers (the D-14 `/fast` pattern), including the parity tests.
    - Expected refusals log at INFO.
  - **Payload.** The session payload gains `outputStyle` (the effective name) and `availableOutputStyles` (the list, or `[]` when unknown).
  - **Expect a cache miss.** A live change invalidates the prompt cache for the next turn. Put that in the picker's tooltip: `Changing style mid-session re-sends the system prompt on the next turn.`
- **The Settings default.**
  - The setting is `defaultOutputStyle`. It goes through the existing settings API and registry, and is validated against the built-in list plus the styles found in the shared `output-styles/` dir.
  - Ruling: the edge has no live session to ask for the list, so for the Settings default the server lists:
    - the five built-ins (`default`, `Proactive`, `Concise`, `Explanatory`, `Learning`);
    - the `.md` files in the shared user dir, named by their frontmatter `name`, else the filename stem.

    A plugin style (`plugin:Name`) can still be picked per session from the live list, but it is not offered as the Settings default.
- **Shared custom styles.**
  - Add `output-styles` to `SHARE_ALLOWLIST` (`src/viberpit/configdir.py`), so a user style in `~/.claude/output-styles/` reaches every account.
  - Keep a private dir if an account already has a real one (it is not linked, and this is logged at INFO), the same as the D-13 `CLAUDE.md` rule.
  - Core tests run with `PYTHONPATH=$PWD/src`.
- **Settings › Output styles.**
  - A section in `settingsMeta.ts` / `settingsDefs.ts` containing:
    - a default picker;
    - the list of styles, each with its source (Built-in / User);
    - a read-only viewer for each user style's file.
  - Editing style files is **out of scope**; the copy points to `~/.claude/output-styles/`. Ruling: an editor is a later slice. The D-13 memory editor pattern would apply, but it is not needed for parity.
  - The section is lazy.
- **`+` menu.**
  - In the session composer's `PlusMenu`, add `Style ›` as a submenu (`PlusMenuEntry.submenu`) listing `availableOutputStyles` with the current one checked. Choosing one posts the route.
  - It is hidden when the list is empty or the engine is not Claude.
  - The DispatchBar `+` menu does not get it; the dispatch path uses the Settings default.
- **Slash commands** (`server/sessions/commands/catalog.py`, `dispatcher.py`).
  - **`/plan [on|off]`.**
    - Argument hint `[on|off]`.
    - It is a `control` command calling `orchestrator.set_permission_mode(sid, "plan")` for `on`, or `"default"` for `off`. `default` restores the pre-plan mode through the driver's existing overlay.
    - A bare `/plan` toggles according to the session's current mode.
    - It goes in `APP_ONLY`.
    - On a detached session, behave as `/effort` does there; check the D-14 dispatcher path.
  - **`/output-style [style]`.**
    - With a name, it validates and calls the same orchestrator action as the route.
    - Bare, it opens the `+` menu's Style submenu (as a `client` command, like `/fast`); or, if that is impractical, it shows the list as a notice. Pick one and record it in the report.
    - It goes in `APP_ONLY`, so the CLI's `/output-style` never passes through. Test that the CLI copy is not forwarded.
    - It is listed only for Claude sessions.
- **Contract test.** `server/tests/test_output_style_cli_contract.py` is a no-model test marked `slow`; it skips when the binary is absent. It follows `test_*cli_contract*.py` (D-14):
  - `initialize.available_output_styles` contains the five built-ins;
  - `--settings {"outputStyle":"Learning"}` sets `initialize.output_style`;
  - a live `apply_flag_settings {outputStyle}` reads back through `get_settings`.
- **Gates.**
  - Server: ruff, mypy `server/`, focused pytest, the RPC parity tests, and the full `server/tests -n 4` before the PR (run in a clean env: `env -i HOME=<tmp>`).
  - Postgres: check `_REQUIRED_EXTRAS` and `RECONCILE_EXEMPT` (nullable columns usually need nothing).
  - Core: `tests/` with `PYTHONPATH`.
  - Web: typecheck, lint, full vitest, build, budget, and e2e.

## Review Focus

1. **A daemon restart with a session style set:** the respawn reapplies it from the row (Task 1).
2. **An unknown style name, via the route or `/output-style`:** 409, and nothing is sent to the CLI (Task 1).
3. **The CLI's own `/output-style` typed in the composer:** it is never forwarded, and no `.claude/settings.local.json` is written into the worktree (Task 1).
4. **`/plan off` after `/plan on` from `acceptEdits`:** the session returns to `acceptEdits` (Task 1).
5. **A Codex session:** no Style submenu, no `/output-style`, and `/plan` still works (Task 2).

---

### Task 1: Server — output style column, spawn, live set, validation, commands

**Files:**
- Migration 0102; `models.py`; `AppSettings`.
- Orchestrator: `set_output_style`, `_spawn_flag_settings`, the cached style list.
- RPC, remote, fleet and facade.
- The route; the payload fields; the settings registry for `defaultOutputStyle` plus the list endpoint (for example `GET /api/settings/output-styles` returning `[{name, source, description, path?}]`).
- `configdir.py` `SHARE_ALLOWLIST`; `catalog.py`; `dispatcher.py`.
- Tests: `server/tests/test_output_style.py`, `test_plan_command.py`, the contract test, and `tests/test_configdir_isolation.py`.

- [ ] **Step 1: Failing tests.** Every bullet above, plus Review Focus 1–4.
- [ ] **Step 2–4.** Implement; run the focused tests, the parity tests, ruff and mypy.
- [ ] **Step 5: Commit** `feat(sessions): output styles per session with a default, app /plan and /output-style (D-16a)`

### Task 2: Web — `+` menu Style submenu, Settings › Output styles, commands

**Files:**
- `PlusMenu` and `Composer` (the Style submenu, with the tooltip copy).
- Hooks: `useSetOutputStyle`, `useOutputStyles`, plus the mocks.
- `types.ts` (`outputStyle`, `availableOutputStyles`).
- `settingsMeta`/`settingsDefs`, and a lazy `OutputStylesSection.tsx`.
- The client side of `/output-style` if it is a client command.
- Tests: unit tests, plus e2e `web/e2e/output-styles.spec.ts` (route-mocked, at 1440 and 390).

- [ ] **Step 1: Failing tests.**
  - The submenu lists the styles with the current one checked; choosing one posts.
  - It is hidden for Codex or when the list is empty (Review Focus 5).
  - Settings lists the styles with their source and saves the default.
  - The user-style viewer opens.
  - `/plan` and `/output-style` appear in the palette only where applicable.
- [ ] **Step 2–4.** Run the full web gates, budget and e2e.
- [ ] **Step 5: Commit** `feat(web): output style picker in the + menu and Settings › Output styles (D-16a)`

### Task 3: Benchmark and docs

- **Benchmark.**
  - `Output styles` → Y.
  - The app slash command row: add `/plan` and `/output-style`. It stays P only if `/add-dir` keeps it partial; the spike recommends leaving `/add-dir` out of D-16, so note it.
  - Regenerate §3 and report the target (D-15: 92%).
- **Spec.**
  - §8/§10/§11: the output-style mechanism, the D5/D6 rulings, the APP_ONLY reason, and the D-16 slicing (a/b/c).
  - Mark D-16a in §11.
- **Product docs.**
- **Ceilings.** measured + 0.3, only if they moved.
- [ ] Commit `docs: D-16a benchmark rerun and output-style docs`.
