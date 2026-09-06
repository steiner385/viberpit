---
slug: openai-codex-integration
title: Native OpenAI / Codex Integration
state: delivering
links:
  specs:
    - docs/superpowers/specs/2026-09-04-native-openai-codex-integration-design.md
  project: https://github.com/users/steiner385/projects/26
  acceptance: docs/superpowers/spikes/2026-09-04-codex-interactive-acceptance.md
---

> **State.** Phases 1–6 and Phase 7 Tasks 7.1–7.3 have landed behind fail-closed flags:
> `CCSWITCH_CODEX_ENABLED` and `CCSWITCH_CODEX_EXTERNAL_AUTH_ENABLED` are both
> `false` everywhere, and `CCSWITCH_CODEX_AUTOMATIONS_ENABLED` stays false: every
> automation origin is still `claude_only`, blocked on the sandbox-egress finding
> below rather than on the prompt contract. This becomes `live` only when Part 2
> of the acceptance record —
> the real-boundary runs on dobby and rocky — passes and the owner enables the
> flags. Operator procedures: `docs/runbooks/codex-credential-authority.md`.

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
- **Automation engine policy (Phase 7).** Every automation origin declares an audited engine
  ceiling — `claude_only`, `codex_allowed`, or `codex_only` — in code
  (`server/automations/engine_policy.py`). There is no implicit "any": an origin that declares
  nothing is `claude_only`. An origin whose ceiling admits more than one engine gains an `engine`
  selector in its ordinary automation config; an origin that does not offers no such field, so the
  API rejects `{"engine": "codex"}` as an unknown key, and the one automation spawn helper clamps
  again at launch. The reason unattended sessions get a stricter gate than interactive ones: they
  run in `full-auto` with git/gh/pytest and nobody watching, so an unverified prompt contract or
  completion-marker mismatch is not a degraded experience. `CCSWITCH_CODEX_AUTOMATIONS_ENABLED`
  (default `false`) is the global kill switch — it forces every `codex_allowed` origin back to
  Claude without an operator unwinding each config, and it is separate from `CCSWITCH_CODEX_ENABLED`
  so enabling Codex for humans does not enable it for control loops. **Every origin is still
  `claude_only`;** widening one is gated on that origin's Codex acceptance suite passing
  (Tasks 7.2 and 7.3).
- **Automation parity is blocked on the Codex sandbox, not on the prompt contract (Phase 7, Tasks
  7.2/7.3).** The offline half of both acceptance gates passes — the backlog and SDLC seed prompts
  carry their whole contract with no Claude-only syntax, the loops handle a refused launch without
  stranding a claim, and permission tiers map without weakening the propose-only human gates
  (`server/tests/test_backlog_runner_codex_acceptance.py`,
  `server/tests/test_sdlc_codex_acceptance.py`). The blocker is elsewhere: an automation's only
  output is a pushed branch and a PR, and Codex's sandbox is network-isolated in every mode except
  `bypassPermissions`. `full-auto` — the automation default — maps to `workspace-write`, whose
  `networkAccess` the pinned app-server schema defaults to `false` with no settable override on
  `thread/start` (Codex's own config file has the knob, but config management is post-v1). So an
  unattended Codex run would do all the work and fail at `git push`. The launch path now refuses
  that pair up front with an operator-legible reason rather than spending a session to discover it.
  Enabling automation parity therefore needs a separate, explicit decision about the authority an
  unattended Codex session runs with — not just a ceiling widening.
- **Owner-authorized network opt-in (#1263).** `CCSWITCH_CODEX_UNATTENDED_NETWORK_OPT_IN` (default
  `false`) is that separate decision. It changes nothing by itself: with it unset, an unattended
  Codex launch is refused exactly as above. Set, it escalates ONLY the automation's own `full-auto`
  fallback — never an operator's explicit `permission_mode` or an account-pinned default — to
  `bypassPermissions`, the one profile whose sandbox actually permits network egress, so the
  automation's own contract (push a branch, open a PR) can complete instead of failing at the last
  step. The escalation is recorded as an `engine_network_opt_in_escalated` activity event, never
  silent (`server/automations/engine_policy.py:escalate_for_codex_network_opt_in`). This does not
  claim a live end-to-end push/PR has been proven against a real Codex binary in a disposable repo —
  that verification is still outstanding and should precede flipping the flag in any environment
  that matters.
- Users explicitly select Claude Code or Codex. V1 Codex controls are model, reasoning effort, and
  permission profile. The data model supports multiple named OpenAI accounts immediately, while
  automatic OpenAI-account rotation is deferred.
- **Command palette (#1286).** A session's palette has two halves, and which half an entry belongs
  to is on the wire (`source`). *App-owned* commands are ccswitch's own orchestrator operations
  (`/help`, `/context`, `/clear`, `/model`, `/account`), declared once in
  `server/sessions/commands/catalog.py` with the engine capability each needs, so they render on any
  engine that can actually perform them. *Vendor* commands are whatever the engine publishes about
  itself: the Claude CLI's `get_server_info()` list, and for Codex the app-server's `skills/list`.
  A Codex skill is dispatched through the pinned `SkillUserInput` variant of `turn/start` — by name
  AND path — never by writing `/name` into the prompt, which the app-server does not parse and would
  hand the model as literal text. Listing and invoking skills is not the "plugin/skill management"
  named in Non-goals: nothing here installs, edits, enables, or scopes a skill.
- The owner accepted the version-pinned experimental external-token dependency with the design's
  mandatory compatibility, redaction, live-test, kill-switch, and fail-closed safeguards.
- PR #1088 configures Codex as a repository development agent; it does not implement this feature.

**Evidence** — current engine and normalized stream in `server/sessions/claude_driver.py` and
`server/sessions/events.py`; orchestration/fleet routing in `server/sessions/orchestrator.py` and
`server/sessions/fleet_orchestrator.py`; account discovery/selection in `src/ccswitch/accounts.py`;
current Copilot translating-proxy behavior in `server/sessions/copilot_proxy.py`; official Codex
app-server/auth contracts at <https://learn.chatgpt.com/docs/app-server> and
<https://learn.chatgpt.com/docs/auth>. Full design:
`docs/superpowers/specs/2026-09-04-native-openai-codex-integration-design.md`.
