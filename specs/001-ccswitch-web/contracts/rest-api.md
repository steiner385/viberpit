# Contract: REST API

All routes require a verified identity (R5). JSON request/response. Errors:
`{ "error": { "code": string, "message": string } }`. **No response ever contains token/credential
material** (FR-023). Base path `/api`.

## Auth / identity
- `GET /api/auth/me` → `{ identity: string, authMode: "tailscale" | "passkey" }` — current caller.
- `POST /api/auth/logout` → `204` — revoke the current AppSession.
- (Phase 4, disabled in v1) `POST /api/auth/webauthn/register|authenticate` — passkey flows.

## Accounts
- `GET /api/accounts` → `[{ name, status, usage: { session: {pct, resetsAt}, weekly: {pct, resetsAt} } }]`
  — refreshes live usage before returning (FR-006/007). Never includes tokens.
- `POST /api/accounts/{name}/refresh` → the single account's fresh usage.
- `DELETE /api/accounts/{name}` → `204` — remove an account (FR-004).
- `POST /api/accounts/{name}/relogin` → `202` — trigger host-side re-login (registration is
  host-side in v1; the web app initiates and reports status).

> Registration of a *new* account (browser/federated login) is performed host-side (`ccswitch add`)
> in v1 (spec Assumptions); a future `POST /api/accounts` remote-OAuth flow is out of scope.

## Sessions
- `GET /api/sessions` → `[{ id, name, accountName, status, createdAt, updatedAt }]` (FR-016).
- `POST /api/sessions` `{ name?, account?, initialPrompt? }` → `201 { id, ... }` — create on the
  most-headroom account unless `account` given; honors the reservation (FR-008/009a). `409` with
  `earliestReset` if all accounts are at ceiling (FR-010).
- `PATCH /api/sessions/{id}` `{ name }` → `200` — rename (FR-012).
- `DELETE /api/sessions/{id}` → `204` — close/terminate (FR-012).
- `GET /api/sessions/{id}` → full session incl. recent scrollback metadata for re-entry.

## Health
- `GET /api/health` → `{ ok: true, version }` (identity still required).

## Status codes
`200/201/204` success · `401` no/!valid identity · `403` identity not allowlisted · `404` unknown
account/session · `409` all-accounts-limited (`earliestReset`) · `5xx` server error (message
redacted of secrets).
