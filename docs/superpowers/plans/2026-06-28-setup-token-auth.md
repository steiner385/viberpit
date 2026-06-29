# Setup-Token Auth (hard replace) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Authenticate every ccswitch session via a per-account long-lived `claude setup-token` (`sk-ant-oat01-…`, 1-year, non-rotating) stored in the GNOME keyring and injected as `CLAUDE_CODE_OAUTH_TOKEN`, hard-replacing the OAuth-grant-harvest + refresh path.

**Architecture:** Three phases. **Phase A** (foundational, detailed below) adds keyring storage for per-account tokens and injects the token into each session's SDK subprocess env — additive, so sessions can auth via token while the old path still exists. **Phase B** (generation) wires the interactive `setup-token` flow into the dashboard. **Phase C** (removal + pre-cutover gate) verifies every account has a working token, then deletes the grant-harvest/refresh machinery. B and C are structured here and detailed once their open verifications (revocation path, quota) resolve.

**Tech Stack:** Python (`src/ccswitch/`, `server/`), `secret-tool`/GNOME keyring, the `claude` binary (2.1.195) + `claude-agent-sdk`, pytest.

**Spec:** `docs/superpowers/specs/2026-06-28-headless-reauth-design.md` (see "REVISION: setup-token architecture").
**Spike evidence:** `docs/superpowers/specs/spikes/2026-spike-setup-token.md`.

## Global Constraints

- Token type is the opaque bearer `sk-ant-oat01-…`; consumed via the env var **`CLAUDE_CODE_OAUTH_TOKEN`** (exact name).
- Secrets route through the EXISTING `src/ccswitch/crypto.py` `secret-tool` pattern (service `ccswitch`); per-account username is **`oat-<account>`** (exact format). No new secrets subsystem, no plaintext at rest.
- Token value is NEVER logged or printed (extends the existing redaction discipline).
- Per-process isolation preserved: `CLAUDE_CONFIG_DIR` stays per session; the token rides `extra_env`.
- Hard replace: the grant-harvest/refresh path is removed in Phase C — but ONLY behind the Phase C pre-cutover gate that proves every active account authenticates via its token first.
- Backend CI gates run first: `ruff` + `mypy` + `pytest server/tests/`. Worktree tests run against the prebuilt venv `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python` with `PYTHONPATH=$PWD/src`.
- Completion workflow: branch off `origin/master`, one PR per phase, ready + auto-merge.

---

## PHASE A — Token storage + injection (foundational, executable now)

Additive: lets a session authenticate via a stored `oat-<account>` token without removing
anything. Safe to ship and deploy before generation/removal exist (a token can be placed in the
keyring manually with `secret-tool` for testing).

### Task A1: Per-account token storage in the keyring

**Files:**
- Modify: `src/ccswitch/crypto.py` (add two functions near `store_key_in_keyring`, ~line 156)
- Test: `tests/test_crypto.py`

**Interfaces:**
- Consumes: nothing new (uses `subprocess` already imported in crypto.py).
- Produces:
  - `store_oauth_token(account: str, token: str) -> None` — `secret-tool store service ccswitch username oat-<account>`.
  - `read_oauth_token(account: str) -> str | None` — `secret-tool lookup …`; returns None on non-zero exit / missing.

- [ ] **Step 1: Write the failing test** (in `tests/test_crypto.py`)

```python
def test_store_and_read_oauth_token_roundtrip(self):
    from ccswitch import crypto
    calls = []
    def fake_run(argv, **kw):
        calls.append((argv, kw.get("input")))
        class R: returncode = 0; stdout = "sk-ant-oat01-XYZ\n"; stderr = ""
        return R()
    with mock.patch("ccswitch.crypto.subprocess.run", side_effect=fake_run):
        crypto.store_oauth_token("primary", "sk-ant-oat01-XYZ")
        val = crypto.read_oauth_token("primary")
    # store used the oat-<account> username and piped the token on stdin
    store_argv, store_input = calls[0]
    assert "oat-primary" in store_argv and store_input == "sk-ant-oat01-XYZ"
    assert val == "sk-ant-oat01-XYZ"

def test_read_oauth_token_missing_returns_none(self):
    from ccswitch import crypto
    class R: returncode = 1; stdout = ""; stderr = ""
    with mock.patch("ccswitch.crypto.subprocess.run", return_value=R()):
        assert crypto.read_oauth_token("ghost") is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest tests/test_crypto.py -k oauth_token -v`
Expected: FAIL — `AttributeError: module 'ccswitch.crypto' has no attribute 'store_oauth_token'`.

- [ ] **Step 3: Implement** (in `src/ccswitch/crypto.py`)

```python
def store_oauth_token(account: str, token: str) -> None:
    """Store a long-lived setup-token (sk-ant-oat01-…) in the GNOME keyring."""
    proc = subprocess.run(
        ["secret-tool", "store", "--label", f"ccswitch oauth token {account}",
         "service", "ccswitch", "username", f"oat-{account}"],
        input=token, text=True, capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"secret-tool store failed (exit {proc.returncode}): {proc.stderr.strip()}")


def read_oauth_token(account: str) -> str | None:
    """Return the stored setup-token for *account*, or None if absent."""
    proc = subprocess.run(
        ["secret-tool", "lookup", "service", "ccswitch", "username", f"oat-{account}"],
        text=True, capture_output=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None
```

- [ ] **Step 4: Run to verify it passes**

Run: `PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest tests/test_crypto.py -k oauth_token -v`
Expected: PASS (both tests).

- [ ] **Step 5: Lint + commit**

```bash
/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/ruff check src/ccswitch/crypto.py
/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/mypy src/ccswitch/crypto.py
git add src/ccswitch/crypto.py tests/test_crypto.py
git commit -m "feat(crypto): per-account setup-token storage in the keyring (oat-<account>)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

### Task A2: Inject the token into the session SDK subprocess env

**Files:**
- Modify: `server/sessions/orchestrator.py` (`_session_factory._factory`, ~lines 294-301; add a helper `_oat_env`)
- Test: `server/tests/test_orchestrator_token_refresh.py` (has the `_orch()` helper + ccswitch_accounts mock pattern)

**Interfaces:**
- Consumes: `crypto.read_oauth_token(account) -> str | None` (Task A1); `self._records[session_id].account`.
- Produces: when a token exists, the factory's `extra_env` includes `{"CLAUDE_CODE_OAUTH_TOKEN": <token>}`, merged with any X11 env. When absent, behavior is unchanged (empty/X11-only extra_env).

- [ ] **Step 1: Write the failing test** (in `server/tests/test_orchestrator_token_refresh.py`)

```python
def test_session_factory_injects_oauth_token_env(monkeypatch):
    from unittest import mock
    from server.sessions.orchestrator import SessionOrchestrator
    from server.sessions.types import SessionRecord
    captured = {}
    def base(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, on_pre_compact=None):
        captured["extra_env"] = extra_env
        return object()
    orch = SessionOrchestrator(sdk_factory=base, poll_interval=9999.0, usage_fn=lambda n: None)
    orch._x11 = None
    orch._records["s1"] = SessionRecord(id="s1", name="n", account="primary",
                                        claude_session_id=None, cols=80, rows=24, cwd="/tmp/x")
    monkeypatch.setattr("server.sessions.orchestrator.crypto.read_oauth_token",
                        lambda acct: "sk-ant-oat01-TOK" if acct == "primary" else None)
    factory = orch._session_factory("s1")
    factory("/tmp/x", "/cfg", None, "default", None)
    assert captured["extra_env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-TOK"
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_orchestrator_token_refresh.py::test_session_factory_injects_oauth_token_env -v`
Expected: FAIL — `KeyError: 'CLAUDE_CODE_OAUTH_TOKEN'` (token not injected).

- [ ] **Step 3: Implement** (in `server/sessions/orchestrator.py`)

Add the crypto import near the top (`from ccswitch import crypto`) if not present, and a helper + injection:

```python
    def _oat_env(self, session_id: str) -> dict:
        """Return {CLAUDE_CODE_OAUTH_TOKEN: <token>} for the session's account, or {}."""
        rec = self._records.get(session_id)
        if rec is None:
            return {}
        tok = crypto.read_oauth_token(rec.account)
        return {"CLAUDE_CODE_OAUTH_TOKEN": tok} if tok else {}
```

Then in `_session_factory._factory`, merge it into `extra`:

```python
        def _factory(cwd, config_dir, can_use_tool, mode, resume, *, on_pre_compact=None):
            extra = {**(x11.env_for(session_id) if x11 else {}), **self._oat_env(session_id)}
            if extra:
                return base(cwd, config_dir, can_use_tool, mode, resume,
                            extra_env=extra, on_pre_compact=on_pre_compact)
            return base(cwd, config_dir, can_use_tool, mode, resume, on_pre_compact=on_pre_compact)
```

- [ ] **Step 4: Run to verify it passes**

Run: `PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_orchestrator_token_refresh.py -v`
Expected: PASS (new test + existing tests unaffected — `crypto.read_oauth_token` returns None without a real keyring, so other tests see empty oat env).

- [ ] **Step 5: Lint + commit**

```bash
/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/ruff check server/sessions/orchestrator.py
/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/mypy server/sessions/orchestrator.py
git add server/sessions/orchestrator.py server/tests/test_orchestrator_token_refresh.py
git commit -m "feat(auth): inject per-account setup-token as CLAUDE_CODE_OAUTH_TOKEN per session

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

### Task A3: Live end-to-end verification (manual, documented)

- [ ] Generate a token for one spare account via `claude setup-token`; store it:
  `secret-tool store service ccswitch username oat-<account>` (paste token).
- [ ] Start a session on that account in the running app; confirm it authenticates and runs a turn.
- [ ] Confirm via `/proc/<session-pid>/environ` (or a debug log that REDACTS the value) that
  `CLAUDE_CODE_OAUTH_TOKEN` is present in the session subprocess env.
- [ ] Record the result in `docs/superpowers/specs/spikes/2026-spike-setup-token.md`.

### Task A4: Open the Phase A PR

- [ ] Branch `feat/setup-token-auth-phase-a` off `origin/master`, cherry-pick A1+A2 commits, push,
  `gh pr create` (body references the spec + spike), `gh pr merge <#> --squash --auto`.

---

## PHASE A.2 — Daemon-readable encrypted token storage (CORRECTION, do before step-1 verify)

A3 live verification found the systemd daemon has **no keyring** (uses `CCSWITCH_MASTER_KEY` from
`/etc/ccswitch/ccswitch.env`; `secret-tool` fails with no D-Bus session). So Phase A's keyring
`read_oauth_token` is inert in prod. Rework storage to envelope-encryption the daemon can read.

### Task A2.1: encrypted-file token store (replaces keyring as primary)

**Files:** Modify `src/ccswitch/crypto.py` (or a new `src/ccswitch/oat_store.py`); Test `tests/test_crypto.py`.

**Interfaces (corrected):**
- `store_oauth_token(account, token)`: if `key_available()`, write `crypto.encrypt(token)` to
  `paths.store_dir()/oat/<account>` (mode 0600). Falls back to keyring only if no master key.
- `read_oauth_token(account) -> str | None`: precedence mirroring `master_key()` —
  1) env override `CCSWITCH_OAT_<ACCOUNT_SANITIZED>`, 2) the encrypted file
  (`crypto.decrypt`, guarded — return None on missing/decrypt-fail), 3) keyring `oat-<account>`
  (interactive fallback, with the existing timeout+guard).

- [ ] Step 1: write tests — roundtrip via encrypted file (mock `key_available`/`encrypt`/`decrypt`),
  env-override wins, missing→None, decrypt-failure→None. (Mirror `tests/test_crypto.py` style.)
- [ ] Step 2: run, confirm FAIL.
- [ ] Step 3: implement the precedence + encrypted-file write/read (reuse `crypto.encrypt/decrypt/is_encrypted`, `paths.store_dir()`).
- [ ] Step 4: run, confirm PASS + full `tests/test_crypto.py` green + ruff/mypy.
- [ ] Step 5: commit.

### Task A2.2: live A3 verification (now meaningful)
- [ ] Generate a `setup-token` for one account; `store_oauth_token` writes the encrypted file.
- [ ] Confirm a daemon session on that account authenticates via the token and
  `CLAUDE_CODE_OAUTH_TOKEN` is in the subprocess env (`/proc/<pid>/environ`, redacted).
- [ ] Confirm the daemon (no keyring) reads it — proving the correction.

---

## PHASE B — Interactive token generation in the dashboard (UNBLOCKED)

> Gates resolved: **revocation** (no per-token revoke; rotation=regen — recorded in spec/spike) and
> **quota/ToS** (RESOLVED: setup-token draws on the subscription's own quota, scoped inference-only,
> Anthropic's sanctioned headless mechanism; Spike D's 6-concurrent confirmed normal metering).

**Design (reuse, not parallel):** extend the existing `ReauthManager` (`server/reauth.py`) — it
already owns the global single-flight lock + start/status/cancel + state machine. Add a
setup-token runner mode, an `awaiting_code` state, an `auth_url` field, and a `submit_code` method.
Storage uses the encrypted-file `crypto.store_oauth_token` from Phase A.2. The code-paste flow was
proven live in the A3 spike driver.

### Task B1 — `capture_setup_token` core (this PR)
**Files:** Modify `src/ccswitch/auth_login.py`; Test `server/tests/test_capture_setup_token.py` +
a fake setup-token binary `server/tests/fakes/fake_claude_setup_token.py` (mirrors
`fakes/fake_claude_login.py`).
- `capture_setup_token(name, *, on_url, get_code, claude_bin=None, env_extra=None) -> bool`:
  pty-drives `claude setup-token`; when the `https://claude.com/cai/oauth/authorize…` URL appears,
  calls `on_url(url)`; calls `get_code()` (blocking, returns the user's pasted code) and injects it;
  on success extracts the `sk-ant-oat01-…` token and stores it via `crypto.store_oauth_token(name, token)`
  (encrypted file). Returns True/False. The token value is NEVER logged or returned.
- Fake binary: prints a fake authorize URL, reads a line (the code) from stdin, prints
  `sk-ant-oat01-FAKE…`, exits 0. Test drives `capture_setup_token` with `claude_bin=[python, fake]`,
  asserts on_url got a URL, get_code was called, and `read_oauth_token(name)` returns the fake token.

### Tasks B2–B5 (subsequent PRs)
2. **B2** — `ReauthManager` extension: `awaiting_code` state + `auth_url` in `ReauthState.to_dict`,
   `submit_code(name, code)`, and a setup-token runner wiring `capture_setup_token` (on_url →
   state.auth_url; get_code → blocks on an Event set by submit_code).
3. **B3** — API: `POST /{name}/reauth/code` (and `/add/code`) → `manager.submit_code`; include
   `authUrl` in the reauth/add status response.
4. **B4** — frontend: in the add/reauth dialog, show `authUrl` + a code-paste input when state is
   `awaiting_code`; POST the code.
5. **B5** — needs-reauth on token 401: classify a setup-token 401 → `account.needs_reauth` (reuse
   the existing ledger + audit path) so the dashboard prompts regeneration (~yearly).

---

## PHASE C — Remove grant-harvest/refresh + pre-cutover gate (structured; gated on A+B in prod)

> Hard-replace removal. MUST run the pre-cutover gate first.

### Pre-cutover gate (mandatory, build first in Phase C)
- A command/probe that, for EVERY active account, confirms a working `oat-<account>` token via a
  live auth probe (e.g. a trivial `CLAUDE_CODE_OAUTH_TOKEN=… claude -p` returning 0). Abort
  removal if any account fails. `log()` exactly which accounts pass/fail — no silent cutover.

### Removal (only after the gate is green in prod)
- Delete: `oauth.refresh_access_token` + the refresh/rotation logic, `accounts.ensure_valid_token` /
  `acquire_valid_token`, the `_token_refresh_monitor` / `_refresh_idle_tokens_once` loop, the
  in-session-401 recompose-**refresh** added in #340 (the recompose itself may stay or go), the
  Cloudflare-1010 UA / idempotent-POST handling, and grant-harvest in `capture_login`.
- Update the account model so `add_account` stores only what the token model needs (no
  `refreshToken`); migrate the slot schema.
- Each deletion in the same PR as its replacement reaching prod — no half-dead code.

---

## Self-Review

**Spec coverage:** storage (A1) ✓; injection (A2) ✓; env-var name + keyring `oat-<account>` format
(Global Constraints + A1) ✓; generation flow (Phase B) ✓; hard-replace removal + pre-cutover gate
(Phase C, with the spec's mandatory safeguard) ✓; revocation/quota open items flagged as Phase B
gates ✓. Phase 1 (#340) removal is explicitly listed in Phase C.

**Placeholder scan:** Phase A is complete TDD with real code/commands. Phases B/C are deliberately
structured (file map + task list + interfaces) and GATED on named verifications — an explicit
decomposition decision, not hidden TODOs; their steps are written once the gates resolve.

**Type consistency:** `store_oauth_token(account, token)->None`, `read_oauth_token(account)->str|None`,
`_oat_env(session_id)->dict`, env var `CLAUDE_CODE_OAUTH_TOKEN`, username `oat-<account>` — used
consistently across tasks.

**Recommendation:** Ship Phase A (additive, safe), generate tokens for all accounts (Phase B),
prove in prod, then run Phase C's gate + removal. Do NOT delete the old path until the gate is green.
