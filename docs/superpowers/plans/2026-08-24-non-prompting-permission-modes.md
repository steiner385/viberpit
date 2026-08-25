# Non-prompting permission modes for automation sessions

**Goal:** An unattended automation session can proceed without a human approving each edit.
Today no such mode exists in ccswitch at all.

**Issue:** #750

## Root cause (measured)

Both SDK translation points collapse every mode to a prompting value:

```python
server/sessions/sdk_factory.py:36   permission_mode=("plan" if mode == "plan" else "default")
server/sessions/driver.py:989       set_permission_mode("plan" if mode == "plan" else "default")
```

`bypassPermissions` / `acceptEdits` appear nowhere in `server/`, `src/`, or `web/src/`. Separately,
no automation passes `permission_mode`, so `_backlog_spawn` (`orchestrator.py:2173`) inherits
`create_session`'s `"interactive"` default.

## Decisions (approved by the repo owner)

- **`acceptEdits` is the default for automation-spawned sessions.** File edits proceed; other tool
  calls still gate. An unattended agent running arbitrary tools is a materially larger blast radius
  than one editing files in its own worktree.
- **`bypassPermissions` is available but opt-in**, never the default.
- Mode names mirror the SDK's own vocabulary so the two cannot drift.

## Global constraints

- **The mode→SDK mapping must exist in exactly ONE place.** It is currently duplicated verbatim in
  two files; unify before extending, or the two paths will diverge and one will silently fall back
  to `"default"`. This project treats N-parallel-implementations as its most expensive bug shape.
- No schema migration: `Session.permission_mode` (`models.py:99`) and
  `Account.default_permission_mode` (`models.py:329`) are already free-form `String`.
- Unknown/legacy mode values must keep behaving exactly as today (`"interactive"` → `"default"`),
  never crash and never silently escalate privileges.
- Four CI gates before pushing: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`,
  `pytest tests/`, `pytest server/tests/`. Web changes also need `vitest run`, `tsc --noEmit`,
  `eslint` from `web/`.

---

### Task 1: One mapping helper, two call sites

**Files:** create the helper next to the existing session code (grep for the best home — likely
`server/sessions/`); modify `server/sessions/sdk_factory.py:36` and `server/sessions/driver.py:989`.
**Test:** `server/tests/test_permission_mode_mapping.py`

**Produces:** `to_sdk_permission_mode(mode: str) -> str` mapping
`"plan"→"plan"`, `"acceptEdits"→"acceptEdits"`, `"bypassPermissions"→"bypassPermissions"`,
and **everything else (including `"interactive"`, unknown values, empty, None) → `"default"`**.

TDD: write the table-driven failing test first, including the unknown-value case and an explicit
case asserting an unrecognised string does NOT map to a permissive mode. Then implement, then
repoint both call sites. Confirm existing sdk_factory and driver tests still pass — behaviour for
`plan` and `interactive` must be byte-identical to today.

---

### Task 2: Automations spawn with `acceptEdits`

**Files:** `server/sessions/orchestrator.py` (`_backlog_spawn` ~2173, and any sibling automation
spawn — grep `spawn_session(` under `server/` and check PR Doctor too);
`server/automations/context.py` if the default belongs there instead.
**Test:** `server/tests/test_automation_permission_mode.py`

Decide deliberately where the default lives: at each automation's spawn call, or once in
`AutomationContext.spawn_session`. **Prefer the single choke point** if every automation should get
it — but read the call sites first and say why in your report.

TDD: failing test asserting an automation-spawned session record carries `acceptEdits`, and that a
UI-created session still defaults to `interactive` (unchanged). Do not alter the human path.

---

### Task 3: API and UI expose the modes

**Files:** `server/api/sessions.py` (validation of `body.permissionMode`, ~607-627);
`web/src/session/useSessionRuntime.ts` (~269) and the mode control component (grep `onSetMode`).
**Test:** `server/tests/test_sessions_api.py` (or the existing API test file for session creation);
web tests alongside `useSessionRuntime.test.ts`.

Accept the new modes through the API and offer them in the session mode control. Validate the input
— an unknown `permissionMode` from a client must be rejected or coerced to `interactive`, never
passed through to the SDK unchecked.

Run the web gates (`vitest run`, `tsc --noEmit`, `eslint`) from `web/`.

---

## Security review before merge

Task 2 changes what unattended sessions may do without a human. Before finishing, re-read the diff
specifically for: whether `bypassPermissions` can be reached without an explicit opt-in, whether an
unknown mode can escalate rather than degrade, and whether the human-created session path is
genuinely unchanged.
