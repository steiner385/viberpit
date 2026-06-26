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

## Cross-host daemon (mTLS)

For multi-host deployments (P4, public hosting), the edge process and session daemon communicate over
mTLS. This section covers certificate generation and configuration.

### Overview

- The daemon (server) binds to an HTTPS endpoint and presents a server certificate to clients.
- The edge (client) presents a client certificate and verifies the daemon's certificate against a CA.
- Both sides verify peer certificates against the same root CA.
- **Hostname verification footgun:** The server cert's SAN (Subject Alternate Name) **MUST match the
  hostname or IP that the edge uses to reach the daemon** — a mismatch will fail verification with
  "certificate does not match the server name." Use the reachable FQDN or IP in the SAN, not the
  daemon's internal hostname.
- An `https://` endpoint with missing TLS files fails loud at startup; env vars are optional (unset
  them to use the default Unix socket on the same host).
- **v2 (deferred):** CN-based allowlist, certificate rotation, and expiry management.

### Generate certificates

Use this OpenSSL recipe to generate a CA and certificates for your deployment:

```bash
# CA (valid 10 years)
openssl req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out ca.pem \
  -subj "/CN=ccswitch-ca" -days 3650

# Server cert (SAN MUST match how the edge reaches the daemon)
openssl req -newkey rsa:2048 -nodes -keyout server.key -out server.csr -subj "/CN=sessiond"
openssl x509 -req -in server.csr -CA ca.pem -CAkey ca.key -CAcreateserial -out server.pem \
  -days 825 -extfile <(printf "subjectAltName=DNS:daemon.example.ts.net,IP:100.x.y.z")

# Client cert
openssl req -newkey rsa:2048 -nodes -keyout client.key -out client.csr -subj "/CN=edge"
openssl x509 -req -in client.csr -CA ca.pem -CAkey ca.key -CAcreateserial -out client.pem -days 825
```

Replace `daemon.example.ts.net` and `100.x.y.z` with the daemon's reachable hostname and/or IP.
The server cert's `subjectAltName` must match exactly what the edge will use in the endpoint URL.

### Configuration

Set environment variables on each host:

**Daemon (server) host:**
```bash
CCSWITCH_SESSIOND_ENDPOINT=https://daemon.example.ts.net:9443
CCSWITCH_SESSIOND_TLS_CERT=/etc/ccswitch/tls/server.pem
CCSWITCH_SESSIOND_TLS_KEY=/etc/ccswitch/tls/server.key
CCSWITCH_SESSIOND_TLS_CA=/etc/ccswitch/tls/ca.pem
```

**Edge (client) host:**
```bash
CCSWITCH_SESSIOND_ENDPOINT=https://daemon.example.ts.net:9443
CCSWITCH_SESSIOND_CLIENT_CERT=/etc/ccswitch/tls/client.pem
CCSWITCH_SESSIOND_CLIENT_KEY=/etc/ccswitch/tls/client.key
CCSWITCH_SESSIOND_CLIENT_CA=/etc/ccswitch/tls/ca.pem
```

Distribute the CA cert to both hosts; distribute the server cert + key to the daemon host, and the
client cert + key to the edge host. Copy `.env.example` to `.env` and fill in the paths.

### Restricting which client certs are accepted (optional)

By default any client cert signed by `CCSWITCH_SESSIOND_TLS_CA` is accepted. To restrict to
specific client identities, set on the **daemon**:

    CCSWITCH_SESSIOND_CLIENT_ALLOWLIST=edge.internal,edge2.internal

Values are matched **exactly** against the client cert's Common Name (CN) and Subject
Alternative Names (SAN). Modern certs carry identity in the SAN — issue client certs whose
SAN matches a listed value.

Troubleshooting: a rejected client sees only a generic TLS handshake error on the **edge**
side. The authoritative diagnostic is the **daemon** log line
`daemon mTLS: rejected client identities [...] (not in allowlist)`.

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

### T060 RESOLVED — full live pass (2026-06-20)

After re-adding accounts and two core fixes, the full end-to-end live run PASSED 8/8:
server+health, 401 gate, **accounts 3/3 ok**, no token leak, **POST /api/sessions 201**
(real session on `primary`), **WS streamed real claude output**, **model produced the
expected response (LIVEOK)**, DELETE 204. The web app creates and streams a real Claude
session across accounts.

Two fixes the live run forced (Principle III):
- **Serialize per-account token refresh** — the server's usage poller + API requests
  refreshed the same single-use refresh token concurrently → desync. Now locked + re-read.
- **Composed `.claude.json` marked onboarded** — interactive `claude` showed the login
  chooser (the `claude auth login` capture omits `hasCompletedOnboarding`, which `-p`
  ignores but interactive honors); `compose` now sets it.
- Operational: a headless server must get `CCSWITCH_MASTER_KEY` via env (no keyring/D-Bus).
