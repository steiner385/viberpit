# Desktop-parity D-7: @-file mentions, Files tab, clickable paths — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Type `@` in the session composer to pick a worktree file, inserted as `@path`, or another session. Browse
worktree files in a read-only **Files** tab. Make file paths in the transcript open that tab at the right line.

**Architecture:** This mirrors D-4.
- A local, read-only, hardened git/file layer runs on the host that owns the session, reached through the
  orchestrator → RPC → RemoteOrchestrator → fleet `_PER_SESSION` path.
- Two HTTP endpoints sit on top of it:
  - `GET /api/sessions/{id}/files?q=&limit=` returns a fuzzy-ranked list of worktree files.
  - `GET /api/sessions/{id}/files/content?path=` returns one file, read-only and capped.
- Path containment reuses the single resolver `server/permission_rules/patterns.worktree_rel_path`. Its plain-path
  core is extracted as `worktree_rel(path, cwd)`, so there is no parallel implementation.
- The web side reuses D-4's lazy highlight chunk and D-5's composer plumbing.

**Tech Stack:** Python, FastAPI, pytest; React + TS, vitest, Playwright.

**Spec:** `docs/superpowers/specs/2026-11-01-ui-desktop-parity-design.md` §2 (Files tab), §3 (`@`), §4 (file paths),
§10 (file seams), §11 row D-7. Decisions: `docs/audits/2026-11-01-claude-desktop-code-benchmark.md` D1, D2, D13.

## Global Constraints

- **File list:**
  - Comes from `git ls-files --cached --others --exclude-standard -z`. Use the same hardened git invocation as
    `src/viberpit/worktree_diff.py` (no system config, `core.hooksPath=/dev/null`, repo-redirect env stripped).
    Import its helpers; don't copy them.
  - Capped at **20,000** entries scanned. Beyond that the response sets `truncated: true`.
  - Fuzzy ranking runs server-side, as a case-insensitive subsequence match:
    - A basename match scores higher than a directory match.
    - Contiguous runs score higher than scattered matches.
    - Shorter paths win ties.
  - Returns the top `limit` results, default 50, max 200.
  - An empty `q` returns recently modified files first (by mtime, top `limit`).
  - Response: `{ files: [{ path: string }], truncated: boolean }`. `path` is relative to the worktree root and
    `/`-separated.
- **File content:**
  - `path` must resolve inside the worktree via `worktree_rel(path, cwd)`. That means realpath, `commonpath`, no `..`,
    no `~`, no NUL, and the target must be a regular file. Otherwise the endpoint returns 400 with code
    `PATH_OUTSIDE_WORKTREE`. A missing file returns 404 `FILE_NOT_FOUND`.
  - Reads use `O_RDONLY | O_NONBLOCK | O_NOFOLLOW`, then `fstat` to confirm a regular file, as D-4 does.
  - Capped at **512 KiB**. A larger file returns the first 512 KiB with `truncated: true`.
  - A NUL byte in the first 8 KiB means `binary: true` and no `text`.
  - Response: `{ path, text: string | null, binary, truncated, size }`.
  - Protected paths (`.git/**`) return 400 `PATH_OUTSIDE_WORKTREE`. Other dotfiles are readable, since they belong to
    the authenticated operator.
- **Status codes:**
  - An unknown, ended or archived session returns 404.
  - An old daemon (UnknownMethod) returns 409 `daemon_outdated`, as for D-4's diff.
  - A session with no cwd, or a non-git cwd, returns 200 with an empty list or 404 `FILE_NOT_FOUND`.
- **`@` menu (session composer):**
  - Typing `@` opens one menu with two groups, **Files** first and then **Sessions**. Files are queried with a 120 ms
    debounce.
  - Choosing a file inserts `@<path> ` (the CLI resolves it).
  - Choosing a session inserts the existing session-mention token format (`MentionMenu`'s current behaviour).
  - The assistant drawer keeps its sessions-only behaviour.
  - The menu reuses `MentionMenu` and `mentionQueryAt`; extend them, don't fork them.
  - Keyboard: ↑/↓ move, Enter/Tab select, Esc closes.
  - It is hidden on engines without a `cwd`, which have no files group.
- **Files tab (`?view=files&file=<path>&line=<n>`):**
  - Lazy-loaded. It holds a search box (the same files query) and a viewer with line numbers and syntax highlighting
    from D-4's lazy `highlightChunk`.
  - `line` scrolls to and highlights that line. Large files render virtualised or capped, with a `truncated` notice.
    Binary files show `Binary file — not shown`.
  - The tab is listed whenever the session has a `cwd`.
  - It adds `files` to `sessionView.ts`.
- **Clickable paths (transcript):**
  - The existing `filePathRef` detection gains optional `:line` (and `:line:col`) parsing.
  - A path that resolves under the session's `cwd` opens the Files tab at the line. An absolute path is converted to
    worktree-relative. Anything else keeps today's copy behaviour.
  - Secondary actions sit in a small menu: **Copy path** and **Attach as @** (inserts `@path` into the composer
    draft).
- **Gates:** ruff, mypy, focused pytest; typecheck, lint `--max-warnings 0`, full vitest, budget, e2e.

## Review Focus

1. **Content endpoint escapes:** a symlink inside the worktree pointing outside, `../`, an absolute path outside, a
   FIFO, `.git/config`. Each returns 400 and none is read. Pinned in Task 1.
2. **A 50k-file repo:** the scan stops at the cap with `truncated`, and response time stays bounded. Pinned in Task 1
   with a cap mock.
3. **A remote-host session:** the list and content are computed on the owning daemon. Pinned in Task 2.
4. **The `@` menu while the composer holds a slash command, or inside a code span:** no file menu (follow
   `mentionQueryAt` rules). Pinned in Task 4.
5. **A transcript path that looks like a file but is outside cwd** (e.g. `/etc/hosts`): copy-only, never a Files-tab
   link. Pinned in Task 5.

---

### Task 1: Files layer (list + read), hardened

**Files:**
- Modify `server/permission_rules/patterns.py`: extract `worktree_rel(path: str, cwd: str | None) -> str | None` from
  `worktree_rel_path`. `worktree_rel_path` then becomes `tool_path_arg` + `worktree_rel`. Behaviour is unchanged, and
  the existing tests stay green.
- Create `server/sessions/worktree_files.py`:
  - `list_files(cwd, q='', limit=50) -> dict`
  - `read_file(cwd, path) -> dict`, raising `FileAccessError(code)`.
  - Reuse `src/viberpit/worktree_diff.py`'s hardened git env and args helpers (export them if they're private).
- Test: `server/tests/test_worktree_files.py`, using real temp git repos.

- [ ] **Step 1: Write failing tests**
  - **Listing:**
    - Tracked and untracked files are listed; ignored files are not.
    - Fuzzy ranking: `q='comp'` ranks `src/Composer.tsx` above `src/a/b/c/o/m/p.ts`; a basename match beats a
      directory match.
    - Empty `q` lists by mtime.
    - The cap gives `truncated`.
    - `limit` is capped at 200.
  - **Reading:**
    - Text is returned.
    - A file over 512 KiB is truncated.
    - A binary file returns `binary: true` and `text: null`.
  - **Escapes and errors:**
    - A symlink pointing outside → `PATH_OUTSIDE_WORKTREE`.
    - `../x` → `PATH_OUTSIDE_WORKTREE`.
    - An absolute path outside the worktree → `PATH_OUTSIDE_WORKTREE`.
    - `.git/config` → `PATH_OUTSIDE_WORKTREE`.
    - A FIFO → `PATH_OUTSIDE_WORKTREE` (never blocks).
    - A missing file → `FILE_NOT_FOUND`.
  - **Patterns tests:** the existing `patterns` tests still pass.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the new tests, the `-k "pattern or permission or accept_edits"` tests, ruff and mypy. Expected:
  PASS.
- [ ] **Step 5: Commit** `feat(sessions): hardened worktree file list and read layer (D-7)`

### Task 2: Endpoints, routed to the owning host

**Files:**
- Modify:
  - the orchestrator: `session_files(session_id, *, q='', limit=50)` and `session_file(session_id, *, path)`. Both run
    off-loop via `asyncio.to_thread` and map `FileAccessError` to typed errors.
  - `server/ipc/orchestrator_rpc.py` `_SIMPLE`.
  - `server/sessions/remote.py`, preserving the codes.
  - the fleet `_PER_SESSION`.
  - the facade.
  - `server/api/sessions.py`: add `GET /{id}/files` and `GET /{id}/files/content`, with `UnknownMethod` →
    `daemon_outdated`.

  Follow D-4's `session_diff` exactly (grep it).
- Test: `server/tests/test_session_files_api.py`.

- [ ] **Step 1: Write failing tests**
  - **Real repo:** a session whose cwd is a temp repo returns the list and content.
  - **Errors:** an escape gives 400 with its code; a missing file gives 404; an unknown session gives 404.
  - **Old daemon:** gives 409 `daemon_outdated`.
  - **Remote:** a RemoteOrchestrator RPC round trip works (copy D-4's UDS round-trip test pattern).
  - **Auth:** an unauthenticated request gives 401.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the focused tests, the RPC parity tests, ruff and mypy. Expected: PASS.
- [ ] **Step 5: Commit** `feat(sessions): GET /sessions/{id}/files and /files/content routed to the owning host (D-7)`

### Task 3: Files tab (read-only viewer)

**Files:**
- Modify `web/src/api/hooks.ts`: add `useSessionFiles(id, q)` and `useSessionFile(id, path)`, and add them to the
  explicit hook mocks.
- Create `web/src/features/sessions/files/FilesTab.tsx` (lazy) and `FileViewer.tsx`. Reuse D-4's `highlightChunk` and
  line-number styling from `DiffRenderer` where it is shared; extract it if needed, but don't copy it.
- Modify `sessionView.ts` (`files`, plus the `file`/`line` params) and `SessionBody.tsx`. The tab is listed when
  `session.cwd` is set.
- Test: `FilesTab.test.tsx`, `FileViewer.test.tsx`, and an e2e `web/e2e/files-tab.spec.ts` (route-mocked) at 390 and
  1440.

- [ ] **Step 1: Write failing tests**
  - **Search:** the search box queries and lists files; clicking one opens the viewer.
  - **Line jump:** `?file=&line=40` scrolls to and highlights line 40.
  - **Edge files:** a binary file shows `Binary file — not shown`; a truncated file shows the notice.
  - **Errors:** a 400 shows `Can't open this path`.
  - **Tab visibility:** the tab is hidden without a cwd.
  - **E2E:** no overflow.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full web gates, the budget and the e2e. Expected: PASS. Report the budget; don't raise it.
- [ ] **Step 5: Commit** `feat(web): read-only Files tab with search and line jump (D-7)`

### Task 4: @ menu — files and sessions

**Files:**
- Modify:
  - `web/src/features/sessions/composer/MentionMenu.tsx`: grouped items, with a Files group (async) and a Sessions
    group.
  - `web/src/lib/mentionQuery.ts`, only if needed.
  - `Composer.tsx` and `SessionComposer.tsx`: give the session composer a files source (`useSessionFiles`, debounced
    120 ms). The assistant drawer stays sessions-only.
- Test: `MentionMenu.test.tsx` and `Composer.mentions.test.tsx`.

- [ ] **Step 1: Write failing tests**
  - **Grouping:** `@comp` lists Files (from a mocked query) above Sessions.
  - **Insertion:** Enter on a file inserts `@src/Composer.tsx ` at the caret; selecting a session inserts today's
    token.
  - **Not triggered:** the menu doesn't open inside a slash command or a code span.
  - **Debounce:** fast typing issues at most one query per 120 ms.
  - **Assistant:** the assistant composer shows no Files group.
  - **No cwd:** no Files group.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full web gates and the budget. Expected: PASS.
- [ ] **Step 5: Commit** `feat(web): @ mentions for worktree files in the session composer (D-7)`

### Task 5: Clickable file paths in the transcript

**Files:**
- Modify:
  - `web/src/features/sessions/transcript/filePathRef.ts`: add `parseFilePathRef(text) -> {path, line?, col?} | null`.
  - `MarkdownDoc.tsx` / `CopyablePath.tsx`. A path under the session cwd renders as a link-styled button that opens
    `?view=files&file=&line=`, with a small menu offering **Copy path** and **Attach as @**. Other paths keep the
    copy behaviour.
  - Thread `cwd` and the open-file and attach callbacks through the transcript (prefer a context provider over
    prop-drilling, if the transcript already has one).
- Test: `filePathRef.test.ts` and `MarkdownDoc.paths.test.tsx`.

- [ ] **Step 1: Write failing tests**
  - **Parsing:** `src/a.ts:12` gives line 12; `src/a.ts:12:4` gives line and col.
  - **Inside cwd:** an absolute path under cwd opens the Files tab at its relative path; a relative path opens
    directly.
  - **Outside cwd:** `/etc/hosts` is copy-only.
  - **Attach as @:** inserts into the draft.
  - **Archived reader:** the read-only reader has no Files link (copy-only).
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full web gates and the budget. Expected: PASS.
- [ ] **Step 5: Commit** `feat(web): transcript file paths open the Files tab (D-7)`

### Task 6: Benchmark and docs

- Update `scripts/desktop_parity_benchmark.py`:
  - `@-file mentions` → `Y`.
  - `Clickable file paths (open / attach as context)` → `Y`.
  - `Files pane (view / edit)` → `Y`, with the note `read-only by design (D1)`.
- Regenerate audit §3.
- Update the product docs: the session view (Files tab, `@` mentions, clickable paths).
- Mark the spec §10 file rows as shipped.
- Raise the bundle ceilings only if the controller rules it.
- [ ] Run the script. Expected: Target completion ≥ 78%.
- [ ] Commit `docs: D-7 benchmark rerun and Files/@-mention docs`.
