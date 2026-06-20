# cc-account-switcher (`ccswitch`)

Automatic Claude Max account rotation to stay within session/weekly usage limits
without manual intervention. See `docs/` for the design spec.

## Usage

```bash
ccswitch add <name> --email you@gmail.com   # capture an account (browser login)
ccswitch list                               # live usage + reset times (America/New_York)
ccrun -- -p "prompt"                         # headless: best account, auto-retry on limit
ccmax                                        # interactive: best account, auto switch + --resume on limit
ccswitch use [name]                          # print `export CLAUDE_CONFIG_DIR=…` for a chosen/best account
```

### Using with `claude agents` (and multiple concurrent sessions)

An account is a `CLAUDE_CONFIG_DIR`. An **agent view** (`claude agents`), every session it
dispatches, those sessions' sub-agents, and the supervising daemon all share the one
`CLAUDE_CONFIG_DIR` they were launched under — i.e. **one agent view = one account** (one set
of 5h/weekly limits). Views under different config dirs are fully isolated (separate daemons,
separate token lineages) and run concurrently without interference.

Pin a shell (and anything you launch from it) to a chosen account with `ccswitch use`:

```bash
# Most-headroom account:
eval "$(ccswitch use)"        # sets CLAUDE_CONFIG_DIR in this shell
claude agents                 # agent view + all dispatched sessions run on that account
# or: claude          (interactive)   /   claude -p "…"   (headless)

# A specific account:
eval "$(ccswitch use secondary)"
claude agents
```

**Parallel throughput across all three accounts** — run one agent view per account, each in
its own terminal/shell:

```bash
# terminal 1
eval "$(ccswitch use primary)";   claude agents
# terminal 2
eval "$(ccswitch use secondary)"; claude agents
# terminal 3
eval "$(ccswitch use kindash)";   claude agents
```

Notes:
- You cannot split one view's sub-agents across accounts — they inherit the view's account by
  design (usage is billed per account). Spread *views*, not sub-agents.
- `ccswitch use` (no name) picks the most-headroom account and places a short soft reservation,
  so running it back-to-back for several terminals spreads them across different accounts.
- `ccmax`/`ccrun` are for a *single* session that should auto-rotate on a limit; the agent view
  is long-lived and manages many sessions, so pin it with `ccswitch use` and re-pin a new view
  to a fresher account when `ccswitch list` shows the current one running low.

## ccswitch web

A browser-based multi-account Claude session manager. It pairs the CLI core above with a FastAPI
backend and a React/TypeScript web UI so you can launch and monitor Claude sessions from any device
on your Tailscale network — including a phone or tablet.

**What it is:**
- **Backend** — FastAPI server that spawns PTY-backed Claude sessions, multiplexes terminal I/O
  over WebSockets, tracks per-account usage, and auto-rotates accounts on a usage limit (waiting
  for the current turn to finish before switching).
- **Web UI** — React 19 + Tailwind dashboard: live account usage, session list with status
  (working/idle/limited/ended), inline xterm.js terminal, and a helper-key bar for touch screens.

### Run (local + Tailscale)

#### 1. Environment setup

```bash
# Required in production; optional in local dev (credentials will be stored unencrypted if unset)
export CCSWITCH_MASTER_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"

# Recommended — signs the HTTP session cookie
export CCSWITCH_SESSION_SECRET="$(python -c 'import secrets; print(secrets.token_hex(32))')"

# Restrict access to your own tailnet identity (comma-separated node names or email addresses)
export CCSWITCH_TAILNET_ALLOWLIST="you@example.com"

# Optional overrides (defaults shown)
# CCSWITCH_HOST=127.0.0.1
# CCSWITCH_PORT=8787
# CCSWITCH_DB_URL=sqlite+aiosqlite:///~/.claude-accounts/ccswitch.db
```

Or copy `.env.example` → `.env` and fill in values; the server loads it automatically.

#### 2. Install and build

```bash
# Backend deps
pip install -e ".[server]"

# Build the web UI (output goes to web/dist/, served by FastAPI)
cd web && npm install && npm run build && cd ..
```

#### 3. Start the server

```bash
uvicorn server.app:app --host 127.0.0.1 --port 8787
```

The server serves both the API and the compiled web assets.

#### 4. Expose to your phone via Tailscale

```bash
tailscale serve https / http://127.0.0.1:8787
```

Open the Tailscale HTTPS URL on your phone. The server never binds to a public interface.

#### Development (hot reload)

```bash
# Backend with auto-reload
uvicorn server.app:app --reload --host 127.0.0.1 --port 8787

# Frontend dev server (proxies /api → :8787)
cd web && npm run dev      # → http://localhost:5173
```

### SECURITY — trust model

> **Read this before exposing the server to any network.**

The server identifies callers from the `Tailscale-User-Login` header injected by `tailscale serve`.
This is **only safe when the server is bound to loopback (`127.0.0.1`) and fronted by
`tailscale serve`**. If you bind to `0.0.0.0` or skip `tailscale serve`, the header can be
spoofed and the server trusts all callers unconditionally.

- **Set `CCSWITCH_TAILNET_ALLOWLIST`** to your own tailnet identity (e.g. `you@gmail.com`). An
  empty allowlist permits all authenticated tailnet nodes — only safe on a single-person tailnet.
- **Refresh tokens are encrypted at rest** using AES-256-GCM with a key derived from
  `CCSWITCH_MASTER_KEY` (via keyring or env). Without `CCSWITCH_MASTER_KEY` in production,
  credentials are stored in plaintext at `~/.claude-accounts/store/` (mode 0600).
- **Composed config-dir credentials** (the per-account `CLAUDE_CONFIG_DIR` assembled by
  `compose()`) are plaintext on disk at mode 0600. They are the only unencrypted secret at rest
  and are re-composed from the encrypted source on each session launch.
- **App-level passkey/WebAuthn auth** is implemented but disabled (`CCSWITCH_WEBAUTHN_ENABLED`).
  It is the required gate before exposing the server to any network that is not your personal
  tailnet. This path is deferred to a future release (P4).

### Deploy (Railway — deferred, app-level auth must be enabled first)

> **Not recommended until the passkey auth gate is enabled.** The steps below are provided for
> reference so the Railway wiring is documented when that hardening ships.

Required environment variables in the Railway service:

| Variable | Description |
|----------|-------------|
| `CCSWITCH_MASTER_KEY` | 64-hex master encryption key — set as a Railway Secret |
| `CCSWITCH_SESSION_SECRET` | 64-hex cookie signing key — set as a Railway Secret |
| `CCSWITCH_TAILNET_ALLOWLIST` | Your tailnet identity, or leave empty only for personal single-user deploys |
| `DATABASE_URL` / `CCSWITCH_DB_URL` | Railway managed PostgreSQL URL (set automatically by the Postgres plugin) |
| `CCSWITCH_HOST` | `0.0.0.0` (Railway requires binding to all interfaces) |
| `CCSWITCH_PORT` | `8787` (or Railway's `$PORT`) |

The Dockerfile is multi-stage: it builds the web assets in a Node stage and copies `web/dist`
into the Python stage, so `railway up` produces a single image serving both API and static files.

---

## Running the web backend (dev)

Install server dependencies:
```bash
pip install -e ".[server]"
```

Set required env vars (see `.env.example`), then start the dev server:
```bash
uvicorn server.app:app --reload --host 127.0.0.1 --port 8787
```

Or copy and fill in `.env.example`:
```bash
cp .env.example .env
# edit .env
uvicorn server.app:app --reload
```
