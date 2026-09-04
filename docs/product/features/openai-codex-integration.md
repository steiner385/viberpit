---
slug: openai-codex-integration
title: Native OpenAI / Codex Integration
state: discovery
links:
  specs:
    - docs/superpowers/specs/2026-09-04-native-openai-codex-integration-design.md
  project: https://github.com/users/steiner385/projects/26
---

# Native OpenAI / Codex Integration

**Problem** — ccswitch is meant to be a central hub for coding agents across accounts, models, and
projects, but its native session engine is currently Claude-specific. GitHub Copilot adds a billing
source through a translating proxy; it does not run Codex. Users with ChatGPT or OpenAI access
cannot add those accounts, discover Codex models, run native Codex threads, respond to Codex
approvals, see Codex quota, or route Codex work through the multi-host fleet.

**Target users** — operators who manage several coding projects and want Claude Code and Codex
sessions in one dashboard; ChatGPT subscribers with one or more named accounts; later, OpenAI API
customers who accept usage-based billing semantics.

**Desired outcome** — “Codex” is a first-class agent choice. A user adds an OpenAI account through
ChatGPT device login, launches a Codex session in a ccswitch-managed worktree, watches normalized
messages/commands/file changes/searches stream into the existing terminal, handles approvals,
interrupts or steers a turn, reconnects without losing history, and sees account-specific models,
reasoning choices, and quota. The refresh credential remains canonical on dobby; a credential
authority service, isolated under its own Unix identity and dedicated mTLS listener, supplies
short-lived access tokens to local app-servers on multiple runners
simultaneously, while the edge keeps vendor protocol connections out of the browser-facing path.

**Success metrics** — initial release gates: account login/reauth and secret-redaction tests pass;
interactive thread start/send/interrupt/approval/replay/restart/archive pass against a real opt-in
test account; every Codex session is stamped with engine, account, and owning host; model choices
come from `model/list`; all applicable rate-limit buckets render; account rotation remains inside
the selected engine; one account can serve simultaneous sessions on multiple runners; a Codex
process failure leaves Claude sessions and other runners healthy.

**Non-goals** — cross-engine automatic routing or live migration; Codex Cloud tasks; exposing
app-server over the network; parsing private Codex rollout files; raw chain-of-thought; Codex
plugin/skill/config-library management; arbitrary external-session adoption; treating current
Copilot support as native Codex; enabling every SDLC/backlog automation before an explicit Codex
compatibility audit; API-key authentication in v1.

**Notes** —

- Architecture uses two axes: `engine` (`claude_code` or `codex`) and account `provider`
  (`anthropic`, `copilot`, or `openai`). Copilot remains a Claude-engine account.
- OpenAI credentials use the same canonical-master principle as Claude: dobby owns the only refresh
  token and managed-auth home. The managed home is outside the runner-mounted NFS tree and belongs
  to a dedicated `ccswitch-auth` OS user; runners never refresh, read, or persist that credential.
- The edge syncs a minimal, revisioned enabled-account projection to the isolated authority over an
  admin-only mTLS route. Disabling or removing an account revokes issuance first and invalidates its
  runner managers; the authority never needs read access to the Claude credential store.
- OpenAI explicitly limits a managed `auth.json` to one machine or serialized job stream. Dobby
  therefore runs the sole managed-auth authority, while each runner uses a host-local app-server in
  experimental external-token mode. A runner receives only an in-memory access token and sends 401
  refresh requests to a dedicated authority listener that exposes no general daemon RPC methods.
- The same account may run concurrently on several runners. Codex's SQLite/WAL databases, logs,
  caches, and thread runtime state remain local to each runner; only normalized session data crosses
  the existing fleet boundary.
- External-token auth is experimental/unstable in Codex 0.153.2. V1 pins and probes the schema,
  fails closed on incompatibility, and never falls back to concurrent use of shared managed-auth
  files. A two-process read-only probe authenticated the same account, listed models, and read rate
  limits successfully without persisting runner credentials or starting a model turn. The owner
  explicitly accepted this dependency for v1 with those safeguards required.
- Browser and fleet transports remain provider-neutral. A new Codex driver translates app-server
  threads, turns, items, approvals, and history into the shared ccswitch session/event contracts.
- Models and reasoning efforts are runtime-discovered per account. The product does not hard-code
  whatever models happened to exist during development.
- Shared usage becomes a list of typed quota windows. OpenAI's multiple/model-specific buckets are
  preserved; an API key with unknown spend must not be ranked as unused quota.
- V1 is ChatGPT device-code auth plus interactive sessions. API-key auth is post-v1. Backlog/SDLC
  Codex automation parity is a required fast follow after origin-specific compatibility tests.
- Users explicitly select Claude Code or Codex. V1 Codex controls are model, reasoning effort, and
  permission profile. The data model supports multiple named OpenAI accounts immediately, while
  automatic OpenAI-account rotation is deferred.
- The owner accepted the version-pinned experimental external-token dependency with the design's
  mandatory compatibility, redaction, live-test, kill-switch, and fail-closed safeguards.
- PR #1088 configures Codex as a repository development agent; it does not implement this feature.

**Evidence** — current engine and normalized stream in `server/sessions/driver.py` and
`server/sessions/events.py`; orchestration/fleet routing in `server/sessions/orchestrator.py` and
`server/sessions/fleet_orchestrator.py`; account discovery/selection in `src/ccswitch/accounts.py`;
current Copilot translating-proxy behavior in `server/sessions/copilot_proxy.py`; official Codex
app-server/auth contracts at <https://learn.chatgpt.com/docs/app-server> and
<https://learn.chatgpt.com/docs/auth>. Full design:
`docs/superpowers/specs/2026-09-04-native-openai-codex-integration-design.md`.
