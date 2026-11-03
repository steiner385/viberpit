# Desktop-parity D-10: ⌘K palette, grouping, read/unread, navigation keys — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sidebar and navigation parity with Desktop:
- a ⌘K palette over everything that's already listed;
- more group-by keys;
- per-viewer read/unread that follows you across devices;
- keys to switch sessions and to jump between your own prompts.

**Architecture:**
- **Unread:** a per-identity `session_views` table, compared against the session's existing `last_turn_at`. Joined in at the API layer from the DB, with no orchestrator or RPC change.
- **Grouping:** extends the one ordering engine, `session/ordering.ts`.
- **Navigation keys:** go through the one shell dispatcher (`shell/shortcuts.ts` + `useShortcuts`) and the one list order (`useSessionListState().ordering`).
- **Palette:** lazy-loaded and fed only from existing registries: sessions, archived sessions, `NAV_DEFS`, settings metadata, `SHORTCUTS`, `useGithubRepos`, and `Session.branch`.

**Tech Stack:** FastAPI + SQLAlchemy + pytest; React + TS, vitest, Playwright.

**Spec:** `docs/superpowers/specs/2026-11-01-ui-desktop-parity-design.md` §7 (sidebar and navigation, the keyboard table) and §11 D-10. Decisions D5, D12 and D20 are in `docs/audits/2026-11-01-claude-desktop-code-benchmark.md`.

**Code map:** see the D-10 ledger `repo-notes.md`. In short, nothing in D-10 exists yet.

## Global Constraints

- **Read/unread.**
  - **Deliberate deviation from the spec.** The spec's `last_viewed_seq` is implemented as `last_viewed_at`, compared against the session's `last_turn_at`. There is no durable per-session seq: the daemon seq resets on restart, and `last_turn_at` is the persisted "assistant output finished" marker. Record this in spec §7 and §10 during Task 6.
  - **Model:** `SessionView(identity: str, session_id: str FK, viewed_at: datetime)`, primary key `(identity, session_id)`, indexed on identity.
  - **Migration:** take the next free number at rebase time. D-9's #3237 adds `0095_session_rewinds`; if it has landed, chain off it. Also update the Postgres lists `_REQUIRED_EXTRAS` and `RECONCILE_EXEMPT`, and use `db_utc_now()`.
  - **Rule:** a session is **unread** for a viewer iff a `SessionView` row exists for that viewer and `last_turn_at > viewed_at`. With no row, it is read. That keeps existing sessions and automation-only sessions from turning bold the day this ships. Opening a session creates the row, so anything you open becomes trackable.
  - **API:**
    - `GET /api/sessions` and `GET /api/sessions/archived` add `unread: bool` per session for the calling identity, through one batched query per request.
    - `POST /api/sessions/{id}/viewed` upserts `viewed_at = db_utc_now()`.
    - `POST /api/sessions/{id}/unread` sets `viewed_at` just before `last_turn_at`, or deletes the row when there is no `last_turn_at`. Both return 204.
    - Identity comes from `current_identity`. The edge serves these from the DB. Never read orchestrator private seams (see memory "API reads of private orchestrator seams are inert").
  - **Web:**
    - The session title is bold when unread.
    - The card menu has **Mark as read** / **Mark as unread**.
    - Opening a session posts `viewed`. While the session stays open and visible, a new turn end (`updatedAt` changes) posts `viewed` again, debounced to 2 s. Nothing is posted while the tab is hidden (`document.visibilityState`).
    - The amber needs-you badge is unchanged; unread complements it.
- **Group-by.**
  - **New `GroupBy` members:** `branch`, `host`, `date`, `pr`.
    - `date` uses `updatedAt`, bucketed **Today / Yesterday / Previous 7 days / Older**, in the viewer's local time.
    - `pr` uses the most advanced state in `session.prs`, ordered **Open / Draft / Merged / Closed / No PR**. Merged beats closed; open beats draft.
    - Sessions with no `branch` or `host` go in a trailing group, `No branch` or `No host`.
  - **Labels** in `SessionListControls`: Branch, Host, Date, PR.
  - **Persistence** stays local (`lib/sessionOrder.ts`). Unknown stored values already fall back to the default.
  - **Archived ordering is unchanged.**
- **Keyboard.** Every row is registered in `SHORTCUTS` and dispatched by `useShortcuts`. Match on `e.code`, so macOS Option-rewritten keys still work.
  - **New ids:** `session.listPrev` / `session.listNext` (Alt+[ / Alt+]), `session.jumpN` (Alt+1–9), `transcript.promptPrev` / `transcript.promptNext` (Alt+↑ / Alt+↓, shown as ⌥↑/⌥↓ on Apple), and `app.palette` (⌘K / Ctrl+K). Never reuse `session.next` / `session.prev`, which a test asserts are absent.
  - **List order** is `useSessionListState().ordering.orderedSessions`. Alt+[ and Alt+] wrap around. With no session open, Alt+] opens the first session and Alt+[ the last.
  - **Retire the divergent chevron order.** `SessionHeader`'s prev/next chevrons switch to the same list order, and `useSessionNeighbors` is deleted (single source of truth).
  - **Alt+1–9** moves from `DispatchBar`'s local handler to the shell. It works with a session open and with focus anywhere outside text inputs. Inside the composer and dispatch textareas it also works, because they gain `!e.altKey` guards. Drop `compose.jump` from `DISPLAY_ONLY_ROWS`.
  - **Own prompt** means `role === 'user' && !origin`. Alt+↑ / Alt+↓ scroll the transcript to the previous or next own prompt relative to the topmost visible row. They unpin stick-to-bottom while jumping and focus nothing.
    - `TranscriptView` exposes the scroll through `SessionGestures` (`jumpPrompt(dir)`).
    - `Composer.tsx:357` and `DispatchBar.tsx:404` gain `!e.altKey` guards so Alt+arrows aren't eaten.
  - **Parser:** extend `chordFromKeys` in `shortcuts.test.ts` to Alt+arrows, Alt+brackets and Alt+digits. The reserved-chord test must still pass.
- **Palette (`AppPalette`).** Don't use the name `CommandPalette`; the composer already has one.
  - **Opening:** ⌘K / Ctrl+K opens it, and pressing it again closes it. It is lazy-loaded on first open and mounted in `Overlays` (`app.overlays`) at `z-palette`. It is a modal dialog with focus trap and Esc to close, and stays usable at 390 px.
  - **No new dependency.** `cmdk` isn't installed. Build it on the existing dialog and menu primitives with plain substring and fuzzy-prefix matching, written as a pure `paletteSearch.ts`.
  - **Sections and actions, in this order:**

    | Section | What runs on Enter |
    |---|---|
    | Sessions, live then archived (title, repo, branch, status) | Opens it (`sessionPath` / `archivedSessionPath`) |
    | Actions (`SHORTCUTS` rows with `group === 'app'` and a handler, plus New session) | Runs the same handler as the shortcut |
    | Navigate (`NAV_DEFS`, filtered by kiosk rules) | Navigates |
    | Settings (settings metadata) | Navigates (`settingsSectionPath`) |
    | Repos (`useGithubRepos`) | Sets the session list search to `repo:<name>`, or the list's existing repo filter if one exists — use it |
    | Branches (distinct `Session.branch` over live and archived) | Filters the list the same way |
  - **Empty query:** shows the 8 most recent sessions, then Actions.
  - **Keys:** ↑/↓ move, Enter runs, Esc closes.
  - **No new index.** Every entry comes from an existing registry or hook.
  - **Settings metadata split.** `settingsDefs.ts` eagerly imports every section component, so split it: `settingsMeta.ts` (id, group, label, description, keywords, icon) is imported by both the palette and `settingsDefs`, and `settingsDefs` adds the components. There is one source of the metadata.
- **Bundle.** The ceilings on master are 157.0 / 291.6 (D-9 raises them to 159.2 / 294.1). Report the numbers per task. Task 6 sets the ceilings to the measured values after rebase, under a controller ruling.
- **Gates.**
  - Server: ruff, mypy, focused pytest plus the postgres-compat and schema-reconcile tests.
  - Web: typecheck, lint, full vitest, budget, e2e.

## Review Focus

1. **Two devices, one viewer.** Opening a session on the phone clears its bold on the desktop at the next poll. Pinned in Tasks 1 and 2.
2. **Option-key layouts.** On macOS, Alt+[ / Alt+] / Alt+1–9 still fire, because they match on `e.code`. Pinned in Task 4.
3. **Typing in the composer.** Alt+↑ jumps between prompts, plain ↑ still recalls prompt history, and plain digits type. Pinned in Task 4.
4. **Palette over a long list.** It stays responsive with 500 sessions and 500 archived. The filter must be O(n) per keystroke, with no per-entry regex compile. Pinned in Task 5.
5. **Kiosk or read-only viewer.** The palette hides nav entries that `NAV_DEFS` hides in kiosk mode, and actions the viewer can't run. Pinned in Task 5.

---

### Task 1: Server — per-viewer read state

**Files:** `server/db/models.py` (`SessionView`), the migration, `server/api/sessions.py` (the `unread` field on both list endpoints, plus the two POST routes), the Postgres lists, and `server/tests/test_session_read_state.py`.

- [ ] **Step 1: Write failing tests.**
  - No row → `unread` is false.
  - `viewed`, then a later `last_turn_at` → true.
  - `viewed` again → false.
  - `unread` → true.
  - Viewer A's state never leaks to viewer B.
  - Archived sessions carry `unread` too.
  - The list endpoints issue one read-state query per request, regardless of session count.
  - An unknown session on POST → 404.
  - The migration round-trips.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the new tests, the session-list API tests, schema reconcile, postgres-compat, migrations, ruff and mypy. Expected: PASS.
- [ ] **Step 5: Commit** `feat(sessions): per-viewer read state for sessions (D-10)`

### Task 2: Web — unread display, mark read/unread, auto-viewed

**Files:**
- `web/src/api/types.ts` (`unread?: boolean`);
- `hooks.ts` (`useMarkSessionViewed`, `useMarkSessionUnread`), updating every whole-module `@/api/hooks` mock that renders an affected component;
- `SessionCard.tsx` (bold title, menu items);
- a `useAutoMarkViewed(sessionId, updatedAt)` hook, mounted where the open session renders;
- tests.

- [ ] **Step 1: Write failing tests.**
  - An unread card has a bold title, with `aria-label` including "unread".
  - The menu toggles Mark as read/unread and posts.
  - Opening posts `viewed` once.
  - A changed `updatedAt` while visible posts again, after the debounce.
  - While hidden, nothing is posted until the tab is visible again.
  - The needs-you badge is unaffected.
- [ ] **Step 2–4.** Then run the full vitest suite, typecheck and lint.
- [ ] **Step 5: Commit** `feat(web): unread sessions in bold, mark read/unread (D-10)`

### Task 3: Group by branch, host, date and PR

**Files:** `session/ordering.ts`, `useSessionOrdering.ts` (`LIVE_SPEC`), `SessionListControls.tsx`, and the ordering tests.

- [ ] **Step 1: Write failing tests.**
  - Each new grouping's group keys and labels, in order.
  - Date buckets at the boundaries, with a fixed `now` (no real clock).
  - The PR priority.
  - The trailing No-branch / No-host groups.
  - A stored unknown value falls back.
  - `buildNavItems` follows the groups.
- [ ] **Step 2–5.** Commit `feat(web): group sessions by branch, host, date or PR state (D-10)`

### Task 4: Session switching and prompt-jump keys

**Files:**
- `shell/shortcuts.ts` (the new rows);
- `AppShell.tsx` (handlers using `list.ordering`);
- `SessionHeader.tsx` (chevrons on the same order);
- delete `useSessionNeighbors.ts` and its test;
- `DispatchBar.tsx` (remove the local Alt+digit handler, add the `!altKey` guard);
- `Composer.tsx` (`!altKey` guard);
- `TranscriptView.tsx` and `sessionGestures.tsx` (`jumpPrompt`);
- `shortcuts.test.ts` (parser, reachability);
- `KeyboardSessions.test.tsx`.

- [ ] **Step 1: Write failing tests.**
  - Alt+] / Alt+[ move through the visible list order, wrap around, and respect filters and search.
  - Alt+3 opens the third session, both with a session open and from inside the composer.
  - With `e.code` set and `e.key` rewritten ("¡"), it still fires.
  - Alt+↑ / Alt+↓ scroll to the previous or next own prompt, skipping nudges (`origin`).
  - Plain ↑ in the composer still recalls history.
  - The chevrons agree with Alt+[ / Alt+].
  - The reserved-chord and reachability tests pass.
- [ ] **Step 2–4.** Then run the full vitest suite and the keyboard e2e, if one exists (grep `web/e2e` for `Alt+`).
- [ ] **Step 5: Commit** `feat(web): Alt+[ ] / Alt+1–9 session keys and Alt+↑↓ prompt jumps (D-10)`

### Task 5: ⌘K palette

**Files:**
- `features/palette/`: `AppPalette.tsx`, `paletteSearch.ts`, `usePaletteEntries.ts`, `index.ts`;
- `settingsMeta.ts` (split out of `settingsDefs.ts`);
- `Overlays.tsx` (lazy mount);
- `shortcuts.ts` (`app.palette`);
- `AppShell.tsx` (open/close state);
- tests;
- e2e `web/e2e/palette.spec.ts` (route-mocked).

- [ ] **Step 1: Write failing tests.**
  - `paletteSearch`: ranking (prefix, then word start, then substring), case-insensitive, stable within a section.
  - A 1,000-entry search completes under a fixed op budget (no regex per entry).
  - Entries:
    - archived sessions are included;
    - kiosk hides non-kiosk nav;
    - actions run the same handler as their shortcut;
    - a repo or branch sets the list filter;
    - the empty query shows recents then actions.
  - Keyboard: ⌘K / Ctrl+K toggles; ↑/↓/Enter/Esc work; focus is trapped and then restored.
  - The palette chunk is not in the entry chunk. Assert via the lazy boundary, and report the budget.
  - Settings still renders every section after the meta split.
  - e2e at 1440 and at 390: open the palette, type, Enter, and land on the session.
- [ ] **Step 2–4.** Then run the full web gates, the budget and the e2e.
- [ ] **Step 5: Commit** `feat(web): ⌘K palette over sessions, sections, settings, actions, repos and branches (D-10)`

### Task 6: Benchmark and docs

- **Benchmark.** In `scripts/desktop_parity_benchmark.py`, update the palette, grouping, unread and keyboard rows. Grep the row titles and set each to Y, or P with a note. Then regenerate audit §3 and report the target completion.
- **Spec.** In spec §7 and §10, record the `last_viewed_at` deviation and the shipped bindings.
- **Product docs.** Add the palette, grouping, unread and keyboard to the product docs (`web-dashboard-mobile.md`). Refresh the shortcut help sheet, which derives from `SHORTCUTS`, and check it.
- **Bundle ceilings.** Set them to the measured values after the controller rules.
- [ ] Run the script. Commit `docs: D-10 benchmark rerun and navigation docs`.
