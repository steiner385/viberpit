---
slug: copilot-account-support
title: GitHub Copilot Account Support
state: live
links:
  code:
    - src/ccswitch/copilot.py
    - server/sessions/copilot_auth.py
    - server/sessions/copilot_proxy.py
    - server/api/accounts.py
    - scripts/fetch-copilot-proxy.sh
  prs: ['#515', '#516', '#540', '#659']
---

# GitHub Copilot Account Support

**Problem** — Some users have a GitHub Copilot Pro+ subscription but not (or in addition to) a
Claude Max subscription, and want to fold that quota into the same multi-account session pool. The
Copilot API speaks a different protocol from Anthropic's and meters usage as premium requests, not
session/weekly reset windows — so it can't be dropped into the rotation logic as-is.

**Target users** — Users with a GitHub Copilot Pro+ account who want to experiment with routing
Claude sessions through it as an extra account in the pool. This is a personal-experimentation
feature, not a supported production path.

**Desired outcome** — A Copilot account is added and managed like any other account (Add Account
dialog → toggle to "GitHub Copilot" → GitHub device-flow login, no browser required), and once
added it participates in usage-aware rotation transparently: its premium-request quota is mapped
onto the same `Usage` dataclass so the provider-agnostic selector ranks it unchanged. Sessions on it
launch against a supervised localhost translating proxy rather than via `CLAUDE_CONFIG_DIR` OAuth.

**Success metrics** — (unknown — needs owner input). Grounded candidates: a Copilot account can be
added via device-flow and run sessions end-to-end; its usage ranks in rotation alongside Anthropic
accounts without special-casing the selector; proxy health-gate and idle-teardown behave (health via
`GET /usage`, idle teardown after `CCSWITCH_COPILOT_IDLE_GRACE_S`).

**Non-goals** — Being a supported/ToS-clean path: routing Claude sessions through the Copilot API may
violate GitHub's or Anthropic's terms (see Notes). Anthropic-style suggestions/auto-summaries on
Copilot sessions (deliberately disabled). Session-reset-token usage semantics — Copilot meters a
single ~monthly premium allowance instead.

**Notes** —
- **Experimental, ToS caveat.** State is `live` (the code path ships) but the capability is gated:
  `CCSWITCH_ENABLE_COPILOT=true` must be set and the `whtsky/copilot2api` proxy binary installed
  (`bash scripts/fetch-copilot-proxy.sh`, or `CCSWITCH_COPILOT_PROXY_BIN=/path`). Routing Claude
  through the Copilot API may violate GitHub's ToS or Anthropic's usage policies — provided for
  personal experimentation; users own compliance (CLAUDE.md § Copilot).
- Provider constant + usage mapping live in `src/ccswitch/copilot.py` (single source of truth):
  `usage_from_proxy` projects the premium-request quota onto BOTH session and weekly dimensions so
  the selector's binding-dimension logic works unchanged; a garbage payload returns None (degrades
  to "usage unknown", not a false ceiling).
- Add flow: GitHub device-flow via `CopilotAuthManager` (`server/sessions/copilot_auth.py`) — spawns
  `copilot2api -token-dir <dir>` in login mode, scrapes the `github.com/login/device` URL + code
  (accumulated across separate output lines, #516), advances to `authorized` when
  `credentials.json` appears. Wired to `POST /api/accounts/add/copilot` (202).
- Runtime: `CopilotProxyManager` (`server/sessions/copilot_proxy.py`) supervises one proxy child per
  Copilot account (modeled on x11manager): resolved via override/vendored-path/PATH, health-gated,
  liveness-graced, idle ref-count torn down, orphan-reaped on startup. `ENABLE_TOOL_SEARCH=true` is
  injected because Claude Code disables MCP tool search for non-first-party base URLs.
- Suggestions/auto-summaries are gated off for Copilot sessions (#659); the usage poller covers
  Copilot fetches through the same ledger-backed backoff (#540).

**Evidence** — Provider constant, `usage_from_proxy`, launch-env builder in `src/ccswitch/copilot.py`;
device-flow scraping state machine in `server/sessions/copilot_auth.py`; per-account proxy
supervision/resolution in `server/sessions/copilot_proxy.py`; add-copilot device-flow routes in
`server/api/accounts.py`; provisioning in `scripts/fetch-copilot-proxy.sh`. PRs: #515 (add GitHub
Copilot Pro+ as an account type), #516 (accumulate URI+code across device-flow lines), #540 (poller
covers copilot usage fetches), #659 (gate suggestions to non-Copilot sessions).
