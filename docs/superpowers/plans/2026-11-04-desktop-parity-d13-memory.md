# Desktop-parity D-13: memory files — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:**
- The session Context card lists every memory file the CLI actually loaded (user, project, local, rules, imports), and each one can be viewed.
- Settings › Claude config › Memory edits the user-level `CLAUDE.md`, and that one file is what every account and host loads.
- Repo memory files are read-only in the UI.

**Architecture:** The verified facts are in `docs/superpowers/specs/2026-11-04-d13-memory-verification.md` (CLI 2.1.318). The design follows from them:
- **The user file is `$CLAUDE_CONFIG_DIR/CLAUDE.md`, and no account has one today.** That is a live bug. Global instructions are picked up only by accident, through the walk up from HOME, and labelled as *project* files. Repos outside HOME never get them.
- **Fix it by sharing `CLAUDE.md`** through `SHARE_ALLOWLIST`, which symlinks it to `~/.claude/CLAUDE.md`. Rocky sees the same file over sshfs, so no sync code is needed.
- **The loaded set comes from the public `get_context_usage` control request** (`memoryFiles: [{path, type, tokens}]`). The driver already calls it after every turn but drops that field. Carry it on the existing `contextUsage` event, and persist it so it replays on attach.
- **Viewing a file outside the worktree** goes through a new owning-host read path. It only allows paths that are in that session's last reported `memoryFiles`.

**Tech Stack:** Python, FastAPI, pytest; React + TS, vitest, Playwright.

**Spec:** `docs/superpowers/specs/2026-11-01-ui-desktop-parity-design.md` §2 (Context card), §8 (Memory files), §10 (memory-file discovery), §11 D-13. Decision D8.

## Global Constraints

- **Engines.** Claude only. Codex AGENTS.md discovery is out of scope; the card hides the memory list when the engine reports none.
- **Shared user file.**
  - Add `"CLAUDE.md"` to `SHARE_ALLOWLIST` (`src/viberpit/configdir.py`).
  - **Never delete a private file.** If an account dir already has a real `CLAUDE.md`, keep it, don't link, and log INFO naming the account. This is the same rule as D-9's file-history collision. The current `reconcile_links` deletes a real file that shadows a shared entry (`:182-186`); that must not happen for `CLAUDE.md`.
  - **Missing shared file.** If `~/.claude/CLAUDE.md` doesn't exist, don't create an empty one. Create the symlink anyway: a dangling link loads nothing, which matches today. Confirm the CLI treats a dangling symlink as absent; the spike suggests it does. If it doesn't, create the link only when the target exists.
  - **Fix the wrong comments** at `scratch_config.py:256`, `assistant/prompt.py:18` and `claude_sdk_factory.py:44-46`.
  - **Helper and assistant sessions.** Check whether the stateless helpers or the assistant use `--setting-sources` / `setting_sources=[]`. If they deliberately exclude user memory, keep that. The share must not change which processes load user memory, apart from fixing the coding sessions.
- **Loaded set.**
  - `_emit_context_usage` (`claude_driver.py:~2303`) carries `memoryFiles: [{path, type, tokens}]` on the `contextUsage` event, in CLI order.
  - The record keeps the last set, and attach/replay re-emits it, as context usage already does. Find the existing persistence; if usage is in-memory only, keep the set in memory too and re-emit it on attach.
  - `type` keeps the CLI's values. The web maps them to labels: **User**, **Project**, **Local**, **Rule**, **Import**, **Managed**. Unknown values show as-is.
  - Paths are absolute, as the CLI reports them. The web shows them worktree-relative when inside the worktree, `~/…` when under HOME, and absolute otherwise.
- **Viewing.**
  - `GET /api/sessions/{id}/memory-file?path=` runs on the owning host (per-session RPC, fleet `_PER_SESSION`, facade, remote; the same pattern as D-11 `transcript_search`).
  - It allows only a path exactly equal to one in the session's current `memoryFiles`. Anything else gets 404 `MEMORY_FILE_UNKNOWN`.
  - It opens with `O_NOFOLLOW` on the final component through `src/viberpit/fs_beneath.py`, where it applies. The user file is a symlink by design, so resolve the allowed path once: realpath it, then require that it is either inside the worktree or one of `{shared ~/.claude/CLAUDE.md, $CLAUDE_CONFIG_DIR/CLAUDE.md}`, before opening it.
  - Size cap 256 kB; above it, `truncated: true`. The response is `{path, type, text, truncated, editable}`.
  - `editable` is true only for the user file.
- **Editing the user file.**
  - Routes: `GET /api/settings/memory` returns `{text, etag, exists}`, and `PUT /api/settings/memory` takes `{text, etag}` and returns `{etag}`.
  - It reads and writes `~/.claude/CLAUDE.md` on the edge host, which owns `~/.claude`. Rocky sees it over sshfs.
  - **Atomic write:** write a temp file in the same directory, fsync it, then rename it over the target. Mode 0644. The target must not be a symlink; if it is, refuse with 409 `MEMORY_FILE_IS_LINK`.
  - **Concurrency:** the etag is sha256(content). A mismatch gets 409 `MEMORY_CONFLICT` with the current text. A missing file has etag `""`.
  - Size limit 256 kB, else 413.
  - Auth: the normal identity. This is a settings write, so follow how other settings PUTs are gated (grep `settings` routes for the operator/owner checks). Write an audit row.
  - **Copy, verbatim:** `Applies to new sessions. Running sessions keep the instructions they started with.`
- **UI.**
  - **Context card** (D-6 `rail/ContextCard.tsx`): a "Memory" list of the loaded files, with label, display path and tokens. Clicking a file opens a read-only viewer (a dialog using the D-7 file viewer component). The user file shows an **Edit in Settings** link.
  - **Settings › Claude config › Memory:** a section in `settingsMeta.ts` / `settingsDefs`, holding a textarea editor with Save and the copy above. On a conflict it shows the current text and a "Reload" action. With no file yet, it shows an empty editor and Save creates the file.
- **Gates.**
  - Server: ruff, mypy, focused pytest plus the RPC parity tests.
  - Core: `tests/` with `PYTHONPATH=$PWD/src` for `configdir`.
  - Web: typecheck, lint, full vitest, budget (ceilings 107.6 / 301.7; the editor and viewer are lazy), e2e.

## Review Focus

1. **A worktree outside HOME** (for example `/mnt/ssk-ssd/...`): after the share, the user file loads as **User**. Pinned in Task 1, by a no-model probe test against the binary, like the D-9 contract test.
2. **Path traversal or symlink tricks on the viewer:** `?path=` naming a file not in the set, or a set entry that is a symlink to `/etc/shadow`, is refused. Pinned in Task 2.
3. **Concurrent edits from two tabs:** the second save gets `MEMORY_CONFLICT` and nothing is lost. Pinned in Task 2.
4. **An account with an existing private `CLAUDE.md`:** it is kept, not linked, and reported. Pinned in Task 1.
5. **A remote-host session:** the memory list and viewer come from the owner daemon. Pinned in Task 2.

---

### Task 1: Share the user CLAUDE.md across accounts

**Files:**
- Modify: `src/viberpit/configdir.py`, plus the three wrong comments.
- Test: `tests/test_configdir_isolation.py`, run with `PYTHONPATH=$PWD/src`, and `server/tests/test_memory_cli_contract.py`.
  - The contract test is no-model and marked `slow`. It skips when the binary is absent.
  - It sets a throwaway `CLAUDE_CONFIG_DIR` whose `CLAUDE.md` is a symlink to a temp "shared" file, and a cwd outside HOME. It uses `get_context_usage` to assert the user file is reported with type user, and that a dangling link reports nothing.

- [ ] **Step 1: Write failing tests.**
  - The share links `CLAUDE.md`.
  - An existing private `CLAUDE.md` is kept, with an INFO log.
  - A dangling link is handled.
  - Credentials are still never linked.
  - The contract test passes.
  - Helper processes that exclude user memory still do.
- [ ] **Step 2–4.** Run core `tests/` and the contract test, then ruff and mypy.
- [ ] **Step 5: Commit** `fix(configdir): share the user CLAUDE.md so every account loads it as user memory (D-13)`

### Task 2: Server — loaded set, viewer, user-file editor

**Files:**
- `claude_driver.py`: `_emit_context_usage` carries `memoryFiles`.
- `server/sessions/events.py` and the web `events.ts`.
- The record and replay.
- The orchestrator `memory_file(session_id, path)`, plus the facade, remote, RPC and fleet wiring.
- `server/api/sessions.py`: the viewer route.
- New `server/api/memory.py`: the settings GET/PUT, with an audit row. Register it in the app.
- Test: `server/tests/test_memory_files.py`.

- [ ] **Step 1: Write failing tests.**
  - The `contextUsage` event carries `memoryFiles` in order, and it is re-emitted on attach.
  - **Viewer:**
    - allows a set path inside the worktree and the user file;
    - refuses a non-set path, a traversal, and a set entry that resolves outside the allowed roots;
    - applies the size cap;
    - `editable` is true only for the user file.
  - **Remote:** a UDS round trip reaches the owner. The parity membership is right, and `daemon_outdated` is returned for an old daemon.
  - **Settings GET/PUT:**
    - a missing file reads as `exists: false` and etag `""`;
    - the write is atomic, which a crash-mid-write simulation shows leaves the old file intact;
    - an etag mismatch gets 409 with the current text;
    - a symlink target is refused;
    - over 256 kB gets 413;
    - an audit row is written;
    - nothing is logged at WARNING.
- [ ] **Step 2–4.** Run the focused tests, the RPC parity tests, ruff and mypy.
- [ ] **Step 5: Commit** `feat(sessions): loaded memory files per session, a viewer and a user-memory editor (D-13)`

### Task 3: Web — Context card memory list and Settings › Memory

**Files:**
- `rail/ContextCard.tsx`: the memory list.
- A lazy `MemoryFileDialog`.
- `api/hooks.ts`: `useMemoryFile`, `useUserMemory`, `useSaveUserMemory`. Update the hook mocks.
- The reducer or runtime: keep `memoryFiles` from the `contextUsage` event.
- `settingsMeta.ts` / `settingsDefs.ts`: the Memory section.
- A lazy `MemorySection.tsx`.
- Tests, plus the e2e `web/e2e/memory.spec.ts`, route-mocked.

- [ ] **Step 1: Write failing tests.**
  - The card lists the files with labels, display paths and tokens, in CLI order, and hides the list when it is empty.
  - The dialog opens read-only. The user file shows "Edit in Settings".
  - The editor loads, saves with the etag, and shows the "Applies to new sessions…" copy.
  - On a conflict it shows the current text and Reload.
  - A missing file shows an empty editor and Save creates it.
  - The e2e covers the card, the dialog, and an edit, at 1440 and 390.
- [ ] **Step 2–4.** Run the full web gates, budget, and e2e.
- [ ] **Step 5: Commit** `feat(web): memory files in the Context card and a Settings editor for user memory (D-13)`

### Task 4: Benchmark and docs

- Benchmark: update the CLAUDE.md / memory row. Regenerate audit §3 and report the target (D-12: 88%).
- Spec §2, §8 and §10: record the shared-file design, the bug it fixes, `get_context_usage` as the source, the viewer's allow rule, and the editor's etag and atomic write.
- Product docs: a config-dir isolation note that CLAUDE.md is now shared, and a web-dashboard section.
- Ceilings: measured + 0.3, by controller ruling.
- [ ] Commit `docs: D-13 benchmark rerun and memory-file docs`.
