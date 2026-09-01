---
slug: multi-account-management
title: Multi-Account Management
state: live
links:
  code:
    - src/ccswitch/accounts.py
    - src/ccswitch/account_names.py
    - src/ccswitch/creds.py
    - src/ccswitch/crypto.py
    - src/ccswitch/configdir.py
    - src/ccswitch/ledger.py
    - server/api/accounts.py
  prs: ['#352', '#345', '#685', '#873', '#627', '#558', '#565', '#878']
---

# Multi-Account Management

**Problem** — A single Claude Max account hits session and weekly usage limits quickly. Users
who juggle several accounts by hand have to manually track which `CLAUDE_CONFIG_DIR` each shell
points at, copy credentials between machines, and remember which account is logged in — an
error-prone, credential-leaking chore.

**Target users** — Individuals running multiple Claude Max (and other provider) subscriptions who
want them managed as one pool: power users, the project owner running parallel agent views, and
anyone hosting a shared session daemon across a fleet.

**Desired outcome** — Each account is a first-class, named entity backed by its own
`CLAUDE_CONFIG_DIR`, with strict per-process isolation (constitution Principle II). Users capture
an account once via browser login (`ccswitch add <name>`), then list, nickname, enable/disable,
and remove accounts through the CLI or web Settings tab. Credentials live in one encrypted store,
never in the responses the API returns.

**Success metrics** — (unknown — needs owner input). Reasonable candidates grounded in the code:
number of accounts a user can register and switch between without manual `CLAUDE_CONFIG_DIR`
bookkeeping; zero token/credential material leaking into API responses (enforced at
`server/api/accounts.py`, "Response NEVER includes token/credential material"); zero cross-account
credential bleed.

**Non-goals** — Forking or patching the `claude` binary (Principle I — accounts are activated only
by pointing the stock binary at a config dir). Splitting one agent view's sub-agents across
accounts (usage is billed per account, so a view inherits one account by design — README).

**Notes** —
- An account == a `CLAUDE_CONFIG_DIR`. `add_account` captures credentials by running login against
  a temp capture dir, then storing them; `compose()`/`configdir.py` assembles the per-account
  config dir from the store at launch, overlaying shareable `.claude.json` keys
  (`mcpServers`, `projects`, `allowedTools`) from `store/claude.shared.json`.
- Credential store: `.credentials.json` is split into `claudeAiOauth` + `mcpOAuth`
  (`creds.split_credentials`); files are written atomically at mode 0600
  (`creds.write_json_secure`). `crypto.py` adds AES-256-GCM envelope encryption of the
  `refreshToken` field only (`accessToken` stays plaintext); the master key comes from
  `CCSWITCH_MASTER_KEY` or the GNOME keyring, and import never raises if crypto is unavailable
  (falls back to plaintext with a one-time warning).
- Account names are validated (`account_names.py`) so a stray/empty name can't collapse a
  per-account path onto the shared store root — the fix for the "phantom empty-named account"
  incident (#873).
- Per-account state (nickname, enabled flag, weekly reserve, allow-extra-usage, account defaults)
  lives in the flock-guarded JSON ledger; `remove_account` drops both the store slot and the
  ledger entry (#627).

**Evidence** — `add_account`/`list_accounts`/`remove_account`/`set_nickname` in
`src/ccswitch/accounts.py`; validation in `src/ccswitch/account_names.py`; credential split and
secure write in `src/ccswitch/creds.py`; envelope encryption in `src/ccswitch/crypto.py`; REST
surface (list/refresh/delete/patch, no tokens) in `server/api/accounts.py`. PRs: #352 (encrypted
setup-token capture), #345 (daemon-readable encrypted-file store), #685 (enable/disable without
deleting), #873 (phantom empty-named account fix), #627 (remove_account drops ledger entry), #565
(consolidated Accounts settings tab), #878 (per-account extra-usage opt-in), #558 (per-account
weekly reserve).
