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
