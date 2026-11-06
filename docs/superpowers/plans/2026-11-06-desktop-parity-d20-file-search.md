# Desktop-parity D-20: content search in the Files pane — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal.** Let the user search the **contents** of a session's worktree from the Files tab. Each result shows the file, line number and matching line, and clicking it opens the file viewer at that line. The Files pane stays read-only; editing was rejected in D1.

**Why.** It is the last buildable gap in the benchmark: "Files pane (view / edit)" is P with the note "content search not yet". The other rows still left are either deferred by the owner (sandbox, branch prefix) or have no backend (cloud).

**Existing code.**
- The file list and the read-only viewer live in `server/sessions/worktree_files.py`.
- The endpoints are `GET /api/sessions/{id}/files` and `GET /api/sessions/{id}/files/content` (`server/api/sessions.py:~2243-2290`).
- The web side is `web/src/features/sessions/files/FilesTab.tsx`, `FileViewer.tsx` and `useFileTarget` (`?view=files&file=&line=`).

## Global Constraints

- **Runs on the owning daemon.**
  - Add a per-session RPC `file_search(session_id, q, …)`, wired through `_PER_SESSION`, `orchestrator_rpc._SIMPLE`, remote, facade and fleet, the same pattern as the existing `/files` RPC.
  - Implement it in `worktree_files.py` using `git grep`. Reuse that module's existing path-confinement and limits instead of writing new ones.
- **`git grep` invocation.**
  - The argument list is built by code and never passed through a shell: `-n --no-color -I --full-name -F` (fixed-string by default).
  - Pass `-i` when the query has no uppercase (smart case).
  - Pass `--max-count` per file, and `-e <q>` so a query starting with `-` can't be read as a flag.
  - Search the working tree, tracked files only, using the existing confinement rules.
  - Never follow symlinks outside the worktree.
  - Set a timeout (5 s) and an overall cap: 200 matches and 50 files.
  - Each line is truncated to 300 characters, and only the matched region is returned for highlighting.
  - Use the same `GIT_OPTIONAL_LOCKS=0` / read-only environment the Changes pane uses, so the search never touches the index (#3275).
  - An optional `regex=true` switches `-F` to `-E`, with the same caps.
- **API.** `GET /api/sessions/{id}/files/search?q=&regex=&limit=`.
  - Response: `{matches:[{path, line, text, start, end}], truncated, timedOut}`.
  - The minimum query length is 2 characters, and the query is capped at the module's `MAX_QUERY_CHARS`.
  - Error codes: `SEARCH_TIMEOUT` returns 200 with `timedOut` set and partial results; `BAD_REGEX` returns 400; a missing worktree returns 404.
  - Search is read-only, so it needs no person check: identity only, the same as `/files/content`.
  - Expected refusals log at INFO.
  - No audit row: it's a read.
- **Web.**
  - The Files tab gets a search box (debounced at 250 ms) with a "Regex" toggle.
  - Results are grouped by file, with the match highlighted. Clicking a result sets `useFileTarget` to that file and line.
  - Show empty, truncated ("Showing first 200 matches") and timeout states.
  - Keyboard: Enter takes you to the first result; ↑/↓ move through the results.
  - Lazy-load it, since budget headroom is about 0.3 kB.
- **Gates.**
  - Server: ruff, mypy, focused pytest plus parity, then the full `server/tests -n 4` with `PYTHONPATH=$PWD/src` in a clean environment.
  - Web: typecheck, lint, the full vitest suite, build, budget and e2e.

## Review Focus

1. No path outside the worktree is ever read: no symlink escape, no `../`, and no query parsed as a flag (Task 1).
2. The caps and the timeout hold for a huge repo or a pathological regex (Task 1).
3. The search never writes the index or takes locks (Task 1).
4. Clicking a result opens the viewer at the right line, and the match text renders as text, never HTML (Task 2).

---

### Task 1: Server — `file_search` RPC and route

**Files:** `worktree_files.py`, the orchestrator method, RPC, remote, facade and fleet, `api/sessions.py`.

**Tests:** `server/tests/test_file_search.py`, using a real temporary git repo.

- [ ] **Step 1:** Write failing tests covering every bullet above plus Review Focus 1–3.
- [ ] **Step 2–4:** Implement, then run focused tests, parity, ruff and mypy.
- [ ] **Step 5:** Commit `feat(sessions): content search over a session's worktree (D-20)`

### Task 2: Web — search in the Files tab

**Files:** `FilesTab.tsx`, a new lazy `FileSearch.tsx`, hooks and their mocks.

**Tests:** unit tests plus the e2e spec `web/e2e/file-search.spec.ts` (route-mocked, at 1440 and 390).

- [ ] **Step 1:** Write failing tests for Review Focus 4, the states and the keyboard behaviour.
- [ ] **Step 2–4:** Implement, then run the full web gates and e2e.
- [ ] **Step 5:** Commit `feat(web): search file contents from the Files tab (D-20)`

### Task 3: Benchmark and docs

- **Benchmark.** Set `Files pane (view / edit)` to the note "read-only viewer + content search (D-20); edit rejected (D1)". Edit is rejected by the owner, so decide whether the row stays P (edit is half the row) or becomes Y, and explain the call in the note. Regenerate §3.
- **Spec and product docs.**
- **Ceilings:** measured + 0.3, only if they moved.
- [ ] Commit `docs: D-20 benchmark rerun and file-search docs`.
