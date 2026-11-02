# acceptEdits worktree boundary (#3217) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** In `acceptEdits` mode, auto-approve a file-write tool only when its target resolves inside the session's
worktree and outside `.git`. Every other write falls through to standing rules and then a permission card.

**Architecture:**
- `PermissionRegistry.request` gains a `cwd` keyword. The Claude driver, and the Codex driver for consistency, pass
  their working directory.
- A pure helper `write_target_inside_worktree(input, cwd) -> bool` resolves the target safely and is unit-tested on
  its own.
- The `acceptEdits` branch (`server/sessions/permissions.py` ~291) calls the helper. Nothing else changes: the
  account-memory auto-allow, MCP handling, the `auto`/`allowlist` branches, full-auto and bypass all stay as they are.

**Tech Stack:** Python, pytest.

**Issue:** #3217. **Context:** found during the D-4 security review (`docs/superpowers/plans/2026-11-02-desktop-parity-d4-changes-pane.md`).

## Global Constraints

- **File-write tools:** `_FILE_WRITE_TOOLS` (`Edit`, `Write`, `MultiEdit`, `NotebookEdit`). The target path comes from
  `input["file_path"]`, or `input["notebook_path"]` for NotebookEdit.
- **When a target counts as inside:** `realpath(target)` must equal `realpath(cwd)` or lie below it.
  - For a target that doesn't exist yet, resolve its nearest existing ancestor with `realpath`, then append the
    remaining components. Reject any remaining component that is `..`.
  - A relative target resolves against `cwd`.
  - Symlinks are resolved, so a symlink inside the worktree that points outside counts as outside.
- **`.git` is excluded:** any target whose path below the worktree starts with a `.git` component (a directory, or the
  `.git` file of a linked worktree) is not inside.
- **No cwd:** when `cwd` is None or empty, nothing is auto-approved for `acceptEdits`, so the request falls through.
- **Behaviour outside the boundary:** fall through to the existing standing-rule check, then a card. Never deny
  outright.
- **Unchanged:** the existing account-memory auto-allow (`_is_account_memory_path`) stays before this branch.
- **Logging:** log one INFO line when an `acceptEdits` write is not auto-approved because of the boundary:
  `perm.accept_edits.outside_worktree session=%s tool=%s`. Never log the path's contents, and never at WARNING
  (WARNING files an auto-error issue).
- **Gates:** ruff, mypy, and focused pytest using the scrubbed env.

## Review Focus

1. **A symlink inside the worktree pointing at `~/.bashrc`** must not be auto-approved.
2. **`../` traversal** (`<cwd>/sub/../../etc/x`) must not be auto-approved.
3. **The linked-worktree `.git` file and a clone-root `.git/config`** must not be auto-approved.
4. **A new file in a new subdirectory inside the worktree** (the parent doesn't exist yet) must be auto-approved, as
   it is today.
5. **A Codex session:** behaviour is unchanged, since its file approvals go through `request_external` and its
   sandbox.

---

### Task 1: Boundary helper, registry branch, driver plumbing

**Files:**
- Modify: `server/sessions/permissions.py`:
  - add the helper `write_target_inside_worktree(input: Mapping[str, Any], cwd: str | None) -> bool`;
  - add `cwd: str | None = None` to `request(...)`;
  - in the `acceptEdits` branch, auto-approve only when the helper returns True. Otherwise log INFO and fall through
    to the rule check and card that follow.
- Modify: `server/sessions/claude_driver.py` (~994) and `server/codex/driver.py` (~1616) to pass `cwd=self.cwd`, or
  whatever attribute holds the session's working directory (grep for it).
- Modify: the comment on the `acceptEdits` branch so it states the boundary.
- Test: `server/tests/test_accept_edits_boundary.py`.

- [ ] **Step 1: Write failing tests**
  - **Helper, inside:** Write to `<cwd>/a.py` → True. A new file in a new subdirectory `<cwd>/new/dir/f.py` → True.
    A relative `a.py` → True.
  - **Helper, outside:** `~/.bashrc` → False. `<cwd>/../x` → False. `<cwd>/sub/../../x` → False.
  - **Helper, symlinks:** `<cwd>/link` → `/tmp/elsewhere`, Write `<cwd>/link/f` → False. A symlinked cwd itself, where
    the target is under the realpath → True.
  - **Helper, `.git`:** `<cwd>/.git/config` (directory form) → False. `<cwd>/.git` (file form) → False.
  - **Helper, no cwd:** `cwd=None` → False.
  - **Registry:** an `acceptEdits` request with a Write inside → auto-approved. One outside → pends a card (or matches
    a standing rule when one exists). The account-memory path is still auto-approved even when it is outside cwd.
  - **Registry, unchanged modes:** `full-auto` and `bypassPermissions` are unchanged.
  - **Driver:** a ClaudeAgentDriver test shows `cwd` reaches `request`.
- [ ] **Step 2: Run** the scrubbed-env pytest. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the new test file plus `pytest server/tests -k "permission or accept_edits or rule"`, then ruff
  and mypy. Expected: PASS.
- [ ] **Step 5: Commit** `fix(permissions): acceptEdits auto-approves only writes inside the session worktree (#3217)`

### Task 2: Docs and residual cleanup

- Update `docs/product/features/session-permissions.md`, in the acceptEdits description, to state the boundary.
- In `src/viberpit/worktree_diff.py`'s docstring, the residuals paragraph says #3217 will close them. Change it to say
  the agent write boundary is now enforced for `acceptEdits` (#3217). Shell-capable modes (full-auto, bypass, or an
  approved Bash) still have same-user access by design.
- Commit `docs: acceptEdits worktree boundary (#3217)`.
