# Sweep: every helper call site honours the setup-token

**Goal:** The nine remaining LLM helper call sites work on accounts whose browser-OAuth refresh is
dead but whose setup-token still launches sessions — the same fix #749 made for suggestions.

**Issue:** #751

## Stacked on #752

This branch is cut from `fix/helper-auth-matches-launch`, not `master`, because it consumes
`oauth_env_for` from #749. **Merge #752 first.** If #752 changes during review, rebase this.

## Why

#749's plan asserted there was "exactly one place building the model-auth env." There are ten. On
`tonystein85_gmail.com` — whose browser-OAuth refresh returns HTTP 400 while its setup-token works —
every one of the other nine silently produces nothing. That is why the daemon logs
`autopilot.decide PERSISTENT FAILURE: N consecutive decider failures`.

Not a regression; it predates #749. But it is the same defect with the same fix, and the project's
working norms make the sweep the point rather than the single instance.

## The shape (verified, all nine identical)

Each site derives a `config_dir` — from an injected `_config_dir` test param, from
`pick_helper_config_dir()`, or from `os.environ` — then builds:

```python
env={**os.environ, "CLAUDE_CONFIG_DIR": config_dir}
```

with no setup-token. **None of them has the account name**, only the config dir. So the fix needs the
inverse mapping, which already exists: `server/sessions/adopt.py:156` `_map_account(config_dir,
known_accounts, account_cfg)`, reverse-mapping by realpath equality.

| file | line |
|---|---|
| `server/sessions/title_generator.py` | 112 |
| `server/sessions/goal_inference.py` | 73 |
| `server/sessions/open_question.py` | 96 |
| `server/sessions/prompt_refiner.py` | 69 |
| `server/automations/autopilot_decider.py` | 126 |
| `server/automations/pr_intent.py` | 88 |
| `server/automations/model_pick.py` | 108 |
| `server/automations/backlog_grouping.py` | 168 |
| `server/automations/nudge_composer.py` | 77 |

## Global constraints

- **One env builder.** After this branch, no call site may build that dict inline. The whole point of
  the sweep is that the eleventh site cannot reintroduce the bug.
- **Never log a token value.**
- Reverse mapping must **fail open**: an unmappable config_dir yields no token rather than raising —
  these are best-effort helpers and must degrade, not crash a tick.
- Behaviour must be unchanged for accounts whose browser-OAuth token is healthy.
- Four CI gates: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`,
  `pytest tests/`, `pytest server/tests/`.

---

### Task 1: One helper env builder, used by all nine

**Files:** the auth module #749 created (grep for `oauth_env_for`); the nine files above.
**Test:** `server/tests/test_helper_env_sweep.py`

**Produces:** `helper_env(config_dir: str) -> dict[str, str]` returning
`{**os.environ, "CLAUDE_CONFIG_DIR": config_dir}` merged with `oauth_env_for(account)`, where
`account` is reverse-mapped from `config_dir`. Returns the base env unchanged when the config_dir
maps to no known account.

Reuse `adopt._map_account` rather than writing a second reverse mapping — if it needs to move to a
shared home to avoid an import cycle, move it and update `adopt.py` to import from the new location.
Do not copy it.

**TDD.** Tests first, each confirmed failing for the right reason:
1. A config_dir that maps to an account with a setup-token → env carries `CLAUDE_CODE_OAUTH_TOKEN`.
2. A config_dir mapping to an account with none → env has no such key (and does not raise).
3. An unmappable config_dir → base env, no raise. (Fail-open.)
4. `CLAUDE_CONFIG_DIR` is always set to the passed value.
5. The token value never appears in any log record. Use `caplog`.

Then convert all nine call sites. **After conversion, grep for `"CLAUDE_CONFIG_DIR":` across
`server/` and assert in your report that the only remaining inline construction is inside the helper
itself** (session-launch env in `sdk_factory.real_sdk_factory` is a separate, legitimate path — say so
explicitly if you leave it, and why).

---

### Task 2: `_default_title_fn` stops being a verbatim copy of the defective gate

**Files:** `server/sessions/orchestrator.py:3834-3842`
**Test:** `server/tests/test_default_title_auth.py`

It is a literal copy of the pre-#749 gate: calls `acquire_valid_token`, returns `None` on `AuthError`,
no setup-token fallback. Give it the same treatment #749 gave `_generate_detached_suggestions`:
proceed when a setup-token exists, log when giving up, never log the token.

**TDD:** a failing test that an account with a dead browser-OAuth refresh but a live setup-token still
generates a title.

---

## Verification

The sweep's value is that it cannot silently regress. In your report, state the grep result proving
no inline env construction remains outside the helper — that claim, not the test count, is the
deliverable.
