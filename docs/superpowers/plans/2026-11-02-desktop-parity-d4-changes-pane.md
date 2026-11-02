# Desktop-parity D-4: Changes pane — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give each session a **Changes** tab with the session's aggregate worktree diff against its base, a `+N −M`
chip in the header, unified/split and whitespace toggles, and next/previous-change navigation. One shared diff
renderer also replaces today's inline tool diffs.

**Architecture:**
- **Git layer.** A pure git layer in `src/viberpit/worktree_diff.py` computes a structured diff for a worktree
  directory. It is local and read-only: no fetch, no index writes.
- **Orchestrator method.** `session_diff(session_id, …)` runs that layer off the event loop on the host that owns
  the session. It is reachable over the daemon RPC and the fleet router, like `mcp_status_for`.
- **HTTP endpoint.** `GET /api/sessions/{id}/diff` returns the model to the browser.
- **Web.** Renders the model with one `DiffRenderer`. The Changes tab is lazy-loaded so it stays off the first-paint
  bundle.

**Tech Stack:** Python (git CLI via the repo's `_run` helper pattern), FastAPI, pytest; React + TS, highlight.js
(already a dependency), vitest.

**Spec:** `docs/superpowers/specs/2026-11-01-ui-desktop-parity-design.md` §2 (tabs, chip), §4 (inline diffs), §10
(diff seam) and §11 row D-4. Decisions: `docs/audits/2026-11-01-claude-desktop-code-benchmark.md` D1, D4, D19. Line
comments are D-5 and out of scope here.

## Global Constraints

- **Base.** The diff base is `git merge-base HEAD <origin default>`, where the origin default is
  `refs/remotes/origin/HEAD`, as in `worktrees.commits_ahead_of_default`. With no `origin/HEAD`, the base is `HEAD`,
  so only uncommitted changes show, and the response says `"base": null`.
- **What the diff covers.** The diff runs from base to the **working tree**. It covers committed and uncommitted
  tracked changes, plus untracked, non-ignored files shown as `added`.
- **Read-only.** Use only local refs. Never write: no `git add -N`, no index refresh with side effects. Use
  `--no-optional-locks`.
- **Caps.**
  - At most 300 files. Beyond that, set `truncated: true` and list the rest without hunks.
  - Per-file patch text up to 256 KiB, else `tooLarge: true` with no hunks.
  - Total hunk payload up to 2 MiB.
  - Binary files report `binary: true` with no hunks.
- **Model.** The model is identical on server and client. In TypeScript:

  ```ts
  type DiffLine = { kind: 'context' | 'add' | 'del'; oldNo: number | null; newNo: number | null; text: string }
  type DiffHunk = { oldStart: number; oldLines: number; newStart: number; newLines: number; header: string; lines: DiffLine[] }
  type FileDiff = { path: string; oldPath: string | null; status: 'added' | 'modified' | 'deleted' | 'renamed';
                    additions: number; deletions: number; binary: boolean; tooLarge: boolean; hunks: DiffHunk[] }
  type SessionDiff = { base: string | null; head: string | null; files: FileDiff[]; additions: number; deletions: number; truncated: boolean }
  ```

  The server emits camelCase.
- **Endpoint.** `GET /api/sessions/{id}/diff?summary=1&ignoreWhitespace=1`.
  - `summary=1` returns files with `hunks: []`, for the chip and cheap polling.
  - `ignoreWhitespace=1` maps to `git diff -w`.
  - A session with no cwd, or a cwd that isn't a git work tree, returns 200 with `files: []` and `base: null`. That
    is not an error.
  - An unknown session returns 404.
- **Chip.** The `+N −M` chip shows when `additions + deletions > 0`. Clicking it opens the Changes tab. The tab is
  listed only when the summary has at least one file.
- **Views.** Unified is the default below 1024px, split at 1024px and wider. The toggle is remembered per browser
  under the existing preference namespace (grep for the `viberpit.`/`ccswitch.` localStorage prefix helper).
- **Refetch.** The summary refetches when a turn ends (the session status leaves busy) and when the tab opens.
  Nothing polls while idle.
- **Bundle.** Load the Changes tab with `React.lazy`. Only the chip and the inline renderer ship in the entry. Run the
  budget gate.

## Review Focus

1. **A repo whose default branch moved after the worktree was cut:** the merge-base, not `origin/HEAD` itself, keeps
   other people's changes out of the session diff. Pinned in Task 1.
2. **A rename with edits:** `status: renamed`, with `oldPath` set, and hunks present. Pinned in Task 1.
3. **A session on another fleet host:** the diff is computed on that host, never on the edge. Pinned in Task 2 with a
   RemoteOrchestrator test.
4. **Paths with spaces or unicode:** use `-z` or NUL-safe parsing, or `core.quotePath=false`. Pinned in Task 1.
5. **A Codex Edit block** (file list only, no text): the inline renderer still shows the file-change list, as it does
   today. Pinned in Task 3.

---

### Task 1: Git diff layer (`src/viberpit/worktree_diff.py`)

**Files:**
- Create: `src/viberpit/worktree_diff.py`.
- Test: `tests/test_worktree_diff.py`. This is the core suite, so run it with `PYTHONPATH=$PWD/src` per repo notes,
  and build real temp git repos in fixtures.

**Interfaces:**
- Produces: `session_diff(cwd: str, *, summary: bool = False, ignore_whitespace: bool = False) -> dict`, which
  returns the `SessionDiff` dict in camelCase. Constants: `MAX_FILES = 300`, `MAX_FILE_PATCH_BYTES = 262144`,
  `MAX_TOTAL_BYTES = 2097152`.

- [ ] **Step 1: Write failing tests** against a temp repo that has a bare "origin" with `origin/HEAD` set:
  - **Modified file:** one hunk with correct `oldNo`/`newNo` and `additions`/`deletions`.
  - **New untracked file:** `status: added`, with hunks from `/dev/null`.
  - **Deleted file:** `status: deleted`.
  - **Rename with an edit:** `renamed`, `oldPath` set.
  - **Binary file:** `binary: true`, no hunks.
  - **Committed and uncommitted:** a change in a commit ahead of the base and an uncommitted change both appear.
  - **Default branch moved:** a commit added to origin's default after the branch point does not appear, because
    the merge-base is used.
  - **No `origin/HEAD`:** `base: null`, and only uncommitted changes appear.
  - **Not a git dir:** `files: []`.
  - **Path handling:** a path with a space and a unicode name round-trips.
  - **Caps:** file count over `MAX_FILES` gives `truncated`; an oversized file gives `tooLarge`.
  - **Flags:** `summary=True` gives empty hunks but the same totals; `ignore_whitespace=True` drops a
    whitespace-only change.
  - **Read-only:** the index mtime is unchanged after a call.
- [ ] **Step 2: Run** `PYTHONPATH=$PWD/src <venv>/python -m pytest -q tests/test_worktree_diff.py`. Expected: FAIL.
- [ ] **Step 3: Implement.** Use `git -C cwd --no-optional-locks`. Use `diff --numstat -z` plus `--name-status -z`
  for the file list, `diff -U3 --no-color --no-ext-diff` per batch for the hunks, and `ls-files --others
  --exclude-standard -z` for untracked files. Untracked file hunks are synthesized from the file content (UTF-8 with
  replacement; NUL bytes mean binary).
- [ ] **Step 4: Run** the tests, then `ruff check src/`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(worktrees): structured session diff against the merge-base (D-4)`

### Task 2: Orchestrator, RPC and HTTP endpoint

**Files:**
- Modify: `server/sessions/orchestrator.py`: `async def session_diff(self, session_id: str, *, summary: bool = False, ignore_whitespace: bool = False) -> dict`
  runs `asyncio.to_thread(worktree_diff.session_diff, rec.cwd, …)`. An unknown session raises the orchestrator's
  usual not-found error.
- Modify: `server/ipc/orchestrator_rpc.py` (`_SIMPLE`), `server/sessions/remote.py` (`RemoteOrchestrator.session_diff`)
  and the fleet router. Follow `mcp_status_for` exactly. If there is a sessions facade, update it too:
  `grep -n "mcp_status_for" server/sessions/*.py server/api/*.py`.
- Modify: `server/api/sessions.py`: add `GET /{session_id}/diff`, auth like its siblings, with `summary` and
  `ignoreWhitespace` query booleans.
- Test: `server/tests/test_session_diff_api.py`.

**Interfaces:**
- Consumes: `worktree_diff.session_diff` (Task 1).
- Produces: `GET /api/sessions/{id}/diff` and `orch.session_diff(...)`.

- [ ] **Step 1: Write failing tests**
  - **Real diff:** the API on a session whose `cwd` is a temp repo returns the model.
  - **Summary:** `summary=1` gives empty hunks.
  - **Unknown session:** 404.
  - **No cwd:** 200 with empty files.
  - **Remote:** a `RemoteOrchestrator` with a fake client calls `session_diff` with the same kwargs.
  - **Auth:** a request without identity returns 401, like the siblings.
- [ ] **Step 2: Run** the scrubbed-env pytest. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the new tests, any facade or RPC parity tests (`grep -rln "_SIMPLE\|mcp_status_for" server/tests`),
  ruff and mypy. Expected: PASS.
- [ ] **Step 5: Commit** `feat(sessions): GET /sessions/{id}/diff routed to the owning host (D-4)`

### Task 3: Shared `DiffRenderer`, replacing the inline tool diff

**Files:**
- Create: `web/src/features/sessions/diff/types.ts` (the model), `web/src/features/sessions/diff/DiffRenderer.tsx`
  and `web/src/features/sessions/diff/toolDiff.ts`.
  - `DiffRenderer` takes `{ file: FileDiff; view: 'unified' | 'split'; maxLines?: number }`. It renders hunk
    headers, line numbers and `+`/`-` gutters, with syntax highlighting through highlight.js by file extension;
    unknown languages render as plain text. Split view pairs del/add runs side by side.
  - `toolDiff.ts` provides `toolInputToFileDiff(input): FileDiff | null`, built from the existing
    `lib/toolContent.buildLines`. It returns one hunk, and null when there is no text (the Codex file list).
- Modify: `transcript/ToolResult.tsx`. Render `DiffRenderer` (unified, `maxLines` 200 with "Show all") for
  Edit/Write/MultiEdit. Keep the Codex `FileChangeList` behaviour, moving it next to the renderer if `DiffView.tsx` is
  deleted. **Delete `transcript/DiffView.tsx`** and its test; move any still-relevant cases to the new tests.
- Test: `DiffRenderer.test.tsx`, `toolDiff.test.ts`, `ToolResult.test.tsx`.

**Interfaces:**
- Produces: `FileDiff`/`SessionDiff` TS types, `DiffRenderer`, `toolInputToFileDiff`.

- [ ] **Step 1: Write failing tests**
  - **Unified:** shows old and new line numbers and the add/del classes.
  - **Split:** pairs a 2-del/3-add run into rows, with an empty cell for the unpaired add.
  - **Highlighting:** a `.ts` file gets highlighted spans; `.unknownext` is plain text.
  - **Edit tool:** `toolInputToFileDiff` on an Edit input gives the same lines today's `buildLines` produced.
  - **Codex:** a Codex change list returns null, and ToolResult renders the file-change list.
  - **Long diffs:** `maxLines` truncates, and "Show all" expands.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full web gates and the budget. Expected: PASS. Report any budget overage; don't raise it.
- [ ] **Step 5: Commit** `feat(web): shared DiffRenderer replaces the inline tool diff (D-4)`

### Task 4: Changes tab, header chip, refetch, navigation

**Files:**
- Modify: `web/src/api/hooks.ts`, adding `useSessionDiff(id, { summary, ignoreWhitespace })` (React Query). Then add
  the field to the explicit `@/api/hooks` mocks the full suite needs. Run the FULL vitest to find them.
- Create: `web/src/features/sessions/diff/ChangesTab.tsx`, loaded lazily. It shows a file list, with each file
  collapsible and rendered by `DiffRenderer`, plus a toolbar:
  - a `Unified`/`Split` segmented toggle, persisted per browser;
  - a `Hide whitespace` toggle;
  - `Previous change` / `Next change` buttons that scroll the next add/del run into view and focus it.

  It also shows the base (`vs <short base sha>`, or "uncommitted changes only" when `base` is null), plus the
  `truncated`, `tooLarge` and binary notices.
- Create: `web/src/features/sessions/diff/DiffChip.tsx`, rendering `+N −M` in success/danger tokens. It is a button
  that opens the Changes tab, and it sits in `SessionHeader` beside the status pill.
- Modify: `SessionBody.tsx`. Add a `Changes` tab after Conversation, listed only when the summary has files. The
  tab id is in the URL as `?view=changes`, per spec §2. Keep the Browser tab as is. Refetch the summary when
  `rt.state.status` leaves busy.
- Test: `ChangesTab.test.tsx`, `DiffChip.test.tsx`, `SessionBody` tab tests, a hooks test, and an e2e
  `web/e2e/changes-tab.spec.ts` (mock the diff API with `page.route`, as other specs do) at 390 and 1440px with no
  horizontal overflow.

- [ ] **Step 1: Write failing tests**
  - **Chip:** hidden at 0/0; shows `+12 −1`; clicking it opens the Changes tab.
  - **Tab visibility:** listed only when files exist.
  - **View default:** unified below 1024px, split at 1024px and wider; the toggle persists.
  - **Whitespace:** the toggle refetches with `ignoreWhitespace=1`.
  - **Navigation:** Next change focuses the next change run.
  - **Notices:** `base: null` shows the uncommitted-only note; `truncated` shows a notice.
  - **Refetch:** status busy → idle triggers a summary refetch.
  - **E2E:** the tab renders at 390 and 1440px without overflow.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full web gates, the budget and the new e2e spec. Expected: PASS. Budget: the entry may grow
  by the chip only. Report the numbers.
- [ ] **Step 5: Commit** `feat(web): Changes tab, +N −M chip, unified/split and navigation (D-4)`

### Task 5: Benchmark and docs

- Modify `scripts/desktop_parity_benchmark.py`:
  - `Changes pane: aggregate diff, unified/split, line comments` → `P`, with the note `line comments → D-5`.
  - `Inline tool diffs` → `Y`, with the note `shared DiffRenderer, highlighted`.
- Regenerate audit §3 from the script output. Document the Changes tab and chip in the product docs for the session
  view. Raise the bundle ceilings only if the controller rules it.
- [ ] Run the script. Expected: Target completion ≥ 72%. Commit `docs: D-4 benchmark rerun and Changes docs`.
