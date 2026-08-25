# Automations should not spawn sessions that stall on permission prompts

**Goal:** A Backlog Runner (or any automation) session runs unattended without blocking on a human.

**Issue:** #750

## Correcting the record — the first attempt at this was built on a false premise

An earlier plan asserted that "ccswitch cannot express a non-prompting permission mode" and set out
to plumb the SDK's `acceptEdits` / `bypassPermissions` through. **That was wrong**, and the two
commits it produced have been discarded.

ccswitch has its own permission layer and its own user-facing vocabulary:

```python
# server/sessions/permissions.py — PermissionRegistry.request
if mode == "full-auto":
    return None, _resolved(True)     # approve everything, no prompt
```

```typescript
// web/src/session/permissionModes.ts — "Single source of truth — do not duplicate."
interactive  "Ask each time"
auto         "Auto-approve safe tools"
allowlist    "Allowlist read-only, prompt the rest"
full-auto    "Full auto — incl. shell ⚠"
```

The SDK's `permission_mode` is a *lower* layer that effectively just says "ask ccswitch"; ccswitch's
`can_use_tool` → `PermissionRegistry.request` is where the user-facing decision is made. Adding
SDK-native modes was the wrong layer and would have introduced a second permission vocabulary into a
file that explicitly forbids duplicating it.

Worse, it would not have worked: `PermissionRegistry.request` does not branch on `acceptEdits` at
all, so it falls through to a pending prompt with no timeout — the exact symptom being fixed.

## Root cause (the real one)

`AutomationContext.spawn_session` (`server/automations/context.py:132`) passes no `permission_mode`,
so every automation-spawned session inherits `create_session`'s `permission_mode="interactive"`
default and prompts on its first `Edit` or `Bash`.

Separately, **automations never consult the account's `defaultPermissionMode`.** That resolution
exists only on the human path (`server/api/sessions.py:609-616`), so an operator who pinned an
account to `interactive` for safety does not actually constrain automations running on it.

## Why `full-auto` and not `auto`

`DANGEROUS = {"Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch"}`.

`auto` auto-approves only tools **not** on that list, so `Edit` and `Bash` still prompt — useless for
a Backlog Runner, which needs `git`, `gh` and `pytest`. `full-auto` is the only existing mode that
lets an unattended run complete. There is no middle tier, and this plan does not invent one.

**The repo owner explicitly approved this**, on an accurate framing: an unattended agent will run
arbitrary shell with no human in the loop.

## Global constraints

- **Do not add a new permission mode or vocabulary.** Use ccswitch's existing values.
- **The human path must be completely unchanged.** It already resolves account defaults and falls
  back to `interactive`; none of that may shift.
- An account's `defaultPermissionMode`, when set, must win over the automation fallback — an operator
  pinning an account is expressing intent that automations should honour.
- An automation passing an explicit `permission_mode` must win over everything.
- Four CI gates before pushing: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`,
  `pytest tests/`, `pytest server/tests/`.

---

### Task 1: Automations resolve permission mode like humans do, falling back to `full-auto`

**Files:**
- Modify: `server/automations/context.py` (`spawn_session`, ~line 132)
- Test: `server/tests/test_automation_permission_mode.py` (create)

**Resolution order**, highest first:
1. an explicit `permission_mode` in `create_kwargs`
2. the account's `defaultPermissionMode` from the ledger, when the spawn pins an account
3. `"full-auto"`

The account-default lookup already exists on the human path
(`ccswitch.ledger.get_account_defaults` under `locked_ledger()`, `server/api/sessions.py:609-616`).
**Read that code and reuse the same call — do not write a second resolution.** If the two are similar
enough to share a helper, extract one and use it from both; say what you chose and why.

Note the human path only consults account defaults when an account is explicitly pinned. Automations
resolve an account before spawning (`_backlog_spawn` passes `account=account`), so the lookup applies
— but handle the unpinned case without raising.

**TDD — a failing test for each, run first and confirmed failing for the right reason:**
1. An automation spawn with no account default and no explicit mode → session record carries `full-auto`.
2. An account with `defaultPermissionMode="interactive"` → the automation session is `interactive`, NOT `full-auto`. (Operator intent wins. This is the property that keeps this from being a blanket escalation.)
3. An explicit `permission_mode` in the spawn kwargs wins over both.
4. A human/UI-created session is unchanged — still `interactive` when nothing is set. Assert against the real `create_session` path, not a double that could have drifted.

Then implement, then confirm the tests pass, then run every existing test touching automations,
session creation, or permission modes.

**Commit** referencing #750, stating plainly that automation sessions now default to full-auto.

---

## Security review before merge

This grants unattended agents arbitrary shell. Before finishing, re-read the diff for: whether the
human path can pick up the new fallback; whether an account pinned to a restrictive mode is genuinely
honoured; and whether any path reaches `full-auto` other than the documented fallback.

## Out of scope

- The `"plan" if mode == "plan" else "default"` duplication between `sdk_factory.py:36` and
  `driver.py:989` is real but unrelated to this fix. Track it separately rather than widening this.
- No API or UI change is needed: `full-auto` is already offered in both.
