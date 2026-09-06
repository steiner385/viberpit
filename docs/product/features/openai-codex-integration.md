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
> `false` everywhere, and `CCSWITCH_CODEX_AUTOMATIONS_ENABLED` stays false. The two
> SDLC origins and the two PM session origins are `codex_allowed` on live-boundary
> evidence (#1265/#1266, #1268/#1269). `backlog-runner` and `pr-doctor` are also
> `codex_allowed` (#1264, #1267) on their offline acceptance suites alone — a
> live Codex session landing a real PR / restoring or consolidating a real PR
> remains unverified there, an owner-accepted gap in both cases.
> Every other automation origin is still `claude_only`. In every case the global
> kill switch, each automation's own `engine` config opt-in, and
> `CCSWITCH_CODEX_UNATTENDED_NETWORK_OPT_IN` still default off, so none of this
> starts a live unattended Codex launch by itself. This becomes `live` only when
> Part 2 of the acceptance record —
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
  so enabling Codex for humans does not enable it for control loops. **`backlog-runner` (#1264), the
  two SDLC origins (`sdlc-intake`, `sdlc-synthesizer`, #1265/#1266), the two PM session origins
  (`pm-onboard`, `pm-classify`, #1268/#1269), and `pr-doctor` (#1267) are `codex_allowed`; every
  other origin is still `claude_only`** — widening one is gated on that origin's Codex acceptance suite passing
  (Tasks 7.2 and 7.3), and the current map is asserted whole in
  `server/tests/test_automation_engine_policy.py`. A ceiling is only half the mechanism: the
  operator's opt-in is a key in the origin's ordinary automation config row, so an origin that is
  not in the registry has nowhere to store one and is `claude_only` by *default* rather than by
  decision. That is why the PM origins had to be registered before they could be widened (#1268).
- **Automation parity was blocked on the Codex sandbox, not on the prompt contract (Phase 7, Tasks
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
  silent (`server/automations/engine_policy.py:escalate_for_codex_network_opt_in`). The push half of
  that escalation is now proven live (see the SDLC bullet below); the `gh pr create` half is not,
  and it is the one step of the contract that is plain shell and identical on either engine.
- **SDLC origins enabled (#1265 intake, #1266 synthesizer).** Both SDLC ceilings are now
  `codex_allowed`, on the evidence of an opt-in initiative driven intent → design → plan on the real
  `codex` binary (`codex-cli 0.153.2` — the same version the app-server contract is pinned to). The
  harness is committed as `server/tests/test_sdlc_codex_live_artifact.py` and is opt-in
  (`CCSWITCH_TEST_CODEX_LIVE=1`), so CI never spends OpenAI quota on it. It runs each stage's **real**
  prompt from `server/sdlc/prompts.py` in a throwaway repository whose `origin` is a local *bare*
  repo, in the `danger-full-access` profile #1263's opt-in escalates a `full-auto` automation launch
  to. Each stage was verified to produce exactly its own artifact at
  `docs/sdlc/<slug>/<kind>.md`, branch off `origin/<default>`, carry
  `Co-Authored-By: Codex <noreply@openai.com>` (and never Claude's trailer), leave its upstream
  artifacts byte-identical, and push the branch. The human gate is structural rather than asserted:
  the harness merges each artifact to the default branch by hand between stages, and the next stage
  is seeded from that committed file — so a stage that advanced the chain on its own would be caught
  by the following stage finding an upstream nobody merged. Not covered, deliberately:
  `gh pr create` (needs GitHub) and therefore the non-draft/no-auto-merge PR attributes, which stay
  prompt-level guarantees checked in `server/tests/test_sdlc_codex_acceptance.py`.
  **`codex_allowed` grants the option, not the traffic.** Three gates still ship off between this
  and an unattended Codex artifact session: `default_engine` still answers Claude for a config that
  says nothing, `CCSWITCH_CODEX_AUTOMATIONS_ENABLED=false` clamps an opted-in origin back to Claude,
  and without `CCSWITCH_CODEX_UNATTENDED_NETWORK_OPT_IN` the launch is refused by
  `unattended_launch_blocker`. Each is pinned by its own test in section 5 of
  `server/tests/test_sdlc_codex_acceptance.py`.
- **PM origins enabled (#1268 onboarding/critic, #1269 classification).** `pm-onboard` and
  `pm-classify` spawn unattended sessions but are *operator-triggered* — the Projects workspace and
  the pm-triage drift path call their seams; nothing ticks them. They were not in the automation
  registry at all, so `AutomationContext.launch_plan` swallowed the `KeyError`, answered
  `claude_only`, and no `engine` config field existed to opt in with; their activity-event and
  spawned-session endpoints 404'd for the same reason. Registering them fixes both. An origin with
  no control loop declares `modes = ()` (`automations.base.operator_triggered`): its config —
  including the engine fields its ceiling admits — is patchable, while an `enabled`/`mode`/`schedule`
  PATCH is refused 422 and the card renders no toggle, rather than showing an operator a switch that
  switches nothing.

  The widening also fixed three engine-routing defects, the same shapes #1265/#1266 found: all three
  PM spawn sites probed the *Claude* account pool (`_backlog_healthy_account` with no engine), so a
  Codex launch's Anthropic account was dropped by `sanitize_launch_for_engine` and the session had
  none; `pm_onboard_spawn_map` never passed its engine to `build_map_synthesis_prompt`, which would
  have told a Codex session to co-author its commits as Claude; and the policy check sat *after* an
  account probe and a full `ensure_clone`, so a refused launch paid for a git clone on every tap.
  The critic prompt carried no attribution trailer at all even though it commits and pushes.

  Live evidence: `server/tests/test_pm_codex_live.py`, opt-in via `CCSWITCH_TEST_CODEX_LIVE=1`. It
  runs a bounded classification batch through the real `codex` binary in `read-only` (a classify
  session has no write to do) and checks the ingest parser `_pm_classify_ingest` uses yields exactly
  one row per listed item, no invented features, and a usable `no_fit` escape hatch; then it runs
  the diff-scoped map-synthesis prompt and its critic in `danger-full-access` against a throwaway
  repo whose `origin` is a local bare repo, and checks the property the chain exists for — **review
  separation**: synthesis pushes ONE branch, the critic augments THAT branch instead of opening a
  second, the default branch is never touched, both commits carry the Codex trailer and never
  Claude's, and neither writes outside `docs/product/features/`. The same three gates as the SDLC
  origins still ship off in front of any of it.
- **PR Doctor enabled (#1267).** `pr-doctor`'s ceiling is now `codex_allowed`, on its offline
  acceptance suite (`server/tests/test_pr_doctor_codex_acceptance.py`): the seed prompts (CI-fix,
  merge-conflict, consolidation) carry their whole contract with no Claude-only syntax and the right
  commit trailer for either engine; the shared launch preconditions hold; and both fresh-spawn call
  sites (`_pr_doctor_session_fix`'s no-live-driver branch, `_pr_doctor_consolidate_spawn`) resolve
  the engine before their clone/model-assessment tail, same as every other origin's Task 7.1 shape.
  PR Doctor also has an injection path unique among these origins — a fix/conflict prompt sent into
  an *already-live* session via the shared `AgentDriver.send_prompt` contract rather than a fresh
  spawn. That path needed no new engine-policy gate (the engine was decided once, at the live
  session's own original spawn), but it was silently building every injected prompt with the Claude
  trailer regardless of which engine the live session was actually running; it now threads the live
  session's own `rec.engine` through.

  The widening also fixed the same phantom-spawn defect #1264's own suite found and named for
  backlog-runner: `AutomationContext.spawn_session` returns `None` when the resolved engine/mode pair
  cannot complete the automation's push+PR contract (e.g. a Codex spawn under the unattended
  `full-auto` default), but both PR Doctor spawn tails assumed any return value was a real session
  record — under `claude_only` this was unreachable (Claude is never blocked by the sandbox check),
  but widening the ceiling made a Codex-configured spawn hit it and get reported as `"spawned"` with
  a null session id and a phantom `attempted` feed event. Both tails now treat a `None` return as a
  refusal that consumes no attempt.

  Not covered, deliberately: a live Codex session actually restoring or consolidating a real PR —
  live-boundary work the owner accepted as outstanding to widen the ceiling now rather than block on
  it, the same call already made for `backlog-runner`. The same three gates as the SDLC/PM origins
  still ship off in front of any of it.
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
- **Sub-agent capability (#1274).** `subagents=False` was found declared-but-unwired, not absent:
  the app-server has real `subAgentActivity` and `collabAgentToolCall` thread items (`agentPath`/
  `agentThreadId`/lifecycle `kind`; `tool`/`senderThreadId`/`receiverThreadIds`/`agentsStates`),
  already normalized to `Task` `ToolCall`/`ToolResult` events by `server/codex/translator.py` and
  already recognized by `server/sessions/subagents.py`'s dashboard summary. The flag stays False
  because two things are unverified against a live binary: whether a multi-call collab sequence
  (spawn → sendInput/wait → close) collapses into one dashboard entry (there is no session-wide
  "agent id" in the wire payload — the real identity lives in `receiverThreadIds`/`agentsStates`,
  whose values are native thread ids that must not ride in a generic wire payload, so they surface
  only as an observability counter, `TranslatorCounters.collab_agent_states_seen`), and there is no
  transcript-drill-down for a child agent's own conversation yet (the natural mechanism is
  `CodexThreadReplay` over the already-wired `thread/read` RPC, given a designed fetch-by-id path).

**Evidence** — current engine and normalized stream in `server/sessions/claude_driver.py` and
`server/sessions/events.py`; orchestration/fleet routing in `server/sessions/orchestrator.py` and
`server/sessions/fleet_orchestrator.py`; account discovery/selection in `src/ccswitch/accounts.py`;
current Copilot translating-proxy behavior in `server/sessions/copilot_proxy.py`; official Codex
app-server/auth contracts at <https://learn.chatgpt.com/docs/app-server> and
<https://learn.chatgpt.com/docs/auth>. Full design:
`docs/superpowers/specs/2026-09-04-native-openai-codex-integration-design.md`.
