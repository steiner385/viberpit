# Design: GitHub Copilot Pro+ as a ccswitch account type

**Date:** 2026-07-06
**Status:** Approved design → implementation planning
**Feature branch:** `feat/copilot-account-type`

## 1. Summary

ccswitch today manages one kind of account: an Anthropic Max/Pro OAuth subscription,
stored as `store/{name}.cred.json` and activated purely by pointing `CLAUDE_CONFIG_DIR`
at an isolated per-account directory. There is no notion of an account *type*.

This feature adds a second account type — a **GitHub Copilot Pro+ subscription** whose
Claude-model access is exposed to `claude` via a **locally-supervised translating proxy**.
Copilot accounts are first-class: they appear in the dashboard, can be selected for a
session, and participate in usage-aware auto-rotation alongside Anthropic accounts.

The change is deliberately narrow-seam: the account *selector* needs no change, the SDK
launch-env path already has an `extra_env` hook, and the proxy supervisor is a near-verbatim
clone of the existing `X11Manager`.

### Load-bearing decisions (from brainstorming)

1. **Credential type** — GitHub Copilot Pro+ with Claude-model access (not an Anthropic API
   key, not "sign in to Claude with GitHub").
2. **Proxy ownership** — ccswitch manages the proxy turnkey (supervised child process), like
   it manages the `claude` PTYs today.
3. **Proxy implementation** — vendor a proven proxy rather than build a translator in-house.
4. **Proxy runtime** — a single static **Go binary** (`whtsky/copilot2api`), not the Bun-based
   `ericc-ch/copilot-api`. Reasoning in §8.
5. **Rotation** — full parity: poll the proxy's `/usage` endpoint, map premium-request quota
   into the existing perishable-quota model, and rank Copilot accounts alongside Anthropic ones.
6. **Suggestions/summaries** — the Haiku one-shot "next-prompt suggestion" and "status summary"
   features are **disabled** for Copilot-backed sessions (they would otherwise burn premium
   requests; they hardcode `CLAUDE_CONFIG_DIR` with no proxy env today).

### Honest risk ledger

- **ToS gray area.** Routing a Copilot subscription into a non-Copilot client is a gray area
  under GitHub's Copilot Terms. This is the user's own account for their own use; we proceed,
  but it is recorded here, not hidden.
- **Abuse detection.** GitHub warns scripted use "may trigger abuse-detection systems." Our
  `/usage` polling must be rate-limited (reuse the existing usage-cache TTL; do not hammer).
- **Undocumented usage upstream.** There is no supported public REST API for *individual*
  Copilot premium-request usage; the proxy taps an internal, undocumented endpoint. Quota
  data is therefore best-effort — the design degrades gracefully (never a false `limited`).
- **Reverse-engineered proxy.** The proxy can break on a Copilot API change. Mitigated by
  pinning a release + checksum, health-gating every spawn, and a stable `CopilotProxyManager`
  interface so the specific binary is swappable (§8).

## 2. Vendored proxy: `whtsky/copilot2api`

A Go proxy, distributed as prebuilt version-tagged static Linux binaries, that exposes GitHub
Copilot as an Anthropic-compatible API. Verified capabilities:

- `POST /v1/messages` (+ `/v1/messages/count_tokens`) — Anthropic Messages API, streaming.
- `GET /usage` — Copilot usage/quota (premium requests).
- GitHub **device-flow** auth; credentials persisted under `-token-dir`; auto-refreshes the
  short-lived Copilot token internally.
- Flags: `-port <int>` (listen port), `-token-dir <path>` (credential storage).
- Prebuilt `copilot2api-linux-amd64` in GitHub Releases, tagged (e.g. `v0.4.0`) for pinning.

We pin a specific release tag and verify the downloaded binary's checksum. The binary is
resolved at runtime via `shutil.which` / a vendored path, exactly like `resolve_chromium`.

Because both candidate proxies (`copilot2api`, `copilot-api`) implement the identical contract
(`POST /v1/messages` + `GET /usage` + a port flag + device-flow auth), `CopilotProxyManager`
is written against *that contract*, making the concrete binary a swappable implementation detail.

## 3. Where the Copilot type fits (the `provider` axis)

`provider` becomes explicit with two values, `anthropic` (the implicit default today) and
`copilot`. Copilot fits *alongside* the Anthropic path, never replacing it.

| Concern | Anthropic (today) | Copilot (new) |
|---|---|---|
| Durable credential | `store/{name}.cred.json` (OAuth blob) | `store/copilot/{name}/credentials.json` (proxy token-dir) |
| Discovery | glob `store/*.cred.json` | glob `store/copilot/*/` |
| Activation env | `CLAUDE_CONFIG_DIR` → OAuth creds | `CLAUDE_CONFIG_DIR` (isolation only) **+** `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` → proxy |
| Usage source | `oauth.fetch_usage` | proxy `GET /usage` → synthesized `Usage` |
| Quota semantics | session% / weekly% | premium-requests → mapped to session%/weekly% + monthly reset |

**Single source of truth (constitution VI):** a single `provider_of(name)` resolver and an
`all_accounts() -> list[(name, provider)]` union live in one module — no scattered
`if name in copilot_dir` checks.

## 4. Component design

### 4.1 Core (`src/ccswitch/`)

- **`paths.py`** — add `copilot_store_dir()` and `copilot_token_dir(name)`.
- **New `copilot.py`** — the provider's single source of truth:
  - discovery (`copilot_account_names()`),
  - `usage_from_proxy(payload) -> oauth.Usage` mapping (premium-requests → `session_pct`,
    `weekly_pct`; monthly reset → `session_resets_at`, `weekly_resets_at`),
  - `launch_env(port, secret) -> dict` returning `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`,
    `ENABLE_TOOL_SEARCH=true`.
- **`accounts.py`**
  - `_account_names()` → union Anthropic + Copilot discovery (or a new `all_accounts()`).
  - `usage_for()` → branch on provider: Copilot reads the proxy `/usage` (via the daemon's
    proxy manager) instead of the OAuth refresh/fetch path. Graceful degradation preserved:
    unreachable → `None` (unknown), never `needs-reauth` for a transient proxy blip.
  - `list_accounts()` → include `provider`.
  - `remove_account()` → Copilot: stop the proxy, remove the token-dir + config dir.
  - a `copilot` add variant (drives the device flow — see 4.3).
- **`selector.py`** — **unchanged.** Synthesized `Usage` values flow through the existing
  perishability math, so Copilot accounts rank for free.

### 4.2 Daemon / server (`server/`)

- **New `server/sessions/copilot_proxy.py` — `CopilotProxyManager`**, modeled 1:1 on
  `X11Manager` (`server/sessions/x11manager.py`):
  - `resolve_copilot_proxy()` mirrors `resolve_chromium` (`x11manager.py:23-29`) —
    fail-loud-disable when the binary is absent, like `build_x11_manager` returning `None`.
  - a port allocator mirroring `DisplayAllocator`.
  - `_await_proxy_ready` = poll `GET /usage` until HTTP 200 (mirrors `_await_vnc_ready`).
  - post-spawn liveness grace (mirrors `_await_browser_alive`: "a dead launch must not look
    healthy" — a token-revoked crash surfaces as an error, not phantom-healthy).
  - terminate→wait→kill teardown (`_terminate`); idle ref-count teardown (`detach` /
    `_idle_teardown`); `stop_all()` on shutdown.
  - `sweep_orphan_processes` matching the `copilot2api` exe name, gated to owned accounts —
    reaps leaks from a SIGKILLed prior daemon (mirrors the x11 orphan reap).
  - public: `ensure(account) -> port`, `usage(account) -> Usage | None`, `env_for(account)`.
  - **One proxy per account**, shared across all that account's live sessions.
- **`orchestrator.py`**
  - `create_session` (`:674-712`): after account pick, if provider is Copilot,
    `await proxy.ensure(account)` (health-gated) *before* constructing the `SessionDriver`.
  - `_session_factory` / `_oat_env` (`:436-458`): branch on provider — inject the proxy env
    (`copilot.launch_env`) instead of `CLAUDE_CODE_OAUTH_TOKEN` for Copilot accounts.
  - new methods: `copilot_add_start/status/cancel`, `copilot_usage`, proxy lifecycle hooks;
    wire proxy teardown into orchestrator shutdown and startup orphan-reap.
- **RPC 3-touch** (project invariant: a request-layer orchestrator method needs a daemon
  proxy + registered RPC, or breadth tests fail). Each new orchestrator method touches:
  1. `server/sessions/orchestrator.py` — the method itself.
  2. `server/ipc/orchestrator_rpc.py` — append to `_SIMPLE` (JSON-safe return) or add a
     custom `register_rpc(app, "<method>", handler)`.
  3. `server/sessions/remote.py` — a `RemoteOrchestrator` proxy calling `self._client.call(...)`.
- **`server/api/accounts.py`**
  - `_check_name_and_slot` (`:135-147`) becomes **provider-aware**: a Copilot account is valid
    when its token-dir exists, not a `.cred.json` slot.
  - `_account_response` (`:106-124`) adds `provider`.
  - new device-flow endpoints: `POST /api/accounts/add/copilot` (start),
    `GET /api/accounts/add/copilot` (poll: `pending | awaiting_authorization | authorized |
    failed`, returns `userCode` + `verificationUri`), `POST /api/accounts/add/copilot/cancel`.
    These drive a `CopilotAuthManager` that runs the proxy binary's own device flow and scrapes
    the `user_code` + `github.com/login/device` URL from its output.

### 4.3 Type system / frontend (`web/src/`)

- **`api/types.ts`** — `Account.provider?: 'anthropic' | 'copilot'`; a Copilot device-flow
  state type. Also collapse the existing `needs-relogin` / `needs-reauth` duplication in the
  status union while here (single source of truth).
- **`api/hooks.ts`** — `useAddCopilotAccount()` (start), `useCopilotAddStatus()` (poll), cancel.
- **`components/AddAccountDialog.tsx`** — a provider toggle; the Copilot branch renders the
  device code + `github.com/login/device` link + live poll (vs. the existing VNC-browser branch).
- **`components/AccountsManager.tsx` / `AccountRow`** — a **"Copilot"** provider chip; the
  usage badge reads premium-requests; the reauth button re-runs device flow for Copilot.

## 5. Integration flow

### 5.1 Setup (add a Copilot account)

1. UI → "Add account → GitHub Copilot", enter a short name.
2. `POST /api/accounts/add/copilot {name}` → orchestrator → **RPC** → daemon
   `CopilotAuthManager` spawns `copilot2api -token-dir store/copilot/{name}` in login mode and
   scrapes the device code from its output.
3. `GET …/add/copilot` polls → `{state: awaiting_authorization, userCode, verificationUri}`;
   the UI shows the code + link.
4. The user authorizes on GitHub; the proxy completes the device flow, writes
   `credentials.json` into the token-dir, and becomes ready.
5. Poll → `authorized`. The account is now discoverable; ledger records `provider=copilot`,
   `status=ok`.

### 5.2 Message proxying (running a session)

1. `create_session` picks an account (selector, now including Copilot via synthesized `Usage`).
2. Copilot → `CopilotProxyManager.ensure(account)` lazily starts a per-account proxy on
   `127.0.0.1:<ephemeral>`, **health-gated on `GET /usage`**.
3. `_session_factory` injects `extra_env = {ANTHROPIC_BASE_URL: http://127.0.0.1:<port>,
   ANTHROPIC_AUTH_TOKEN: <shared secret>, ENABLE_TOOL_SEARCH: true}` alongside the isolated
   `CLAUDE_CONFIG_DIR`.
4. The `claude-agent-sdk` process sends Anthropic Messages → proxy translates to Copilot
   chat-completions, streams back, and refreshes the Copilot token internally.
5. The usage poller reads `/usage` (rate-limited by the existing cache TTL) → maps
   premium-requests → `Usage` → ledger → drives rotation + UI badges.
6. Idle account (no live sessions) → proxy torn down after a grace period. Daemon restart →
   orphan-reap + lazy restart on the next session.

### 5.3 Status mapping

- `/usage` 401 or login expired → `needs-reauth` (re-run device flow).
- premium quota exhausted → `limited` (selector excludes at ceiling).
- proxy unreachable / usage unmapped → `Usage=None` → `unknown`, ranked last (optimistic),
  never a false `limited`.
- Copilot session → Haiku suggestion/summary one-shots are **skipped** (decision 6).

## 6. Security

- The proxy binds **`127.0.0.1` only**; sessions reach it via loopback.
- `ANTHROPIC_AUTH_TOKEN` is a per-proxy shared secret, not a real credential.
- The GitHub token lives only in the proxy's token-dir under `store/`, mode `0700` like the
  rest of the credential store; API responses never include token material (existing FR-023).

## 7. Testing (TDD, constitution V)

CI provisions only Python 3.12 + Node 20 — **no Go toolchain, no proxy binary**. Tests must
pass with the binary absent, exactly like the x11 suite:

- **`CopilotProxyManager`** — injected `spawn` + injected readiness/`usage` probes (mirrors
  `X11Manager(spawn=…, ready_check=…)`); assert health-gate, liveness-grace rejection, idle
  teardown, orphan reap, `shutil.which`→`None` graceful-disable.
- **`copilot.usage_from_proxy`** — quota→`Usage` mapping incl. missing/garbage payloads → `None`.
- **`accounts`** — union discovery, provider-aware `usage_for`, `list_accounts` provider field,
  `remove_account` teardown.
- **selector** — a regression test that a synthesized Copilot `Usage` ranks alongside Anthropic.
- **API** — provider-aware `_check_name_and_slot`; device-flow add endpoints (start/poll/cancel)
  with a faked `CopilotAuthManager`.
- **RPC breadth** — the existing breadth test must see the new orchestrator methods proxied.
- **Frontend** — `AddAccountDialog` provider toggle + device-flow view; `AccountRow` provider
  chip; `useAddCopilotAccount` hook. (Note: adding a hook `AccountsManager` calls may require
  updating the `@/api/hooks` `vi.mock` lists per the known SessionCard/SessionList gotcha.)

Quality gates: `ruff`, `mypy server/`, `pytest tests/ server/tests/`, and web
`tsc`/`eslint`/`vitest` must be green.

## 8. Runtime choice rationale (Go static binary vs Bun)

Analyzed against ccswitch's four deployment constraints; every one favors the static Go binary:

- **Managed-process model** — ccswitch supervises self-contained binaries resolved via
  `shutil.which` and reaped by exe name (`x11manager.py`). A single static binary drops into
  the x11 patterns near-verbatim. Bun's `copilot-api` supervises a `bun`/`node` process running
  a script → orphan reaping can't safely name-match, deeper process tree.
- **Version pinning** — one release asset + checksum (Go) vs two axes (npm package **and** a
  separate Bun ≥1.2.x runtime) with the docs pushing `@latest`.
- **CI / provisioning** — `curl -L` a static binary vs installing a Bun runtime toolchain on
  CI and the deploy host.
- **Health checks** — both expose `GET /usage`; `copilot2api` additionally documents token
  auto-refresh, which a supervisor needs to tell "proxy down" from "token dead".

The one place Bun's `copilot-api` is genuinely stronger — more battle-tested (35 releases),
better-documented interface — is neutralized by the shared contract: if `copilot2api` breaks,
swapping to `copilot-api` is a binary + launch-arg change behind the stable
`CopilotProxyManager` interface, not a redesign.

## 9. Out of scope (YAGNI)

- Non-`linux-amd64` proxy binaries (add when a non-amd64 host appears).
- Copilot Business/Enterprise account types (`--account-type`); v1 is individual Pro+.
- Suggestions/summaries on Copilot sessions (decision 6 — disabled).
- Building our own translator (decision 3 — vendored).

## References

- `whtsky/copilot2api` — https://github.com/whtsky/copilot2api
- `ericc-ch/copilot-api` — https://github.com/ericc-ch/copilot-api
- Claude Code auth (base URL / auth token, `ENABLE_TOOL_SEARCH`) —
  https://code.claude.com/docs/en/authentication
- No public individual Copilot usage API —
  https://github.com/orgs/community/discussions/185848
