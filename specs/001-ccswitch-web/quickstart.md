# Quickstart & Validation: ccswitch-web

Run the platform locally and validate the spec's success criteria end-to-end. Implementation
details live in `tasks.md`/code; this is a run + verification guide.

## Prerequisites
- The Claude Code CLI installed; at least two Claude Max accounts registered host-side
  (`ccswitch add <name>` — see project README). Phase 0 must be merged (sessions can launch).
- Python 3.12 + the backend deps installed (`server/`); Node + the frontend deps (`web/`).
- `CCSWITCH_MASTER_KEY` set in the environment (envelope-encryption key, R9).
- Tailscale running on the host and the phone on the same tailnet (v1 access path, R5).

## Run (local / Tailscale)
```bash
# backend (binds loopback; fronted by `tailscale serve` for the phone)
CCSWITCH_MASTER_KEY=... python -m server.app          # serves API + built web assets
tailscale serve https / http://127.0.0.1:8000          # expose to the tailnet only
# dev frontend (optional, hot reload): cd web && npm run dev
```
Open the tailnet URL on the phone.

## Validation scenarios (map to Success Criteria)

1. **Dashboard + usage (SC-001, FR-006/016)** — open the URL; confirm each account shows live
   session + weekly usage with reset times in local time, and the session list renders. Target:
   interacting with a session on the best account in <15s.
2. **Create + interact (FR-008/012/018)** — create a session; confirm it lands on the
   most-headroom account and opens a working terminal; type a prompt and see output.
3. **Auto-rotation, wait-for-idle (FR-013, SC-002, NFR-001)** — drive the focused account near its
   limit (or inject a limit signal via the fake-`claude` stub in tests); confirm rotation waits for
   the current turn to finish, then the session continues on another account within 30s with full
   scrollback/history and the account badge updates. Target: ≥95% of rotations lossless.
4. **Concurrency (FR-009/009a)** — create two sessions near-simultaneously; confirm they land on
   different accounts when headroom allows.
5. **Reconnect (SC-008)** — drop the phone's connection mid-session, restore it; confirm reattach to
   the same session with recent scrollback within 10s.
6. **All-limited (FR-010)** — with all accounts at ceiling, confirm `new session` reports the
   earliest reset in local time (no silent failure).
7. **Auth (FR-021, SC-006)** — hit any route/WS from off-tailnet (or without verified identity);
   confirm it is rejected. On-tailnet identity is accepted.
8. **Mobile/a11y (SC-005, NFR-004)** — on a phone viewport: all actions reachable without
   horizontal scroll; helper-key bar present in the terminal; status by text+icon; keyboard
   focus returns to the list on close; recent terminal output exposed to a screen reader.
9. **Secrets hygiene (FR-023, NFR-003)** — grep logs/responses: no token material; confirm
   refresh tokens are ciphertext at rest (0600).

## Automated equivalents
- Backend: `pytest` (API/WS via httpx + the fake-`claude` PTY stub drives scenarios 2–6, 8-data).
- Frontend: `npm run test` (Vitest/RTL) + `npm run test:e2e` (Playwright, mobile viewport) for 1–8.
- Scenario 3's real-claude path is also covered by a live smoke (Constitution Principle III).

## T060 live-run validation log (2026-06-20)

Ran the real server (`uvicorn server.app:app`) against the 3 real accounts.

**Verified live (PASS):** server serves SPA+API; `GET /api/health` 200 and 401 without identity (auth gate live); `GET /api/accounts` 200 with usage shape and NO token material in the response (FR-023 live); `needs-reauth` surfaced gracefully (no crash).

**Findings (Principle III):**
1. Headless/service server has no keyring/D-Bus access → cannot decrypt via keyring. Set `CCSWITCH_MASTER_KEY` in the server env (Railway-ready path).
2. Refresh-token rotation desync: Anthropic refresh tokens are single-use; concurrent refreshers (CLI + server) over a long session desynced the stored tokens → all three `needs-reauth`. Live session creation blocked until accounts are re-added (`ccswitch add`). Follow-up: serialize refresh + always persist the rotated token.

**Not yet verified live:** create + stream a real claude session (blocked by #2; retry after re-add).
