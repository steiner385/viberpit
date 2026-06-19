# Data Model: ccswitch-web

Per **FR-025 (single state ownership)**, state lives in exactly one of two stores:

- **Account store** (existing `ccswitch` file store, per-account, 0600) — owns account credentials,
  identity, and the usage cache. **Never duplicated into the DB.**
- **App store** (SQLite via SQLAlchemy) — owns web sessions, login/identity sessions, and
  display/UI metadata.

Secrets (tokens) never enter the DB; the DB references accounts by stable `name`.

## Account store (file-backed; owned by `ccswitch` core)

### Account
| Field | Type | Notes |
|-------|------|-------|
| name | string (PK) | user-chosen label, unique |
| claudeAiOauth | object | accessToken, refreshToken (**envelope-encrypted at rest**, R9), expiresAt, scopes, subscriptionType |
| status | enum | `ok` / `limited` / `needs-relogin` / `not-max` (derived) |
Files: `store/<name>.cred.json` (0600), `store/<name>.claude.json` (per-account `.claude.json`, R1),
`store/claude.shared.json` (shared allowlist), `store/mcp.shared.json` (shared MCP), `ledger.json`.

### UsageWindow (cached in `ledger.json`)
| Field | Type | Notes |
|-------|------|-------|
| account | string (FK→Account.name) | |
| kind | enum | `session` (5h) / `weekly` |
| utilizationPct | float | 0–100, authoritative |
| resetsAt | datetime \| null | null = no active window (0% used) |
| fetchedAt | datetime | cache freshness (≤60s, R7) |

## App store (SQLite)

### Session
| Field | Type | Notes |
|-------|------|-------|
| id | uuid (PK) | |
| name | string | user-chosen, renamable (FR-012) |
| accountName | string (FK→Account.name) | current account; updated on rotation (FR-013) |
| claudeSessionId | string | for `--resume` across rotation |
| status | enum | `working` / `idle` / `limited` / `ended` (R2; transitions in spec) |
| pid | int \| null | owning PTY process (within server lifetime, R3) |
| cwd | string | working directory |
| createdAt / updatedAt | datetime | |
Transient (not persisted): the bounded scrollback ring (in-memory, R3) and the live PTY handle.

### AppSession (login/identity)
| Field | Type | Notes |
|-------|------|-------|
| id | uuid (PK) | opaque, server-side |
| identity | string | verified Tailscale identity (R5) |
| createdAt / expiresAt | datetime | idle + absolute expiry |
| revokedAt | datetime \| null | server-side revocation |

### AuditEvent (write-only; security forensic surface)
| Field | Type | Notes |
|-------|------|-------|
| id | uuid (PK) | |
| at | datetime | |
| type | enum | `auth.ok` / `auth.deny` / `account.add` / `account.remove` / `session.create` / `session.close` / `session.rotate` / `token.refresh` |
| identity | string | actor |
| detail | json | **never contains token/credential material** (FR-023) |

## Relationships
- `Account 1—N UsageWindow` (session + weekly per account).
- `Account 1—N Session` (a session runs on one account at a time; rotation changes `accountName`).
- `AppSession`/`AuditEvent` reference `identity`, independent of accounts.

## Validation / invariants
- `Account.name` unique; a `Session` always references an existing account.
- No token/secret value persisted in the App store or any `AuditEvent.detail`.
- Refresh tokens encrypted at rest (R9); files 0600; store dir 0700.
- `Session.status = limited` ⇒ a rotation is pending or no account has headroom.
- Selection excludes accounts with `status ∈ {limited, needs-relogin, not-max}` and honors the
  soft reservation (R8).
