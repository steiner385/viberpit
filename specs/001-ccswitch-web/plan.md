# Implementation Plan: ccswitch — Multi-Account Claude Session Manager (Web)

**Feature dir:** `specs/001-ccswitch-web/`
**Spec:** [spec.md](./spec.md)
**Constitution:** `.specify/memory/constitution.md` **v1.1.0**
**Created:** 2026-06-19
**Branch:** `impl/ccswitch` (recommend branching the web work fresh from `master` after PR #1 merges)

## Status as of 2026-06-24

This plan is the **design-time roadmap**; the shipped system has outgrown it. Phases 0–2 functional
goals are met and the app runs in production (Tailscale-hosted, daemon-mode). Two architectural
pivots are **not** reflected in the original roadmap below — recorded here rather than rewriting it:

1. **Terminal → structured view.** The planned Phase 2 centerpiece (xterm.js raw terminal) was
   **never built**. The Phase 3 "deferred alternate" — an Agent-SDK stream-json structured view
   (message bubbles, tool cards, diffs) — became the **sole, primary** UI. Rendering is
   `SessionBody`→`TranscriptView`→`Turn` over a normalized event reducer (`web/src/session/reducer.ts`,
   `events.ts`); there is no terminal emulator in `web/`. **Phase 3 is therefore effectively complete,
   not deferred.**
2. **Daemon/IPC process separation, pulled forward from Phase 4.** Sessions are owned by an
   out-of-process daemon (`server/ipc/daemon.py`, `orchestrator_rpc.py`, `ccswitch-daemon.service`),
   so they survive web-process restarts — the "Phase 4 detached supervisor" mitigation shipped early.
   The bounded scrollback ring lives there (`daemon.py`, `deque(maxlen=256)`); the in-process path
   still uses an unbounded `asyncio.Queue` (`server/sessions/driver.py`) — a minor latent divergence.

Still deferred (Phase 4): app-level passkey/WebAuthn (scaffolded, `WEBAUTHN_ENABLED=false`),
rate-limited login, public-hosting hardening. The data model is faithful to `data-model.md`
(snake_case columns) with extra `Session` columns added for features built past the original spec
(`repo`, `branch`, `title`, `permission_mode`, `model`, `sub_agents`, `last_turn_at`, …).

Per-phase status is annotated inline in the roadmap below.

## Technical Context

| Area | Decision |
|------|----------|
| Language (backend) | Python 3.12 (async) |
| Backend framework | FastAPI + Uvicorn (ASGI; native WebSockets) |
| Data store (app) | SQLite via SQLAlchemy 2.x (async) + Alembic migrations; portable to Postgres |
| Account/credential store | Existing `ccswitch` file store (per-account, 0600), reused as a library |
| Session transport | One native `claude` per session in a PTY; browser ↔ PTY over WebSocket |
| Frontend | React + Vite + TypeScript, Tailwind + shadcn/ui, xterm.js, TanStack Query |
| Auth (v1) | Network-enforced identity (Tailscale) — verified via the Tailscale identity header/`whois`; no app login. App-level passkey/WebAuthn path is built but disabled until public exposure (Principle IV). |
| Token at rest | AES-GCM envelope encryption of replayable secrets; key from env/secret store |
| Real-time | Server push over WebSocket (terminal I/O + a multiplexed control/event channel) |
| Hosting (v1) | Loopback bind + Tailscale; env-config + Dockerfile kept Railway-portable |
| Testing | Backend: pytest + httpx + a fake-`claude` PTY stub; Frontend: Vitest + RTL + Playwright (mobile viewport) |
| Repo layout | Monorepo: `ccswitch/` (core lib) · `server/` (FastAPI) · `web/` (React) · `docs/` |

No `NEEDS CLARIFICATION` remain — the `/speckit-clarify` session resolved rotation semantics,
state ownership, auth posture, and token-at-rest; remaining items have documented defaults in the
spec's Clarifications.

## Constitution Check (v1.1.0)

| Principle | Gate | Status |
|-----------|------|--------|
| I. Black-Box Integration (no fork) | Sessions are unmodified `claude` processes; integration via CLI flags, `CLAUDE_CONFIG_DIR`, usage/refresh endpoints, PTY. No binary patching. | ✅ PASS |
| II. Account = Config Dir; Per-Process Isolation | Each session runs under its account's composed `CLAUDE_CONFIG_DIR`; Phase 0 fixes the share/isolate split + per-account daemon/session state. | ✅ PASS |
| III. Verify Against Reality | Phase 0 acceptance requires a live `claude -p` launch smoke; rotation/limit detection tested against real signals; binary facts already verified. | ✅ PASS |
| IV. Security First (exposure-conditional auth) | v1 gated by Tailscale identity (verified server-side); app-auth path built, required before public exposure; tokens envelope-encrypted; no secrets in logs. | ✅ PASS (per v1.1.0 amendment) |
| V. Test-Driven Quality Gates | TDD; ≥85% coverage on account-selection/credential/auth/rotation; full unit pass; lint/type-clean. | ✅ PASS (enforced in tasks) |
| VI. Single Source of Truth / No Dead Code | FR-025 split ownership (file store = accounts/creds/usage; DB = sessions/auth/metadata); Phase 0 deletes the over-sharing symlink logic in place. | ✅ PASS |
| VII. Modern Vetted Dependencies | FastAPI, SQLAlchemy, xterm.js, shadcn/ui, an auth lib — mature, maintained. | ✅ PASS |

No violations to justify. Re-evaluate after Phase 1 design.

## Project Structure

```
cc-account-switcher/
  ccswitch/                  # existing core library (accounts, usage, selector, configdir, creds)
                             #   + Phase 0 fixes (per-account .claude.json, share/isolate split)
  server/
    app.py                   # FastAPI app factory, middleware (auth, redaction), lifespan
    auth/                    # Tailscale-identity verifier + (disabled) passkey path; signed sessions
    sessions/                # SessionManager: PTY spawn/own, wait-for-idle rotation, scrollback ring
    bridge/                  # WebSocket ↔ PTY bridge; control/event channel
    usage/                   # per-account usage poller (push to subscribers)
    db/                      # SQLAlchemy models, Alembic migrations
    crypto/                  # envelope encryption for replayable secrets
    api/                     # REST routers: sessions, accounts, auth, health
  web/
    src/                     # React + Vite + TS; routes: dashboard, session(terminal), accounts
    src/components/          # shadcn/ui-based; SessionList, SessionCard, Terminal, UsageBadge, HelperKeyBar
  docs/                      # specs, plans, design
  tests/                     # backend pytest; web vitest/playwright under web/
```

## Phased Roadmap

Each phase is independently shippable and ends with passing tests (and, where it launches sessions,
a live smoke). Phases map to spec §Roadmap.

### Phase 0 — Config-dir launch correctness (foundation; fixes current breakage)  — ✅ SHIPPED
- Capture per-account `.claude.json` in `ccswitch add`; compose per-account `.claude.json`
  (account identity + shared allowlist: `mcpServers`, `projects`, `allowedTools`).
- Rewrite `reconcile_links` to a **share allowlist** (`plugins`, `commands`, `settings*.json`,
  `projects/`) and isolate runtime/identity state (`.credentials.json`, `.claude.json`, `daemon.*`,
  `sessions/`, `agents/`, `tasks/`, history) per account.
- **Acceptance:** a real `claude -p "ok"` runs under `cfg/<acct>`; `ccmax`-style resume works.

### Phase 1 — Backend session engine + API  — ✅ SHIPPED (+ daemon/IPC mode, pulled forward from Phase 4)
- `SessionManager`: PTY spawn under composed config dir; per-session scrollback ring (bounded);
  **wait-for-idle rotation** (detect idle = no PTY output for the idle threshold, then on a limit
  signal kill + `--resume` on next-best account, rebridge same session id, force repaint).
- Usage poller (per-account, push to subscribers; authoritative signal; 60s cache).
- `db/` models + migrations; envelope `crypto/`; account/usage glue via `ccswitch` core (FR-025).
- Auth middleware: Tailscale identity verification; passkey path scaffolded + disabled.
- REST (`sessions`/`accounts`/`auth`/`health`) + WebSocket bridge + control/event channel.
- **Acceptance:** API + WS tests green (fake-`claude` PTY stub); ≥85% coverage on critical paths.

### Phase 2 — Frontend (dashboard + terminal)  — ⚠️ SHIPPED EXCEPT xterm.js terminal (replaced by the Phase 3 structured view)
- Dashboard: cross-account session list (name · account badge · status · usage), usage strip,
  new/rename/close/switch; empty/first-run state; 44px targets; status by text+icon.
- Session view: xterm.js terminal over WS; mobile helper-key bar; full-screen on phone with a
  visible "back to list"; reconnect within 10s with recent scrollback (SC-008).
- Real-time updates via the WS event channel (status ≤3s, usage ≤60s).
- a11y: management UI WCAG 2.1 AA; terminal `aria-live` recent-output region + documented note.
- **Acceptance:** Vitest/RTL component tests; Playwright dashboard↔session + mobile-viewport pass.

### Phase 3 — Structured session view  — ✅ SHIPPED AS PRIMARY (no longer deferred; replaced the terminal)
Agent SDK stream-json structured (message/tool-card/diff) view. Originally scoped as an *alternate*
to the terminal; in practice it is the **only** session UI (`TranscriptView`/`Turn` + the
`web/src/session/` reducer). The xterm.js terminal from Phase 2 was not built.

### Phase 4 — Public-hosting hardening (deferred; partially pulled forward)
Enable the app-level passkey/WebAuthn auth, rate-limited login, audit log, Railway deploy.
**Done early:** the detached session supervisor (daemon/IPC mode) and the audit-event log.
**Still deferred:** passkey/WebAuthn (scaffolded, `WEBAUTHN_ENABLED=false`), rate-limited login,
public exposure.

## Complexity Tracking

| Risk | Mitigation |
|------|------------|
| PTY ownership vs server restart (sessions are Uvicorn children) | ✅ Resolved ahead of plan: sessions are owned by a detached daemon (`server/ipc/`), surviving web-process restarts; the original "v1 lives within server lifetime" limitation no longer applies. (research.md R3) |
| `.claude.json` account-identity coupling | Per-account capture + compose with shared non-identity allowlist (Phase 0). (research.md R1) |
| Limit detection in terminal (non-structured) mode | Primary = authoritative usage poll; secondary = transcript-tail (reused from `ccmax`). (research.md R4) |
| Wait-for-idle could stall rotation if a turn never ends | Idle threshold + a max-wait ceiling, then fall back to interrupt-then-rotate. (research.md R2) |
| Terminal accessibility vs WCAG AA | Management UI full AA; terminal gets `aria-live` recent-output + documented partial-conformance. (research.md R6) |

## Artifacts
- [research.md](./research.md) — resolved technical unknowns
- [data-model.md](./data-model.md) — entities + store ownership
- [contracts/](./contracts/) — REST + WebSocket contracts
- [quickstart.md](./quickstart.md) — run + end-to-end validation guide
