# Setup-token generation: in-process OAuth2 PKCE (replace PTY scraping)

**Date:** 2026-06-30
**Status:** Approved (design); ready for implementation plan
**Related:** PR #362 (observability), memory `setup-token-needs-browser-not-paste`,
`setup-token-auth-pivot`, `reauth-facade-field-and-method-fanout`, `cloudflare-1010-refresh-403-mass-reauth`

## Problem

ccswitch generates per-account long-lived inference tokens (`sk-ant-oat01-…`) by driving
`claude setup-token` in a headless PTY and scraping the result
(`auth_login.capture_setup_token` + `reauth._setup_token_runner`). **This has never worked.**

Verified live against the real binary (claude 2.1.183):
- `claude setup-token`'s success path is **browser + server-poll auto-complete**: it opens a
  browser, the user approves, and claude polls `claude.com` for the code and finishes on its own.
  (Confirmed: plain `claude setup-token` prints the token with **no paste**.)
- The "Paste code here if prompted" prompt is a **fallback**. Run headless, ccswitch is forced
  onto that fallback. With a **valid** code the binary reads the code (strace:
  `read(<pts>) = 93`) and then **spins forever** — no token, no error. (An *invalid* code errors
  fast, which is why earlier fixes looked plausible.)
- The token store (`~/.claude-accounts/store/oat/`) was empty — zero successful generations ever.

Ruled out by live testing (do not re-chase): the `\r` submission fix (#360), continuous PTY
draining, controlling-tty (`setsid`+`TIOCSCTTY`), bracketed-paste, elapsed-time/staleness.

## Goal

Make setup-token generation reliable and fully headless while **keeping the existing mobile
code-paste UX** (#357): generate → show authorize URL → approve on any device → paste `code#state`
→ done. No VNC, no subprocess.

## Approach

Stop driving the binary. ccswitch performs the **OAuth2 PKCE** flow itself, in-process, with
`httpx`. The OAuth client config is read out of the bundled binary (confirmed present):

| Field | Value |
|---|---|
| Authorize URL | `https://claude.com/cai/oauth/authorize` |
| Token URL | `https://platform.claude.com/v1/oauth/token` |
| Client ID | `9d1c250a-e61b-44d9-88ed-5944d1962f5e` |
| Redirect URI | `https://platform.claude.com/oauth/code/callback` |
| Scope | `user:inference` |
| PKCE | S256 |
| Pasted code format | `<authorization_code>#<state>` |

**Rejected alternative:** drive the real binary inside a virtual browser (Xvfb) approved via the
existing VNC reauth infra (`_production_runner`). More robust to claude changes, but worse for
the mobile-first goal (requires VNC approval, discards #357's paste UI). Not chosen.

**Accepted risk:** these are claude's OAuth client details; if Anthropic rotates the client_id or
changes the endpoint/exchange shape, generation breaks until the constants are updated. The
typed errors + WARNING logs (below) make such a break diagnosable immediately.

## What stays unchanged

- **Frontend (#357):** AccountsManager per-row "Generate long-lived token", authUrl display +
  copy, code-paste field, "Submit code".
- **API:** `POST /accounts/{name}/reauth?mode=setup_token`, `GET /accounts/{name}/reauth`,
  `POST /accounts/{name}/reauth/code`.
- **State machine:** `running → awaiting_code (authUrl) → success | error`.
- **Reauth facade** (`InProcessReauthAdapter` / `RemoteReauthManager`) and `ReauthState` shape —
  no new fields, so no facade fan-out (cf. `reauth-facade-field-and-method-fanout`).

## What changes

### New module: `src/ccswitch/setup_token_oauth.py`

Single purpose: the PKCE flow. No I/O beyond the one token POST.

```python
# Constants — discovered from the bundled claude binary; verify against the real binary.
AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
TOKEN_URL     = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID     = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
REDIRECT_URI  = "https://platform.claude.com/oauth/code/callback"
SCOPE         = "user:inference"

@dataclass
class SetupTokenChallenge:
    authorize_url: str
    verifier: str          # held server-side only; never sent to the browser
    state: str

def begin() -> SetupTokenChallenge: ...
    # generate verifier (high-entropy, url-safe), challenge=b64url(sha256(verifier)),
    # state (random). Build authorize_url with query:
    #   code=true&client_id=...&response_type=code&redirect_uri=...&scope=user:inference
    #   &code_challenge=...&code_challenge_method=S256&state=...

def exchange(pasted_code: str, challenge: SetupTokenChallenge) -> str: ...
    # split pasted_code on "#" -> (code, state); verify state == challenge.state
    # POST TOKEN_URL with: grant_type=authorization_code, code, state,
    #   client_id, redirect_uri, code_verifier=challenge.verifier
    # real User-Agent header (cf. cloudflare-1010); timeout ~30s
    # parse sk-ant-oat01-... from response; return it (raises on any failure)
```

Typed exceptions: `SetupTokenStateMismatch`, `SetupTokenInvalidCode` (400/`invalid_grant`),
`SetupTokenTransient` (network/5xx/timeout), `SetupTokenNoToken` (200 but no token).

### `server/reauth.py`

Replace the PTY worker-thread runner with an in-process implementation:
- `start(mode=setup_token)`: call `setup_token_oauth.begin()`, stash the `SetupTokenChallenge`
  on the manager, set `state=awaiting_code` + `auth_url`. **No `asyncio.to_thread`, no
  `get_code`/`on_pid`/`_code_event` plumbing, no subprocess.**
- `submit_code(name, code)`: `exchange()` is sync (`httpx.post`) for simple unit testing; call it
  via `await asyncio.to_thread(...)` so the daemon event loop is not blocked. Then
  `crypto.store_oauth_token(name, token)` + `accounts.mark_reauthenticated(name)`; set
  `state=success`. On typed error → `state=error` with a user-facing message.
- The overall `_run` timeout still bounds the await-for-code window.

### Deletions (same PR — no half-dead code)

- `auth_login.capture_setup_token` and module-level `_TOKEN_RE` / `_URL_RE` (only used by it).
- `reauth._setup_token_runner` and its `_code_event` / `_submitted_code` / `on_pid` /
  `_launcher_pid` machinery **specific to setup-token** (keep anything the Chrome-login
  `_production_runner` still needs).
- `server/tests/test_capture_setup_token.py` and its `fakes/fake_claude_setup_token*.py`.
- The PR #362 observability test moves into the new module's tests (its WARNING lines retire
  with the code they guarded).

## Error handling → `ReauthState`

| Condition | state | message (user-facing) |
|---|---|---|
| pasted `#state` ≠ ours | error | "Code didn't match this request — restart generation." |
| 400 / `invalid_grant` | error | "Code invalid or expired — generate a new one." |
| network / 5xx / timeout | error | "Couldn't reach the token service — try again." |
| 200 without `sk-ant-oat01-` | error | "Token service returned no token." |

Every path logs at `warning` (with account name, never the token/code). No silent failures.

## Testing

**Unit — `setup_token_oauth`:**
- `begin()`: `b64url(sha256(verifier)) == code_challenge`; URL has all required params and S256;
  state differs across calls; verifier never appears in `authorize_url`.
- `exchange()` (mocked `httpx` transport): success → returns token; state-mismatch → raises
  before any HTTP; 400/`invalid_grant` → `SetupTokenInvalidCode`; network error → transient;
  200-without-token → `SetupTokenNoToken`. Assert the request carries `code_verifier`,
  `grant_type=authorization_code`, client_id, redirect_uri, and a real User-Agent.

**Integration — `reauth`:**
- `start(mode=setup_token)` → `awaiting_code` with a well-formed `auth_url` and **no subprocess
  spawned**.
- `submit_code` success → token stored encrypted + `mark_reauthenticated` called + `state=success`.
- `submit_code` with each typed error → `state=error` + correct message.

**Live-gated (skipped in CI):** one end-to-end test behind an env flag requiring a real approval.

## Verified exchange request (DONE — mitmproxy capture + live end-to-end, 2026-06-30)

Captured the real binary's exchange via mitmproxy AND validated our own implementation against the
live endpoint (hosted redirect + pasted code → 200 + token; stored for `tony_kindash.com`). No
residual risk.

**Request:** `POST https://platform.claude.com/v1/oauth/token`
- Headers: `Content-Type: application/json`, `Accept: application/json`, `User-Agent: axios/1.15.2`.
- Body (JSON):
  `{"grant_type":"authorization_code","code":"<auth code, ~48 ch, part BEFORE '#'>",
  "redirect_uri":"<MUST match the authorize redirect_uri>","client_id":"9d1c250a-…",
  "code_verifier":"<43 ch>","state":"<part AFTER '#'>","expires_in":31536000}`
  - `expires_in: 31536000` (1 yr) — **required to mint the long-lived token**; always send it.
  - `redirect_uri`: the binary used `http://localhost:<random>/callback` (it runs a loopback
    callback server in the browser path). We have no loopback in the headless daemon and the user
    approves on a remote device, so we use the **HOSTED** `https://platform.claude.com/oauth/code/callback`
    in BOTH `begin()` and `exchange()` — validated working.

**Response 200 (JSON):** `token_type=Bearer`, **`access_token`** = `sk-ant-oat01-…` (108 ch) ← the
setup-token; `refresh_token` = `sk-ant-ort01-…` (unused, non-rotating); `expires_in=31536000`,
`scope=user:inference`, plus `token_uuid/organization/account`. Parse `access_token` (regex
`sk-ant-oat01-…` as fallback). 400 → invalid/expired; ≥500/network → transient.

## Out of scope

- Token rotation/refresh (setup-tokens are 1yr, non-rotating — `setup-token-auth-pivot`).
- The immediate manual unblock for kindash (already handled out-of-band: plain-CLI generate +
  `crypto.store_oauth_token('tony_kindash.com', tok)`).
- Any change to the Chrome OAuth-login reauth path (`_production_runner` / `capture_login`).
