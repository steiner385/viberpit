# Desktop-parity D-11: Transcript search and post-turn summary card — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:**
- **Search.** ⌘F (Ctrl+F) inside a session searches the whole transcript on the server, including turns that aren't loaded yet. Hits are highlighted, and next/prev pages to them.
- **Summary card.** After a turn, a summary card shows the outcome, the files the turn changed, and `+N −M`. The card replaces the pinned `YourTurnPanel`, which is deleted, including its "Likely complete" branch.

**Architecture:**
- **Search runs on the server.** It walks the engine-neutral replay window (`transcript_window(sid, limit=None)`), so it respects rewinds, forks, and Codex. It returns hits that each carry a turn ordinal. It is routed to the owning host like `transcript_source`.
- **The client highlights at render time.** The virtualizer keeps off-screen rows out of the DOM, so the browser's find can't reach them. To jump to a hit it loads older pages (`fetchOlderTurns`) down to that ordinal and calls `scrollToIndex`.
- **The summary card is a transcript tail row** (like D-9's rewind divider). Its file set comes from the last logical turn's edit, write, and Codex file-change tool blocks, client-side.

**Tech Stack:** FastAPI + pytest; React + TS, vitest, Playwright.

**Spec:** `docs/superpowers/specs/2026-11-01-ui-desktop-parity-design.md` §4 (Search, Post-turn summary card), §10 (`GET /sessions/{id}/search`), and §11 D-11. Decision D13.

**Code map:** the ledger's `repo-notes.md`.

## Global Constraints

### Search API
- **Route:** `GET /api/sessions/{id}/search?q=&limit=` returns `200 {hits:[{turnId, ordinal, role:'user'|'assistant', snippet, matchStart, matchEnd}], truncated:bool, totalTurns}`.
  - `matchStart` and `matchEnd` index into `snippet`.
  - **Matching.** All terms must match, case-insensitive substring, the same way as `server/sessions/search.py`. Reuse `parse_terms`; never add a second matcher. Only the first term's first occurrence per event is a hit position.
  - **Scope.** It covers user prompts and assistant text. Thinking and tool JSON are excluded, as are sub-agent events (`parentToolUseId`). The skip rules are the same as `events_text`, so share them: extract a single helper.
  - **Ordinals.** A user prompt takes the ordinal of the next TurnStart, per the `_window` semantics.
  - **Snippets.** Each is 80 chars around the match, trimmed on word boundaries. The default limit is 200 hits and the maximum is 1000; past the limit, `truncated: true`.
  - **Empty or whitespace `q`** returns 400 `SEARCH_EMPTY`.
- **Routing.** It is routed per session: orchestrator `transcript_search(sid, q, limit)`, facade passthrough, RemoteOrchestrator RPC, daemon registration, and fleet `_PER_SESSION` membership. An unknown session returns 404; an old daemon returns 409 `daemon_outdated`.
  - **Failures.** It never raises past the route. A replay failure returns `{hits:[], truncated:false}` and logs at INFO.
- **The list search is unchanged.**

### Search UI
- **⌘F / Ctrl+F row.** Add a `transcript.search` row in `SHORTCUTS`, matched on `e.key` like ⌘K (with a `KeyF` fallback for non-Latin layouts).
  - **When it fires:** only when a session is open and focus is in the transcript, the composer, or nowhere (`document.body`). Everywhere else (Files/Changes tabs, Settings, other inputs) it does not fire, so the browser's find works there.
  - **Focusable scroller.** Give the transcript scroller `tabIndex={-1}` so a click focuses it.
- **The bar.** It opens at the top of the transcript pane with the query input, `N of M` (or `M+` when truncated), ↑/↓ buttons, and ✕.
  - **Keys:** Enter goes to the next hit, Shift+Enter to the previous, Esc closes and returns focus. ⌘F while the bar is open refocuses and selects the input.
  - **Search requests** are debounced 250 ms, at least 2 chars, with stale responses discarded.
- **Jumping to a hit.** If `hit.ordinal < earliestTurn`, load older pages via the existing `onLoadOlder` path until the turn is loaded, at most 2000 turns. Then `scrollToIndex` the row whose turn id matches (or whose ordinal matches), with `align:'center'`, and hold it like `jumpPrompt`.
  - **Single scroll API:** extend the `TranscriptJump` handle with `scrollToTurn(turnId, ordinal)`; no second scroll API.
- **Highlighting.** Highlight every occurrence of each term inside rendered user and assistant text with `<mark>`, and give the current hit a stronger style. It is applied at render time through a context (`TranscriptSearchContext`), and only in text and markdown blocks, never in code or tool blocks.
  - **Write a pure `highlightRanges(text, terms)` helper** with tests.
- **Capability.** No capability is needed, because every engine replays.
- **On the palette.** The new row is a palette action (D-10's actions come from the handlers), so it appears there too. It is gated on an open session.

### Summary card
- **Where it renders.** It is a `summary` row appended by `buildRenderRows` after the last turn, before the D-9 rewind divider. It shows only when the session is not busy and the last logical turn (every assistant `Turn` since the last user turn) has ended.
- **The outcome line** uses the existing labels from `statusTier.ts` `PRESENTATION` (single source):

  | Session state | Shown |
  |---|---|
  | `complete` / `ended` | **Complete** |
  | `handed_back` | **Handed back** + the hand-back reason |
  | `done` | **Your turn** |
  | `needs_attention` | **Turn failed** |
  | `limited` | the limited label |
  | `waiting_*` / `ready_review` | **no card**; their own cards already own the moment |

  - **Never write "Done".** See the memory rule *Done means turn, Complete means objective*.
- **"Likely complete".** When `suspectedCompleteReason` is set, the card shows the "Likely complete" line with **Mark complete** and **Not done**, using the existing `useResolveSuspectedComplete`. That branch moves here from `YourTurnPanel`.
- **"Asked you" mode** (busy plus a hand-back reason) stays as the card's note while busy. Port it from `YourTurnPanel`; behaviour must not regress.
- **Files.** Distinct paths from that turn's tool blocks: `toolKind` edit/write `input.file_path`, plus Codex `fileChanges`/`changedPaths`. Show at most 5, then `+K more`.
  - **Counts.** `+N −M` is the sum of `toolInputToFileDiff` counts where they exist. Codex paths have none, so when any path is uncounted, show the file count only. Render with the existing `DiffCounts`.
  - **No files:** omit the files section.
  - **Open Changes** goes to `setView('changes')`.
- **Delete `YourTurnPanel`.** Remove `YourTurnPanel.tsx`, its tests, and the `SessionBody` mount in the same PR, and port the assertions that still apply to the card tests.
  - **The list card's "Likely complete" pill (`SessionCard`) stays**; it is not part of this cut.
- **Accessibility.** The card is a `section` with `aria-label="Turn summary"`.

### Gates and bundle
- **Gates.**
  - Server: ruff, mypy, focused pytest, and the RPC parity tests.
  - Web: typecheck, lint, full vitest, budget, and e2e.
- **Bundle.** The ceilings are 159.0 / 298.1. Lazy-load the search bar. Task 4 sets the ceilings to measured + 0.3 under a controller ruling.

## Review Focus

1. **A hit in a turn that isn't loaded yet** (session with 300 turns, hit at ordinal 5): next pages back, lands centred, and the highlight shows. Pinned in Task 2's e2e.
2. **A rewound or forked session:** hits never land in cut turns. Pinned in Task 1, with a replay that has an active-chain cut.
3. **⌘F in the Files tab, Settings, or another input:** the browser's find opens and our bar doesn't. Pinned in Task 2.
4. **A Likely-complete session after the panel is deleted:** Mark complete and Not done still work, from the card. Pinned in Task 3.
5. **A session on another host:** search runs on the owner daemon. Pinned in Task 1 with a UDS round-trip.

---

### Task 1: Server transcript search

**Files:**
- Modify:
  - `server/sessions/search.py`: the shared skip rules plus `search_events(events, terms, limit)`.
  - The orchestrator: `transcript_search`.
  - `facade.py`, `remote.py`, `server/ipc/orchestrator_rpc.py`, `fleet_orchestrator.py` (`_PER_SESSION`).
  - `server/api/sessions.py`: the route, declared after `/search`.
- Test: `server/tests/test_transcript_search.py`.

- [ ] **Step 1: Write failing tests.**
  - All-terms, case-insensitive matching. A hit's ordinal matches the `_window` cursor for both user and assistant hits.
  - Thinking, tool JSON, and sub-agent text are not hits.
  - Snippet and match offsets are exact.
  - The limit sets `truncated`.
  - Empty `q` gives 400. An unknown session gives 404.
  - A rewound transcript has no hits in the cut turns; use a fixture with an explicit rewind row.
  - The Codex-style event path works, using a fake `transcript_window`.
  - A UDS RPC round-trip runs on the owner, `_PER_SESSION` includes the method, and `daemon_outdated` is mapped.
  - Nothing logs at WARNING.
- [ ] **Step 2–4.** Then run the focused tests plus the existing list-search tests, ruff, and mypy.
- [ ] **Step 5: Commit** `feat(sessions): server-side transcript search with turn ordinals (D-11)`

### Task 2: Search UI

**Files:**
- Create: `features/sessions/transcript/search/` with `TranscriptSearchBar.tsx`, `useTranscriptSearch.ts`, `highlightRanges.ts`, and `TranscriptSearchContext.tsx`.
- Modify:
  - `shortcuts.ts`: the row.
  - `AppShell.tsx`: the handler.
  - `TranscriptView.tsx`: scroller `tabIndex`, `scrollToTurn`, and the bar mount.
  - `sessionGestures.tsx`: `TranscriptJump`.
  - The text/markdown block renderers: highlight.
  - `api/hooks.ts` or `api/transcript.ts`: `useTranscriptSearch` / `fetchTranscriptSearch`. Update the hook mocks if needed.
- Tests: unit tests, plus e2e `web/e2e/transcript-search.spec.ts` (route-mocked).

- [ ] **Step 1: Write failing tests.**
  - **`highlightRanges`:** multiple terms, overlaps, case-insensitive matching, no regex injection (e.g. `a.*b`).
  - **⌘F:** fires in the transcript, in the composer, and with no focus. It does not fire in the Files tab, in Settings, or in another input (asserting `defaultPrevented` is false).
  - **Bar:**
    - Enter and Shift+Enter cycle through the hits with wraparound.
    - Esc closes the bar and restores focus.
    - A stale response is ignored.
    - `M+` is shown when the results are truncated.
  - **Paging:** a hit below `earliestTurn` triggers the load-older path until the turn is loaded, then the view scrolls to it.
  - **Highlighting:** text blocks get `<mark>` highlights; code and tool blocks never do.
  - **Palette:** the action is present only when a session is open.
  - **E2E:** a 300-turn mocked session, with a hit at ordinal 5, run at 1440 and 390.
- [ ] **Step 2–4.** Then run the full web gates, the budget, and the e2e.
- [ ] **Step 5: Commit** `feat(web): ⌘F transcript search with paged next/prev and highlights (D-11)`

### Task 3: Post-turn summary card; delete YourTurnPanel

**Files:**
- Create: `features/sessions/transcript/TurnSummaryCard.tsx` and the pure helpers `session/turnSummary.ts`.
  - `lastLogicalTurn(turns)`
  - `changedFiles(turns)`, returning `{path, plus?, minus?}`
  - `summaryOutcome(status, handBackReason, suspected)`
- Modify:
  - `transcriptRows.ts`: the `summary` row.
  - `TranscriptView.renderRow`.
  - `SessionBody.tsx`: remove the panel mount, and feed the card its inputs through the runtime.
- Delete: `YourTurnPanel.tsx` and its tests; port the assertions that still apply.
- Tests: helper tests, card tests, and `SessionBody` tests. Extend an existing e2e (`transcript-virtualization` or `dashboard`) to cover a card after a turn.

- [ ] **Step 1: Write failing tests.**
  - Each status maps to the right label from `PRESENTATION` (and "Done" never appears). Waiting/ready_review shows no card.
  - Likely-complete shows the line plus Mark complete / Not done, which call `useResolveSuspectedComplete` with the same payload the panel sent.
  - "Asked you" while busy.
  - Files:
    - Paths are deduplicated, capped at 5, and followed by `+K more`.
    - Counts are summed.
    - An uncounted Codex path means the file count is shown with no `+/−`.
    - A turn with no file changes omits the files section.
    - Open Changes switches the view.
  - The card sits after the last turn and before the rewind divider.
  - `YourTurnPanel` no longer exists (a grep test).
- [ ] **Step 2–4.** Then run the full web gates, the budget, and the e2e.
- [ ] **Step 5: Commit** `feat(web): post-turn summary card replaces the Your-turn panel (D-11)`

### Task 4: Benchmark and docs

- Update the benchmark rows for transcript search and the summary card. Regenerate audit §3 and report the target.
- Spec §4 and §10: record the shipped search API, the focus rule, the card's placement and outcome mapping, and the panel deletion.
- Product docs: add search and the summary card.
- Set the ceilings to measured + 0.3, with the reason in the commit message.
- [ ] Commit `docs: D-11 benchmark rerun and search/summary docs`.
