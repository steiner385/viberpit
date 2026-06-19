# Research: ccswitch-web

Resolves the technical unknowns surfaced by the spec, the 8-persona clarify review, and the
binary-verification work. Format per decision: **Decision / Rationale / Alternatives**.

## R1 — Per-account `.claude.json` provisioning

**Decision:** `ccswitch add` captures the temp-login dir's `.claude.json` to `store/<name>.claude.json`.
`compose(name)` writes `cfg/<name>/.claude.json` = the account's captured file (correct
`oauthAccount`/`userID` + account caches) **overlaid** with a shared allowlist (`mcpServers`,
`projects`, `allowedTools`) from `store/claude.shared.json`.
**Rationale:** Verified: `claude` refuses to launch under a config dir lacking `.claude.json`, and
the file is account-coupled (`oauthAccount`, `userID`). Per-account identity must be preserved;
sharing MCP/trust avoids reconfiguring each account.
**Alternatives:** Symlink one shared `~/.claude.json` (rejected — wrong identity per account); fresh
empty `.claude.json` (rejected — loses MCP servers + project trust).

## R2 — Wait-for-idle rotation mechanism

**Decision:** The SessionManager tracks per-session idle state (no PTY output for `idle_threshold`,
default 2s, and not awaiting a tool-permission prompt). On a limit signal it marks the session
`limited`, waits until idle (bounded by `rotation_max_wait`, default 60s), then SIGTERMs the PTY,
relaunches `claude --resume <session-id>` under the next-best account, rebridges the same WebSocket,
and forces a repaint (winsize nudge). If `rotation_max_wait` elapses mid-turn, fall back to a
graceful interrupt then rotate.
**Rationale:** Spec clarify chose wait-for-idle (no dropped in-flight turn). The transcript lives in
the shared `projects/`, so `--resume` reconstructs context on the new account.
**Alternatives:** Immediate kill+resume (rejected per clarify — drops in-flight turn); cooperative
interrupt-first (kept only as the max-wait fallback).

## R3 — PTY ownership & reconnect

**Decision:** v1 — PTYs are children of the Uvicorn process; a per-session bounded scrollback ring
(default 5000 lines) is held server-side so a WebSocket reconnect replays recent output then resumes
live (SC-008 ≤10s). Server **process restart** orphaning sessions is an accepted v1 limitation,
documented; Phase 4 may add a detached supervisor.
**Rationale:** Simplest correct model that satisfies the network-drop reconnect guarantee without a
process-supervision dependency in v1.
**Alternatives:** Detached process group / tmux-style supervisor (deferred — complexity); disk-backed
PTY log (deferred — the ring buffer suffices for v1 reconnect).

## R4 — Limit detection (terminal/non-structured mode)

**Decision:** Primary trigger = the authoritative usage poll (exact remaining usage + the provider's
limit response from `/api/oauth/usage`), per account, pushed to subscribers. Secondary safety net =
transcript-tail of the session JSONL for the live rate-limit event (reused from the CLI `ccmax`
detector). The terminal byte stream is NOT parsed for limits.
**Rationale:** NFR-006 requires authoritative signals; usage polling is exact and deduplicated
per-account (not per-session). Constitution Principle III (verify reality) — these signals were
verified against the binary/endpoint.
**Alternatives:** Scrape PTY stdout for "limit" text (rejected — fragile, non-authoritative).

## R5 — Auth: Tailscale identity verification

**Decision:** The server verifies caller identity from the Tailscale-provided identity (the
`Tailscale-User-Login` header when fronted by `tailscale serve`, or `tailscaled` LocalAPI `whois`
on the peer address), allowlisted to the operator's tailnet identity; a signed server-side session
cookie caches it. The passkey/WebAuthn path is implemented behind a disabled flag and is the
required gate before any non-Tailscale exposure (Principle IV, v1.1.0). All routes + the WebSocket
upgrade pass through the same identity middleware.
**Rationale:** Spec clarify chose network-identity for v1; this keeps "no unidentified caller" while
deferring app-login. Header/whois verification prevents spoofing when bound to loopback behind
`tailscale serve`.
**Alternatives:** Trust source IP only (rejected — spoofable, no identity); app login in v1
(deferred per clarify).

## R6 — Terminal accessibility under WCAG 2.1 AA

**Decision:** Management surfaces (list, accounts, controls, dialogs) meet WCAG 2.1 AA in full
(keyboard, contrast ≥4.5:1, focus, semantics, status by text+icon not color, 44px targets, focus
returns to the list on close, a visible "back to list" exit from the terminal). The terminal
surface uses xterm.js with an `aria-live="polite"` recent-output region (last N lines) plus the
accessibility addon, and carries a documented partial-conformance note for the raw live stream
(WCAG 1.1.1/2.2.2 "essential live process" with a pause-output control).
**Rationale:** A raw terminal cannot fully satisfy AA; scoping full AA to management UI + a
screen-reader-readable recent-output mirror is the honest, standards-aligned approach.
**Alternatives:** Claim full AA on the terminal (rejected — not achievable with a live PTY); no a11y
on terminal (rejected — violates NFR-004).

## R7 — Real-time channel

**Decision:** A single authenticated WebSocket per client multiplexes message types: terminal I/O
(per session) + a control/event channel (session status, usage updates, account-rotation notices).
Status changes pushed ≤3s; usage ≤60s staleness.
**Rationale:** One channel simplifies auth and ordering; satisfies FR-019 push requirement.
**Alternatives:** Separate SSE for events + WS for terminal (rejected — two auth paths); polling
(rejected — staleness + load).

## R8 — Concurrent placement safety

**Decision:** Account selection takes a short-TTL soft reservation (existing `ccswitch` ledger
mechanism) under a lock so two near-simultaneous `new session` requests don't pick the same account
when another has headroom (FR-009a).
**Rationale:** Reuses the proven CLI reservation; deterministic and testable.
**Alternatives:** DB row lock (heavier); best-effort (rejected — flaky correctness).

## R9 — Token-at-rest envelope encryption

**Decision:** Replayable secrets (refresh tokens) encrypted with AES-256-GCM; data key from a master
key supplied by env/secret store (`CCSWITCH_MASTER_KEY`); ciphertext + nonce stored in the
per-account file (0600). Decrypt only in memory at use. Access tokens (short-lived) may stay
plaintext-at-0600 or encrypted; refresh tokens always encrypted.
**Rationale:** Principle IV "encrypt replayable secrets"; portable (key as a Railway secret later).
**Alternatives:** OS keyring (rejected — doesn't travel to headless cloud); 0600-only (rejected —
single file read leaks all accounts).
