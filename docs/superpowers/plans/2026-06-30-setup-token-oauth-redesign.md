# Setup-token in-process OAuth2 PKCE — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate per-account long-lived setup-tokens (`sk-ant-oat01-…`) by performing the OAuth2 PKCE flow in-process, replacing the broken headless `claude setup-token` PTY-scraping path, while keeping the existing mobile code-paste UX.

**Architecture:** A new pure module `setup_token_oauth.py` builds the PKCE authorize URL and performs the token exchange via `httpx`. `server/reauth.py` calls `begin()` on `start(mode=setup_token)` (→ `awaiting_code` with the URL) and `exchange()` on `submit_code` (→ store token + `mark_reauthenticated`). The PTY-scraping path is deleted. Frontend, API, and the reauth facade/state-machine are unchanged.

**Tech Stack:** Python 3.12, `httpx` (already a dep), `hashlib`/`secrets`/`base64` (stdlib), pytest.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-30-setup-token-oauth-redesign-design.md` (authoritative).
- OAuth constants (verified present in the bundled binary; Task 1 byte-verifies the exchange body):
  - `AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"`
  - `TOKEN_URL = "https://platform.claude.com/v1/oauth/token"`
  - `CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"`
  - `REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"`
  - `SCOPE = "user:inference"`, PKCE method `S256`, pasted code format `<code>#<state>`.
- Never log or print the token value or the pasted code (existing redaction filter scrubs
  `sk-ant-oat01-…`; also never embed that literal in a log message — see memory
  `redaction-filter-scrubs-log-literals`).
- Store via existing `crypto.store_oauth_token(name, token)`; require `key_available()`.
- Use a real `User-Agent` header on the token POST (memory `cloudflare-1010-refresh-403-mass-reauth`).
- Run on branch `feat/setup-token-oauth-redesign` (already created off `origin/master`).
- Test env: `PYTHONPATH=src`, venv at `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv`;
  set `CCSWITCH_MASTER_KEY` for crypto tests (see existing `test_capture_setup_token.py` for the
  deterministic test key pattern). Lint gates: `ruff check` + `mypy` (run both before each commit).

---

### Task 1: Byte-verify the real token-exchange request — ✅ DONE (2026-06-30)

Completed before execution. Captured the real `claude setup-token` exchange via mitmproxy AND
validated our own implementation end-to-end against the live endpoint (hosted redirect + pasted
code → HTTP 200 + `sk-ant-oat01` token; stored for `tony_kindash.com`). The verified request/response
shape is recorded in the spec under "## Verified exchange request" and already baked into Task 3
below (JSON body, `expires_in: 31536000`, hosted `redirect_uri`, `User-Agent: axios/1.15.2`,
`access_token` response field). No subagent action required for this task.

---

### Task 2: `setup_token_oauth.begin()` — PKCE challenge + authorize URL

**Files:**
- Create: `src/ccswitch/setup_token_oauth.py`
- Test: `server/tests/test_setup_token_oauth.py`

**Interfaces:**
- Produces: `begin() -> SetupTokenChallenge` where
  `SetupTokenChallenge = dataclass(authorize_url: str, verifier: str, state: str)`.
  Module constants `AUTHORIZE_URL, TOKEN_URL, CLIENT_ID, REDIRECT_URI, SCOPE` (values in Global
  Constraints).

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_setup_token_oauth.py
import base64, hashlib
from urllib.parse import urlparse, parse_qs
from ccswitch import setup_token_oauth as sto


def _b64url_nopad(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def test_begin_pkce_challenge_is_s256_of_verifier():
    c = sto.begin()
    q = parse_qs(urlparse(c.authorize_url).query)
    assert q["code_challenge_method"] == ["S256"]
    expected = _b64url_nopad(hashlib.sha256(c.verifier.encode("ascii")).digest())
    assert q["code_challenge"] == [expected]


def test_begin_url_has_required_params():
    c = sto.begin()
    p = urlparse(c.authorize_url)
    assert p.scheme == "https" and p.netloc == "claude.com" and p.path == "/cai/oauth/authorize"
    q = parse_qs(p.query)
    assert q["client_id"] == [sto.CLIENT_ID]
    assert q["response_type"] == ["code"]
    assert q["redirect_uri"] == [sto.REDIRECT_URI]
    assert q["scope"] == [sto.SCOPE]
    assert q["state"] == [c.state]
    assert q["code"] == ["true"]


def test_begin_verifier_not_leaked_in_url_and_state_is_random():
    c1 = sto.begin()
    c2 = sto.begin()
    assert c1.verifier not in c1.authorize_url      # verifier stays server-side
    assert c1.state != c2.state                      # random per call
    assert c1.verifier != c2.verifier
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/python -m pytest server/tests/test_setup_token_oauth.py -q`
Expected: FAIL (`ModuleNotFoundError: ccswitch.setup_token_oauth`).

- [ ] **Step 3: Implement `begin()`**

```python
# src/ccswitch/setup_token_oauth.py
"""In-process OAuth2 PKCE flow for generating long-lived setup-tokens.

Replaces driving `claude setup-token` headlessly (which relies on a browser+poll
path that does not work without a browser). Constants are claude's own OAuth
client config, read from the bundled binary — verify against the real binary if
generation suddenly starts failing. NEVER log the token or the pasted code.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
SCOPE = "user:inference"
# Mimic the binary's exchange client (captured: axios/1.15.2) for WAF/Cloudflare friendliness.
_USER_AGENT = "axios/1.15.2"
# Captured: claude requests a 1-year token lifetime in the exchange body. Required.
_EXPIRES_IN = 31536000


@dataclass
class SetupTokenChallenge:
    authorize_url: str
    verifier: str
    state: str


def _b64url_nopad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def begin() -> SetupTokenChallenge:
    verifier = _b64url_nopad(secrets.token_bytes(32))
    challenge = _b64url_nopad(hashlib.sha256(verifier.encode("ascii")).digest())
    state = _b64url_nopad(secrets.token_bytes(32))
    query = urlencode(
        {
            "code": "true",
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
    )
    return SetupTokenChallenge(f"{AUTHORIZE_URL}?{query}", verifier, state)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/python -m pytest server/tests/test_setup_token_oauth.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Lint + commit**

```bash
.venv/bin/ruff check src/ccswitch/setup_token_oauth.py server/tests/test_setup_token_oauth.py
PYTHONPATH=src .venv/bin/mypy src/ccswitch/setup_token_oauth.py
git add src/ccswitch/setup_token_oauth.py server/tests/test_setup_token_oauth.py
git commit -m "feat(setup-token): begin() builds PKCE authorize URL in-process"
```

---

### Task 2.5: Adjust to verified shape — ✅ N/A

Folded into Task 3 (verified shape already baked in). No separate task.

---

### Task 3: `setup_token_oauth.exchange()` — token exchange + typed errors

**Files:**
- Modify: `src/ccswitch/setup_token_oauth.py`
- Test: `server/tests/test_setup_token_oauth.py`

**Interfaces:**
- Consumes: `SetupTokenChallenge` from Task 2.
- Produces:
  - `exchange(pasted_code: str, challenge: SetupTokenChallenge) -> str` (returns the
    `sk-ant-oat01-…` token).
  - Exceptions: `SetupTokenError` (base), `SetupTokenStateMismatch`, `SetupTokenInvalidCode`,
    `SetupTokenTransient`, `SetupTokenNoToken`.

- [ ] **Step 1: Write the failing tests**

```python
# append to server/tests/test_setup_token_oauth.py
import httpx
import pytest
from ccswitch import setup_token_oauth as sto


def _challenge():
    c = sto.begin()
    return c


def _exchange_with_handler(monkeypatch, handler, pasted):
    """Patch httpx.post (sync) with a fake that returns handler(url, **kwargs)."""
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return handler(url, **kwargs)

    monkeypatch.setattr(sto.httpx, "post", fake_post)
    c = sto.begin()
    # build a pasted code whose #state matches unless overridden
    code = pasted if pasted is not None else f"AUTHCODE#{c.state}"
    return c, code, captured


def test_exchange_success_returns_token(monkeypatch):
    def handler(url, **kw):
        return httpx.Response(200, json={"access_token": "sk-ant-oat01-REALTOKEN0123"})
    c, code, captured = _exchange_with_handler(monkeypatch, handler, None)
    tok = sto.exchange(code, c)
    assert tok == "sk-ant-oat01-REALTOKEN0123"
    body = captured["kwargs"].get("json") or captured["kwargs"].get("data")
    assert body["grant_type"] == "authorization_code"
    assert body["code_verifier"] == c.verifier
    assert body["client_id"] == sto.CLIENT_ID
    assert body["redirect_uri"] == sto.REDIRECT_URI
    assert body["expires_in"] == 31536000   # required for the 1-yr token (verified)
    headers = captured["kwargs"].get("headers", {})
    assert "User-Agent" in headers


def test_exchange_state_mismatch_raises_before_http(monkeypatch):
    called = {"n": 0}
    def handler(url, **kw):
        called["n"] += 1
        return httpx.Response(200, json={"access_token": "sk-ant-oat01-X"})
    c, _, _ = _exchange_with_handler(monkeypatch, handler, None)
    with pytest.raises(sto.SetupTokenStateMismatch):
        sto.exchange("AUTHCODE#WRONGSTATE", c)
    assert called["n"] == 0  # never hit the network


def test_exchange_invalid_code_raises(monkeypatch):
    def handler(url, **kw):
        return httpx.Response(400, json={"error": "invalid_grant"})
    c, code, _ = _exchange_with_handler(monkeypatch, handler, None)
    with pytest.raises(sto.SetupTokenInvalidCode):
        sto.exchange(code, c)


def test_exchange_network_error_is_transient(monkeypatch):
    def handler(url, **kw):
        raise httpx.ConnectError("boom")
    c, code, _ = _exchange_with_handler(monkeypatch, handler, None)
    with pytest.raises(sto.SetupTokenTransient):
        sto.exchange(code, c)


def test_exchange_200_without_token_raises(monkeypatch):
    def handler(url, **kw):
        return httpx.Response(200, json={"nope": 1})
    c, code, _ = _exchange_with_handler(monkeypatch, handler, None)
    with pytest.raises(sto.SetupTokenNoToken):
        sto.exchange(code, c)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/python -m pytest server/tests/test_setup_token_oauth.py -q`
Expected: FAIL (`AttributeError: module 'ccswitch.setup_token_oauth' has no attribute 'exchange'`).

- [ ] **Step 3: Implement `exchange()` + exceptions**

```python
# add to src/ccswitch/setup_token_oauth.py
import re

import httpx

_TOKEN_RE = re.compile(r"sk-ant-oat01-[A-Za-z0-9_\-]+")
_TIMEOUT_S = 30.0


class SetupTokenError(Exception):
    """Base for setup-token OAuth failures."""


class SetupTokenStateMismatch(SetupTokenError):
    pass


class SetupTokenInvalidCode(SetupTokenError):
    pass


class SetupTokenTransient(SetupTokenError):
    pass


class SetupTokenNoToken(SetupTokenError):
    pass


def exchange(pasted_code: str, challenge: SetupTokenChallenge) -> str:
    """Exchange a pasted ``code#state`` for a long-lived sk-ant-oat01 token.

    NEVER log pasted_code or the returned token.
    """
    code, _, state = pasted_code.strip().partition("#")
    if not state or state != challenge.state:
        raise SetupTokenStateMismatch("pasted code state does not match this request")

    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "code_verifier": challenge.verifier,
        "state": state,
        "expires_in": _EXPIRES_IN,
    }
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    try:
        resp = httpx.post(TOKEN_URL, json=body, headers=headers, timeout=_TIMEOUT_S)
    except httpx.HTTPError as exc:
        raise SetupTokenTransient(f"token endpoint unreachable: {exc!r}") from exc

    if resp.status_code == 400:
        raise SetupTokenInvalidCode("code invalid or expired")
    if resp.status_code >= 500:
        raise SetupTokenTransient(f"token endpoint {resp.status_code}")
    if resp.status_code != 200:
        raise SetupTokenError(f"unexpected token endpoint status {resp.status_code}")

    # The setup-token is the JSON `access_token` (verified). Regex is a fallback.
    try:
        token = (resp.json() or {}).get("access_token") or ""
    except ValueError:
        token = ""
    if not token:
        m = _TOKEN_RE.search(resp.text)
        token = m.group(0) if m else ""
    if not token.startswith("sk-ant-oat01-"):
        raise SetupTokenNoToken("token endpoint returned no setup-token")
    return token
```

> Verified live (see spec "Verified exchange request"): body is JSON; `redirect_uri` must equal the
> hosted `REDIRECT_URI` used in `begin()`; `expires_in` is required for the 1-yr token; response
> `access_token` is the `sk-ant-oat01-…` value.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/python -m pytest server/tests/test_setup_token_oauth.py -q`
Expected: PASS (8 passed).

- [ ] **Step 5: Lint + commit**

```bash
.venv/bin/ruff check src/ccswitch/setup_token_oauth.py server/tests/test_setup_token_oauth.py
PYTHONPATH=src .venv/bin/mypy src/ccswitch/setup_token_oauth.py
git add src/ccswitch/setup_token_oauth.py server/tests/test_setup_token_oauth.py
git commit -m "feat(setup-token): exchange() performs PKCE token exchange with typed errors"
```

---

### Task 4: Rewire `server/reauth.py` to use the in-process flow

**Files:**
- Modify: `server/reauth.py` (replace `_setup_token_runner`; update `start`/`submit_code` for
  `setup_token` mode; remove `_code_event`/`_submitted_code`/`on_pid`/`_launcher_pid` plumbing
  that exists ONLY for setup-token — keep anything `_production_runner`/Chrome login needs).
- Test: `server/tests/test_reauth_manager.py` (add cases; keep existing).

**Interfaces:**
- Consumes: `setup_token_oauth.begin()`, `setup_token_oauth.exchange()` and its exceptions
  (Task 2/3); `crypto.store_oauth_token` and `accounts.mark_reauthenticated` (existing).
- Produces: `ReauthManager.start(name, identity, mode="setup_token")` → state `awaiting_code`
  with `auth_url`; `ReauthManager.submit_code(name, code)` → state `success` (token stored) or
  `error` (typed message). No new `ReauthState` fields.

- [ ] **Step 1: Write the failing tests**

```python
# add to server/tests/test_reauth_manager.py  (mirror existing key-setup pattern)
import pytest
from ccswitch import setup_token_oauth as sto


@pytest.mark.asyncio
async def test_setup_token_start_enters_awaiting_code_without_subprocess(monkeypatch, tmp_path):
    # key available + store redirected (follow existing test helpers in this file)
    mgr = _make_manager(monkeypatch, tmp_path)  # existing helper or inline ReauthManager(...)
    state = await mgr.start("acct", "id@example.com", mode="setup_token")
    # poll to awaiting_code
    st = mgr.status("acct")
    assert st.state == "awaiting_code"
    assert "claude.com/cai/oauth/authorize" in (st.auth_url or "")


@pytest.mark.asyncio
async def test_setup_token_submit_code_stores_token(monkeypatch, tmp_path):
    mgr = _make_manager(monkeypatch, tmp_path)
    stored = {}
    monkeypatch.setattr("ccswitch.crypto.store_oauth_token",
                        lambda name, tok: stored.__setitem__(name, tok))
    monkeypatch.setattr("ccswitch.accounts.mark_reauthenticated", lambda name: stored.__setitem__("reauth", name))
    monkeypatch.setattr(sto, "exchange", lambda code, ch: "sk-ant-oat01-OK123")
    await mgr.start("acct", "id@example.com", mode="setup_token")
    await mgr.submit_code("acct", f"CODE#{mgr._setup_challenge.state}")  # name per impl
    assert stored["acct"] == "sk-ant-oat01-OK123"
    assert stored["reauth"] == "acct"
    assert mgr.status("acct").state == "success"


@pytest.mark.asyncio
async def test_setup_token_submit_invalid_code_sets_error(monkeypatch, tmp_path):
    mgr = _make_manager(monkeypatch, tmp_path)
    def boom(code, ch): raise sto.SetupTokenInvalidCode("bad")
    monkeypatch.setattr(sto, "exchange", boom)
    await mgr.start("acct", "id@example.com", mode="setup_token")
    await mgr.submit_code("acct", f"CODE#{mgr._setup_challenge.state}")
    st = mgr.status("acct")
    assert st.state == "error"
    assert "invalid or expired" in (st.message or "").lower()
```

> If `test_reauth_manager.py` lacks a `_make_manager` helper, construct `ReauthManager` directly
> following the existing tests in that file (they set `CCSWITCH_MASTER_KEY` and inject deps). Match
> the manager's actual attribute name for the stashed challenge (rename `mgr._setup_challenge` to
> whatever the impl uses).

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/python -m pytest server/tests/test_reauth_manager.py -q`
Expected: FAIL (setup_token start still spawns the old runner / attribute missing).

- [ ] **Step 3: Implement the rewire**

Replace `_setup_token_runner` and adjust `start`/`submit_code`. Sketch (adapt to the file's exact
structure — keep the `_run` timeout wrapper and the lock):

```python
# in start(), for mode == "setup_token": don't create _code_event/_submitted_code; instead:
#   from ccswitch import setup_token_oauth
#   self._setup_challenge = setup_token_oauth.begin()
#   self._state.auth_url = self._setup_challenge.authorize_url
#   self._state.state = "awaiting_code"
#   (no asyncio task spawning a PTY runner is needed for setup_token; the work happens in submit_code)

async def submit_code(self, name: str, code: str) -> ReauthState:
    async with self._lock:
        if self._state.state != "awaiting_code":
            return self.status(name)
        challenge = self._setup_challenge
    from ccswitch import accounts, crypto, setup_token_oauth
    try:
        token = await asyncio.to_thread(setup_token_oauth.exchange, code, challenge)
        await asyncio.to_thread(crypto.store_oauth_token, name, token)
        await asyncio.to_thread(accounts.mark_reauthenticated, name)
        async with self._lock:
            self._state.state = "success"
            self._state.message = None
    except setup_token_oauth.SetupTokenStateMismatch:
        await self._fail(name, "Code didn't match this request — restart generation.")
    except setup_token_oauth.SetupTokenInvalidCode:
        await self._fail(name, "Code invalid or expired — generate a new one.")
    except setup_token_oauth.SetupTokenTransient:
        await self._fail(name, "Couldn't reach the token service — try again.")
    except setup_token_oauth.SetupTokenError as exc:
        log.warning("setup-token exchange failed for %r: %s", name, exc)
        await self._fail(name, "Token service returned no token.")
    return self.status(name)
```

Add a small `_fail(name, msg)` helper that sets `state=error` + message under the lock and logs at
`warning` (no token/code in the message). Delete the now-unused setup-token PTY plumbing
(`_setup_token_runner`, the `_code_event`/`_submitted_code`/`on_pid`/`_launcher_pid` bits used only
by it). If `_run`'s timeout branch referenced the setup-token runner, simplify it so `setup_token`
mode no longer needs a background task (or keep a no-op task that just waits for the
awaiting_code→terminal transition if the facade expects `_state._task`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/python -m pytest server/tests/test_reauth_manager.py server/tests/test_reauth_api.py -q`
Expected: PASS (existing + new).

- [ ] **Step 5: Lint + commit**

```bash
.venv/bin/ruff check server/reauth.py server/tests/test_reauth_manager.py
PYTHONPATH=src .venv/bin/mypy server/reauth.py
git add server/reauth.py server/tests/test_reauth_manager.py
git commit -m "feat(reauth): setup-token via in-process OAuth exchange (no PTY/subprocess)"
```

---

### Task 5: Delete the PTY-scraping path (no half-dead code)

**Files:**
- Modify: `src/ccswitch/auth_login.py` (remove `capture_setup_token`, `_TOKEN_RE`, `_URL_RE` if
  unused elsewhere — grep first).
- Delete: `server/tests/test_capture_setup_token.py` and `server/tests/fakes/fake_claude_setup_token*.py`.

- [ ] **Step 1: Confirm nothing else imports the deleted symbols**

```bash
grep -rnE 'capture_setup_token|fake_claude_setup_token' src/ server/ | grep -v test_capture_setup_token
```
Expected: only `reauth.py` references (now removed in Task 4) — confirm zero live references remain.

- [ ] **Step 2: Delete the code and tests**

```bash
git rm server/tests/test_capture_setup_token.py
git rm server/tests/fakes/fake_claude_setup_token.py server/tests/fakes/fake_claude_setup_token_notoken.py server/tests/fakes/fake_claude_setup_token_hang.py
```
Then edit `src/ccswitch/auth_login.py` to remove `capture_setup_token` and the now-unused
`_TOKEN_RE`/`_URL_RE` (keep `capture_login` and everything the Chrome path uses). Remove any now-unused
imports (`pty`, `select`, `struct`, `termios`, `fcntl`, `signal`, `time`) **only if** no other
function in the file uses them — verify with `ruff` (F401).

- [ ] **Step 3: Verify the broader suite is green**

Run: `PYTHONPATH=src .venv/bin/python -m pytest server/tests/test_setup_token_oauth.py server/tests/test_reauth_manager.py server/tests/test_reauth_api.py server/tests/test_reauth_no_token_leakage.py -q`
Expected: PASS. Also `.venv/bin/ruff check src/ccswitch/auth_login.py` (no F401) and
`PYTHONPATH=src .venv/bin/mypy src/ccswitch/auth_login.py`.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor(auth): delete dead setup-token PTY-scraping path"
```

---

### Task 6: Full-suite sweep + PR

The reauth state machine and `auth_login` are shared; signature/shape changes can break unrelated
callers only the full suite catches (memory `signature-change-needs-full-suite`).

- [ ] **Step 1: Run the full server suite**

Run: `PYTHONPATH=src .venv/bin/python -m pytest server/tests/ -q 2>&1 | tail -30`
Expected: no regressions vs. a baseline (a fresh worktree has ~160 environmental failures — judge
by diffing failure node-id sets, not raw counts; memory `worktree-fullsuite-baseline-diff`). Any
NEW failure referencing reauth/auth_login/setup-token must be fixed before proceeding.

- [ ] **Step 2: Run ruff + mypy across changed files**

```bash
.venv/bin/ruff check src/ccswitch/setup_token_oauth.py src/ccswitch/auth_login.py server/reauth.py server/tests/test_setup_token_oauth.py server/tests/test_reauth_manager.py
PYTHONPATH=src .venv/bin/mypy src/ccswitch/setup_token_oauth.py src/ccswitch/auth_login.py server/reauth.py
```

- [ ] **Step 3: Open the PR with auto-merge**

```bash
git push -u origin feat/setup-token-oauth-redesign
gh pr create --title "feat(setup-token): in-process OAuth2 PKCE generation (replace PTY scraping)" --body "<summary + 'Fixes' link to the spec; note Task 1 verification result>"
gh pr merge <#> --squash --auto
```
Confirm the PR is non-draft and auto-merge is armed; let required CI (`lint-and-test`, `frontend`)
land it.

- [ ] **Step 4: Post-merge live validation**

After deploy, generate a real token for one account via the ccswitch UI (code-paste) and confirm
`success` + a file at `~/.claude-accounts/store/oat/<slug>` + the account leaves `needs-reauth`.

---

## Self-Review

**Spec coverage:** begin() → Task 2; exchange()+errors → Task 3; reauth rewire (start/submit_code,
store, mark_reauthenticated, error→state map) → Task 4; deletions → Task 5; testing → Tasks 2–6;
byte-verification → Task 1; "no facade fan-out / unchanged frontend+API" → respected (no
`ReauthState` field added, no API/frontend edits). Covered.

**Placeholders:** none — every code step has concrete code; the only deliberately-deferred item is
Task 1's observed body shape, gated by Task 2.5 with an explicit fallback path in Task 3's note.

**Type consistency:** `SetupTokenChallenge(authorize_url, verifier, state)`, `begin()`, `exchange()`,
and the five exception names are used identically across Tasks 2–4. The manager's stashed-challenge
attribute name is flagged as impl-defined (tests must match the chosen name).
