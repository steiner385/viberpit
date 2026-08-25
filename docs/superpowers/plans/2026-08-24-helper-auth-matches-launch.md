# Helper calls must resolve account auth the same way session launch does

**Goal:** An account that can launch a session must also be able to run helper calls
(suggestions). Today the helper path rejects accounts whose browser-OAuth refresh is dead but whose
setup-token still works — which is the live state of `tonystein85_gmail.com`.

**Issue:** #749

## Measured root cause

- `acquire_valid_token("tonystein85_gmail.com")` raises `AuthError: token refresh failed with 400`.
  Its browser-OAuth refresh token is genuinely rejected.
- The same account holds a setup-token (`sk-ant-oat01`), and session launch uses it via
  `CLAUDE_CODE_OAUTH_TOKEN` (`server/sessions/orchestrator.py:620`). Sessions on this account work.
- `orchestrator._generate_detached_suggestions` gates on `acquire_valid_token` only, catches
  `AuthError`/`FileNotFoundError`, writes `(signature, [])` and returns — **silently**.
- `real_suggestion_factory` / `real_summary_factory` build `env = dict(os.environ,
  CLAUDE_CONFIG_DIR=config_dir)` and never inject `CLAUDE_CODE_OAUTH_TOKEN`, so even bypassing the
  gate would fail inside the SDK.

Confirmed by absence: `SuggestionService.generate`'s failure log has **zero** occurrences in 6h, so
generation is never reached.

## Explicitly NOT in scope

- **Summaries are working as designed.** `should_regenerate` returns False when `not viewing`
  ("When no viewer is present, never regenerate (so an unwatched dashboard spends zero quota)"), and
  most sessions are terminal. Do not change summary triggering.
- The account's dead browser-OAuth refresh token is the owner's housekeeping, not a code fix.

## Global constraints

- **Single source of truth.** There is currently exactly one place that builds the model-auth env:
  `orchestrator.py:620`. Do not write a second one — extract it and have both callers use it.
- Never log a token value. `crypto.read_oauth_token` returns a secret.
- Copilot accounts are out of scope for suggestions (already skipped upstream).
- Four CI gates before pushing: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`,
  `pytest tests/`, `pytest server/tests/`.
- `src/ccswitch/` is imported from the DEPLOY worktree by the shared venv — if any change lands
  there, run with `PYTHONPATH=$PWD/src` and verify with
  `python -c "import ccswitch.accounts as m; print(m.__file__)"`.

---

### Task 1: Extract the model-auth env as a shared helper

**Files:**
- Create/modify: `server/sessions/account_auth.py` (new, small) — or place beside the existing
  helpers if a better home exists; check first and follow the codebase.
- Modify: `server/sessions/orchestrator.py:~618-620` to call it.
- Test: `server/tests/test_account_auth_env.py`

**Produces:** `oauth_env_for(account: str) -> dict[str, str]` returning
`{"CLAUDE_CODE_OAUTH_TOKEN": tok}` when a setup-token exists, else `{}`.

Steps: write the failing test first (a fake `read_oauth_token` returning a token → env contains it;
returning None → empty dict; the value is never logged). Then implement. Then repoint
`orchestrator.py`'s standard-account branch at it, leaving the Copilot branch untouched. Confirm the
existing orchestrator tests still pass.

---

### Task 2: Helper factories inject the setup-token env

**Files:**
- Modify: `server/sessions/sdk_factory.py` — `real_suggestion_factory` (line ~62) and
  `real_summary_factory` (line ~80).
- Test: `server/tests/test_sdk_factory_helper_auth.py`

Both currently build `env = dict(os.environ, CLAUDE_CONFIG_DIR=config_dir)`. They must also merge
`oauth_env_for(account)`. Note the factories receive `config_dir`, not an account name — derive the
account (there is a `compose(rec.account)` → config-dir mapping in the orchestrator; find the
inverse, or thread the account through). **Read how `config_dir` is produced before choosing**; if
threading the account name through the factory signature is cleaner, do that and update callers.

Write the failing test first: a factory built for an account with a setup-token must produce options
whose env carries `CLAUDE_CODE_OAUTH_TOKEN`; without one, it must not.

Fix `real_summary_factory` the same way even though summaries are not currently triggered — leaving
one of two sibling factories wrong is how this class of bug recurs.

---

### Task 3: The suggestion auth gate accepts a setup-token, logs, and stops caching failures

**Files:**
- Modify: `server/sessions/orchestrator.py` — `_generate_detached_suggestions`
- Test: `server/tests/test_detached_suggestion_auth.py`

Three changes, each with a failing test first:

1. **Gate:** when `acquire_valid_token` raises `AuthError`/`FileNotFoundError`, fall back to checking
   `crypto.read_oauth_token(rec.account)`. If a setup-token exists, proceed with generation — the
   account is model-capable. Only give up when neither is available.
2. **Log:** the give-up branch must `log.warning` naming the account and the reason. Its silence is
   what made this take hours to diagnose. Never log the token itself.
3. **Do not cache a failure as an answer.** Today every give-up path writes `(signature, [])`, keyed
   by transcript mtime, so a session stays blank even after the cause is fixed until its transcript
   changes. Distinguish "the model genuinely returned nothing" (cache it) from "the call could not be
   made" (do not cache — let the next poll retry). Keep the in-flight flag cleanup in `finally`
   exactly as it is.

Verify the third point explicitly: a test asserting that after a failed attempt, a second call
re-attempts generation rather than serving a cached empty.
