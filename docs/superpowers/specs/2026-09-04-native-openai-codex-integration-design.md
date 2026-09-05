# Native OpenAI / Codex Integration Design

**Status:** accepted; implementation plan in `../plans/2026-09-04-native-openai-codex-integration.md`

**Date:** 2026-09-04

**Scope:** native Codex execution, OpenAI account management, fleet routing, and product UI

**Related:** GitHub Project [ccswitch #26](https://github.com/users/steiner385/projects/26),
PR [#1088](https://github.com/steiner385/ccswitch/pull/1088)

## 1. Executive summary

ccswitch should add Codex as a second native **agent engine**, not bolt OpenAI onto the existing
Claude account-provider switch. The distinction is load-bearing:

| Account provider | Agent engine | How it runs today / in this design |
|---|---|---|
| Anthropic | Claude Code | Claude Agent SDK with an isolated `CLAUDE_CONFIG_DIR` |
| GitHub Copilot | Claude Code | Claude Agent SDK through the supervised Copilot translating proxy |
| OpenAI | Codex | Supervised `codex app-server` with a central refresh authority and host-local runtime state |

The browser and fleet should continue to consume one normalized session API and event stream.
Behind that boundary, an engine registry selects either the existing Claude driver or a new Codex
driver. Codex is integrated through its official app-server protocol over child-process stdio;
ccswitch must not scrape Codex rollout files, patch the CLI, or expose app-server on the network.

The first useful release should support named ChatGPT accounts, Codex model discovery, interactive
threads, streaming transcript items, approvals, interruption, resumption, quota display, and
multi-host dispatch. API-key authentication is explicitly deferred until after v1; its later
implementation can use the same engine architecture without complicating subscription quota and
rotation in the first release.

This is an architectural extension, not a provider-sized patch. The current session driver,
database names, model catalog, account discovery, quota shape, replay path, and automation prompts
all encode Claude assumptions. Adding conditionals to those surfaces would produce two partial
implementations. The design instead creates one provider-neutral contract and keeps
engine-specific behavior in adapters.

## 2. Goals

1. Add and reauthenticate one or more named OpenAI accounts without copying secrets through the
   browser after login.
2. Launch native Codex sessions against any configured project/worktree and interact with them
   from the existing web terminal.
3. Preserve the existing decentralized topology: the edge coordinates, the owning runner executes,
   and normalized events cross the fleet boundary.
4. Expose the models, reasoning-effort choices, quota windows, approvals, and activity types Codex
   actually reports rather than presenting false Claude parity.
5. Preserve all current Anthropic and Copilot behavior while removing Claude-only concepts from
   shared abstractions.
6. Recover sessions after browser reconnect, app-server restart, or daemon restart without parsing
   private on-disk transcript formats.
7. Keep credentials isolated per account and all command/file actions inside the session's assigned
   worktree and explicit approval policy.

## 3. Non-goals for the first release

- Cross-engine migration of a live conversation.
- Automatic selection between Claude Code and Codex. The user or automation chooses the engine;
  account rotation happens only among compatible accounts for that engine.
- Codex Cloud tasks, remote app-server WebSockets, or hosted task execution.
- OpenAI API-key authentication or usage-based billing controls; both are post-v1 work.
- Experimental app-server APIs other than the version-pinned external-token broker required for
  simultaneous multi-runner use; dynamic tool registration and custom collaboration agents remain
  out of scope.
- Managing Codex plugins, skills, MCP servers, or `config.toml` from the ccswitch config library.
- Importing arbitrary CLI-created Codex sessions into ccswitch.
- Rendering raw chain-of-thought. Only protocol-provided reasoning summaries may be shown.
- Full automation parity on day one. Interactive-session correctness is the release gate; automation
  enablement follows an explicit compatibility audit (§14).

## 4. What exists today

### 4.1 Product and runtime

- `SessionOrchestrator` prepares a worktree or scratch directory, selects an account, composes a
  Claude config directory, and starts one `SessionDriver`.
- `SessionDriver` and `real_sdk_factory` construct `ClaudeSDKClient` directly. Translation,
  permission handling, interrupts, model changes, context reporting, and replay are consequently
  Claude-shaped even where their output event names look generic.
- `SessionRecord` and the `Session` database row store `account_name` and
  `claude_session_id`; neither records an engine.
- `server/sessions/events.py` is already the right boundary for browser-facing events. It can be
  evolved rather than replaced.
- Transcript replay for existing sessions reads Claude transcript JSONL. That remains a Claude
  adapter responsibility; Codex replay must use `thread/read`.
- The web session terminal already handles multiplexed live events, reconnect/gap healing,
  permission and question cards, interruptions, and multiple viewers.

### 4.2 Accounts and rotation

- Anthropic accounts are inferred from credential files, while Copilot accounts are inferred from a
  separate directory tree. `provider_of(name)` effectively means “Copilot if that directory exists,
  otherwise Anthropic.” A third provider makes that fallback unsafe.
- Account names are global identifiers across providers. The ledger and selectors key by name.
- Copilot is not a second agent engine: it translates the Anthropic protocol and still launches a
  Claude session.
- The shared `Usage` shape assumes two Claude-style percentages. Copilot projects its one premium
  allowance into both fields. Codex can report several independent and model-specific rate-limit
  buckets, so repeating that projection would lose decision-critical information.

### 4.3 Fleet

- The edge uses `FleetOrchestrator` to rank healthy runners, dispatch over mTLS RPC, and stamp each
  session with one owning host.
- The runner daemon owns live agent processes. Browser traffic and events are proxied; agent
  processes do not run in the edge web process.
- Current production is effectively one targeted session runner (`rocky`) with `dobby` acting as
  edge/primary, while the architecture supports N runners.
- Host health advertises daemon protocol, capacity, load, and source SHA, but not installed agent
  engines or engine versions.

### 4.4 GitHub planning state

As of 2026-09-04, user Project #26 contains 42 items. Its active initiatives are primarily the
GitHub-native planning work (003) and the AI assistant epic. No issue or project item specifies
native OpenAI/Codex product integration. PR #1088 adds Codex-compatible repository governance,
skills, rules, and agent profiles for contributors; it touches no account/session runtime code and
is not this feature.

## 5. Verified Codex integration surface

The local installed CLI (`codex-cli 0.153.2`) was exercised against a real signed-in ChatGPT
account through `codex app-server` without starting a model turn. The following worked over stdio:

- JSON-RPC initialization and capability handshake.
- `account/read` for the active auth type and plan.
- `account/rateLimits/read`, including multiple independent quota buckets.
- `model/list`, including model IDs, defaults, supported reasoning efforts, and modalities.
- `configRequirements/read`.
- Two app-server processes with separate local homes simultaneously accepted the same externally
  managed ChatGPT access token, returned account state, listed models, and returned rate limits.
  No model turn was started. The method required `capabilities.experimentalApi=true`.

The official app-server lifecycle supports thread start/resume/fork, turn start/steer/interrupt,
streamed item notifications, server-initiated approval requests, account login/logout, and thread
history reads. The installed command still labels app-server experimental, so ccswitch must isolate
protocol churn behind one adapter and advertise its tested CLI/schema compatibility.

Contract sources:

- [Codex app-server protocol](https://learn.chatgpt.com/docs/app-server)
- [Codex authentication](https://learn.chatgpt.com/docs/auth)
- [Codex CLI](https://developers.openai.com/codex/cli)

Only documented, non-experimental app-server methods are in first-release scope except
`chatgptAuthTokens`, which is the explicit, isolated exception required by D4. The official page
calls it experimental, while the generated 0.153.2 schema says unstable and for OpenAI internal use
only. That risk must remain visible in the UI/operator docs and is contained by version pinning,
startup probes, a kill switch, and a managed-auth fallback that sacrifices multi-runner concurrency
rather than corrupt credentials. Build and test fixtures should be generated from the exact
supported CLI version using app-server's schema generation command, not copied from memory.

## 6. Decisions of record

| # | Decision |
|---|---|
| D1 | Model the executable agent as `engine` (`claude_code`, `codex`) and the credential/billing source as `provider` (`anthropic`, `copilot`, `openai`). |
| D2 | Add an `AgentDriver` protocol and engine factory registry. The existing driver becomes the Claude implementation; Codex gets a separate implementation. Shared orchestration never imports either vendor SDK/protocol type. |
| D3 | Each runner may supervise one local app-server child per OpenAI account, using stdio JSONL and host-local runtime state. A single account may be active on several runners simultaneously. Do not expose the experimental WebSocket transport. |
| D4 | Dobby hosts a dedicated credential-authority service under a separate OS identity: one managed-auth app-server per account owns device login, the canonical refresh token, and all refreshes. Runner app-servers use external `chatgptAuthTokens`, receive only short-lived access tokens over a dedicated mTLS listener, and request refresh through dobby. |
| D5 | Keep the browser/edge wire provider-neutral. Adapters translate native activity into normalized events and capabilities. |
| D6 | Discover models from the selected engine/account at runtime. Do not hard-code the locally observed model list. |
| D7 | Replace the two-field shared usage assumption with a list of typed quota windows. Rotation is engine-local and considers every applicable binding window. |
| D8 | Replay Codex from `thread/read` and live protocol events. Never parse `~/.codex/sessions` or other private rollout files. |
| D9 | Store the native thread identifier as `engine_session_id`; record `engine` on every session. Migrate and remove `claude_session_id` rather than maintaining parallel identifier columns indefinitely. |
| D10 | Ship interactive Codex sessions as v1, then deliver backlog/SDLC automation parity as a required fast follow. Keep each automation origin disabled for Codex until its compatibility audit passes. |
| D11 | V1 supports ChatGPT device-code authentication only. Add API-key authentication in a later release. |
| D12 | Accept the external-token API's experimental/unstable status for v1 because simultaneous use of one ChatGPT account across runners is a hard requirement. Fail closed if the pinned contract is absent or incompatible. |

D4 preserves the existing “dobby is canonical” topology while changing how workers consume the
credential. OpenAI's managed-auth guidance says one `auth.json` must be used by only one machine or
serialized job stream because Codex refreshes and rewrites it internally. Therefore runner
app-servers must not open the shared file in managed-auth mode. Dobby alone owns that file and its
refresh token. It fans out the current short-lived access token to any number of external-auth
runner processes; a 401 produces a server request that ccswitch routes back to the one authority,
which coalesces refreshes and returns the new access token. This is the only app-server surface
found that satisfies both one canonical ChatGPT login and simultaneous execution on several hosts.

## 7. Target architecture

```text
Browser
  │  existing HTTP + normalized session WebSocket
  ▼
Edge FastAPI / FleetOrchestrator
  │  engine-aware mTLS session RPC; normalized events only
  ▼
Runner daemon(s), concurrently
  │
  ├── SessionOrchestrator ── AgentDriver protocol ── ClaudeAgentDriver
  │                                              └── CodexAgentDriver
  │                                                       │ request IDs,
  │                                                       │ thread routing,
  │                                                       │ event translation
  └── CodexAppServerManager(account)
          ├── external chatgptAuthTokens supplied in memory
          ├── host-local CODEX_HOME, SQLite, logs, caches, and thread state
          └── codex app-server --listen stdio://

Dobby-only credential plane (separate service and Unix identity)
  Edge ────────────────┐
  Codex runner(s) ─────┼── dedicated mTLS auth API ── CodexCredentialAuthority
                       │                                ├── managed-auth app-server(s)
                       │                                └── private auth.json homes under
                       │                                    /var/lib/ccswitch-codex-auth
                       └── no general session-daemon RPC routes
```

### 7.1 Shared engine contract

The exact Python names can be settled in the implementation plan, but the boundary must cover:

```text
start_or_resume(session_spec) -> engine_session_id
send(prompt, attachments?) -> turn_id
steer(prompt) -> acknowledgement
interrupt() -> acknowledgement
answer_approval(request_id, decision, amendment?)
answer_question(request_id, answers)
set_model(model, reasoning_effort?)
set_permission_profile(profile)
replay() -> normalized events
capabilities() -> SessionCapabilities
close()
```

Engine-specific methods must not leak into `FleetOrchestrator`, HTTP routes, the session database,
or React conditionals. Optional behavior is advertised through capabilities and rejected explicitly
when unsupported.

### 7.2 Codex process manager

`CodexAppServerManager` owns process supervision and protocol mechanics, not session product logic:

- Resolve a supported `codex` binary and record its version.
- Spawn with a minimal environment, an entirely host-local `CODEX_HOME`, inherited project-safe
  essentials, and separate stderr capture with secret redaction.
- Perform `initialize` / `initialized` before serving requests.
- Correlate JSON-RPC request IDs, route thread/turn notifications to the correct driver, and route
  server-initiated requests back to exactly one pending approval.
- Bound outgoing requests and incoming event queues so one noisy turn cannot starve the daemon.
- Restart after a crash with jittered backoff, then let active drivers resume their native threads.
- Ref-count account users and apply an idle grace period, as the Copilot manager does.
- Reject an incompatible schema/version and make the account unhealthy instead of guessing.
- Authenticate by requesting the current access-token envelope from the credential authority and
  calling experimental `account/login/start` with `type=chatgptAuthTokens`.
- Forward `account/chatgptAuthTokens/refresh` server requests to the authority and answer only with
  a newer access token for the requested account/workspace.

One process per account per active runner provides the local account/auth boundary while allowing
that runner's threads to share a protocol connection. The same account may have another manager on
another runner. A manager crash affects only that host's sessions; the supervisor must emit a
visible degraded state and resume them after restart.

## 8. Accounts and authentication

### 8.1 Explicit account registry

Replace provider inference with one canonical, versioned, non-secret `accounts.json` manifest under
the existing account-store root. It is distinct from the cross-host multi-writer usage ledger:
dobby/authority is the only manifest writer and runners consume read-only snapshots. Each entry
needs at least:

```json
{
  "name": "openai-main",
  "provider": "openai",
  "engine": "codex",
  "auth_mode": "chatgpt",
  "runtime_auth_mode": "brokered_external_tokens",
  "credential_authority": "dobby",
  "enabled": true
}
```

Existing Anthropic and Copilot accounts are migrated into this registry from their current storage
locations. Secret material remains in provider-owned files/keyrings; the manifest contains only
metadata and credential references. Account names remain globally unique in the first release to
avoid a breaking API/UI change.

The authority OS user cannot read this manifest because the surrounding Claude store contains
credentials owned by `tony`. The edge therefore projects only `{name, provider, engine, enabled,
revision}` to the authority over the admin-only mTLS route. The manifest remains canonical; the
authority projection is a private, non-secret authorization cache with a monotonic revision and a
short sync lease. Edge startup and periodic reconciliation send a full snapshot. The authority
fails closed when the lease expires or a revision moves backward.

Disable/remove is ordered for safety: revoke issuance at the authority first, fan out a manager
invalidation to every runner, wait for managers to drop cached envelopes/stop the account's local
app-server, and only then finalize the manifest mutation. Enable writes the manifest before syncing
the authority, so a sync failure leaves the account visibly degraded but cannot accidentally issue
tokens. Direct manifest repair is reconciled on the next periodic snapshot.

### 8.2 ChatGPT login

Recommended default flow:

1. User chooses **Add account → OpenAI → ChatGPT subscription**.
2. Dobby's dedicated `CodexCredentialAuthority` service starts or acquires its managed-auth
   app-server against the canonical authority-only account home.
3. That authority calls `account/login/start` with the device-code mode.
4. API returns only the verification URL, user code, flow ID, and expiry/state to the browser.
5. Browser displays the code and polls the existing account-flow status surface.
6. App-server's completion notification and `account/read` confirm success; ccswitch writes the
   non-secret account manifest.

Reauthentication always runs through the authority. Logout first prevents new token issuance, drains
runner managers for the account, calls managed `account/logout` on the authority, and removes the
account only after no active sessions reference it.

### 8.3 Post-v1 API-key login

API-key auth is not part of v1. When added, it is an alternate `auth_mode`, not another provider or
engine. The key may cross the authenticated edge-to-runner control path once, must be redacted at
every logging layer, and must never be returned or written to the application database. The
app-server credential store owns it afterward.

API-key accounts do not expose subscription reset windows through `account/rateLimits/read` in the
same way as ChatGPT accounts. They should therefore be manually selectable and shown as
usage-unverified unless a separate billing-budget integration is designed. They must not appear
artificially “least used” in automatic rotation.

### 8.4 Credential authority and access-token broker

Codex supports file or OS-keyring credential storage for managed auth. V1 selects file storage for
the **authority only**, retaining one canonical per-account `auth.json` on dobby. Managed homes live
under `/var/lib/ccswitch-codex-auth/accounts/`, outside every NFS-exported or runner-mounted tree.
The authority runs as a dedicated `ccswitch-auth` OS user; session daemons and agent processes run
as `tony` and cannot traverse the authority state directory. Only the non-secret account manifest
is shared with runners.

`CodexCredentialAuthority` has four responsibilities:

1. Own device-code login/logout and the sole managed-auth app-server for each account.
2. Serialize proactive or 401-driven refresh through that app-server. Do not call OpenAI's OAuth
   refresh endpoint directly; Codex remains the black-box refresh implementation.
3. Read the newly persisted token bundle from the documented local auth cache, extract only the
   access token, workspace/account ID, and optional plan type, and return a short-lived in-memory
   envelope to authenticated runner daemons.
4. Coalesce concurrent refresh requests so one upstream refresh satisfies every waiting runner,
   then publish a monotonically increasing credential generation for cache invalidation.

The authority is a separate, narrowly scoped service and listener, not another handler on the
session daemon's `/rpc/{method}` dispatcher. Its network surface contains only versioned Codex-auth
login administration and token-envelope/refresh methods. Separate client certificate identities
and deny-by-default route ACLs allow the edge to administer login while runner identities may only
request envelopes/refresh for enabled accounts. A runner certificate can never invoke ordinary
session, automation, deletion, or deploy RPCs. The listener has a distinct server certificate/key
and client-verification CA; clients use distinct authority client certificates and a CA bundle for
verifying that server. It never reuses session-daemon server or edge-client key material.

Runner behavior:

1. Use a host-local `CODEX_HOME`; external-token login must not create `auth.json` there.
2. Request an envelope over the dedicated Codex-authority mTLS listener and pass it to app-server
   over stdio.
3. Keep tokens only in process memory and strictly redact protocol frames, exceptions, traces, and
   crash diagnostics.
4. When app-server sends `account/chatgptAuthTokens/refresh`, forward the request to the authority,
   verify the returned generation/account ID, and answer the pending JSON-RPC request within an
   eight-second total network budget. This stays below app-server's roughly ten-second callback
   timeout and includes pool acquisition, connect, write, and read time.
5. If the authority is unavailable or the contract changes, stop new turns and show
   `auth_authority_unavailable`; never fall back to concurrent managed `auth.json` use.

The local two-process probe confirmed that Codex 0.153.2 external-token app-servers created only
host-local runtime databases/caches, authenticated simultaneously, listed models, and read rate
limits without persisting an `auth.json` or starting a model turn. A two-host refresh-under-load
test remains a release-blocking spike.

### 8.5 Filesystem security

- Authority state root and account directories: owned by `ccswitch-auth`, mode `0700`, outside all
  runner-mounted/exported paths; canonical credential files: mode `0600`.
- Only the authority may read/write the refresh token; runners receive short-lived access tokens.
- The dedicated authority listener has no general session-daemon RPC dispatcher. Edge and runner
  client certificates have different identities and deny-by-default per-route authorization.
- Never include `auth.json`, API keys, access/refresh tokens, account IDs, or raw protocol frames in
  application logs, status responses, events, support bundles, or test fixtures.
- Device verification codes are short-lived but still redacted after the flow completes.

## 9. Sessions, threads, and turns

### 9.1 Creation

The create-session request gains:

- `engine`: required after the compatibility transition; UI defaults to the user's last choice.
- `account`: optional named compatible account; omitted means rotate within the selected engine.
- `model`: optional engine-native ID.
- `reasoning_effort`: optional and validated against the chosen model.
- `permission_profile`: provider-neutral policy intent.

Creation order is important:

1. Resolve repository and worktree exactly as today.
2. Resolve engine and a compatible account.
3. Rank all compatible healthy hosts normally; another host using the same OpenAI account does not
   exclude a candidate.
4. Persist the session with engine, provider/account, and owning host.
5. On that host, start or acquire the account's app-server and call `thread/start` with the exact
   worktree as `cwd` only after external-token authentication succeeds.
6. Persist the returned native thread ID as `engine_session_id` before starting the first turn.

No Codex command may run before the database row owns the worktree and host. If thread start fails,
creation compensates by cleaning the unused worktree and inactive row using the existing failure
rules.

### 9.2 Interaction mapping

| ccswitch action | Codex operation |
|---|---|
| Send first/follow-up prompt | `turn/start` on the stored thread |
| Steer active turn | `turn/steer` when advertised; otherwise reject with a capability message |
| Interrupt | `turn/interrupt` |
| Resume after daemon/app-server restart | `thread/resume`, then `thread/read` to heal history |
| Duplicate conversation | `thread/fork` (later UI capability) |
| Archive native history | `thread/archive`; ccswitch worktree/session archival remains separate |
| Delete native history | `thread/delete` only as part of the explicit destructive delete flow |

The ccswitch session ID remains the public identifier. Native thread and turn IDs are internal
correlation keys and must never replace it in URLs or fleet routing.

### 9.3 Replay and reconnect

The existing bounded live replay ring and monotonic ccswitch `seq` remain authoritative for an
attached daemon epoch. On cold recovery, `CodexAgentDriver.replay()` calls `thread/read` with turns
included and deterministically maps the returned items into normalized events. It then starts live
notification consumption after a replay watermark so completed items are not duplicated.

The mapper needs stable deduplication keys `(engine_session_id, turn_id, item_id, event_phase)`.
Where app-server provides deltas and a completed canonical item, completed content wins and replay
must not reproduce every historical delta.

## 10. Events, activity, and approvals

### 10.1 Normalized event evolution

Retain the existing high-level turn/text/status events and add one generic activity envelope rather
than a Codex event per protocol notification:

```text
ActivityStarted / ActivityDelta / ActivityCompleted
  activity_id
  kind: command | file_change | mcp_tool | web_search | collaboration | other
  title
  input_summary
  output_summary
  status
  engine_metadata (strictly allow-listed, no secrets)
```

Mapping:

- `agentMessage` → existing text delta/final message events.
- `reasoning` → thinking/reasoning-summary event; never label it raw chain-of-thought.
- `commandExecution`, `fileChange`, `mcpToolCall`, `webSearch`, and `collabToolCall` → activity
  envelope.
- `contextCompaction` → condensing status/event.
- token-usage notifications → context-usage event.
- `turn/completed` → turn end with a normalized success/interrupted/failed reason.

Existing Claude tool events can migrate to the same envelope, deleting parallel renderer logic
once parity tests pass.

### 10.2 Approval requests

Codex sends server-initiated JSON-RPC requests for command execution and file changes. The driver
must turn each into a normalized permission card containing:

- opaque ccswitch request ID bound to native request ID, thread, turn, and item;
- action type and allow-listed command/path/change summary;
- exact available decisions (`accept`, `acceptForSession`, `decline`, `cancel`, and command
  amendment where offered);
- expiry/cancellation state.

The reply path verifies session ownership, user identity, pending state, and decision membership
before responding on the app-server connection. A response for a different thread or a replayed
request is rejected and audited.

Provider-neutral permission profiles express intent; adapters map them to native controls:

| Profile intent | Claude Code | Codex baseline |
|---|---|---|
| Interactive | existing standard/ask behavior | workspace-write sandbox + ask on risky actions |
| Plan/read-only | plan/read-only mode | read-only sandbox + approvals as required |
| Trusted automation | existing automation mapping | explicit configured approval policy + workspace-write |
| Unrestricted | existing bypass mode | danger-full-access only after explicit high-friction choice |

These are not asserted to be semantically identical. The UI shows engine-specific details before a
profile change. “Always allow” maps only to an offered session-scoped decision and never invents a
persistent global exception.

## 11. Models and configuration

### 11.1 Engine-aware model catalog

The current global Claude catalog becomes an engine catalog service. A model response includes:

```text
engine, id, display_name, is_default,
supported_reasoning_efforts, default_reasoning_effort,
input_modalities, availability_source, observed_at
```

- Claude keeps its existing binary-derived catalog behind `ClaudeModelCatalog`.
- Codex uses `model/list` from the selected account's app-server and caches briefly by account,
  Codex version, and config profile.
- A model selected for one engine can never be passed to the other engine.
- Model availability drift returns a validation error with refreshed choices; it does not silently
  substitute a model except when the user explicitly selected “engine default.”

Suggested HTTP shape: `GET /api/models?engine=codex&account=openai-main`. Session summaries also
include the effective model and reasoning effort reported by the engine.

### 11.2 Project and user configuration

Codex receives the session worktree as `cwd`, so project `AGENTS.md` discovery and normal Codex
configuration apply natively. ccswitch may pass an allow-listed set of session overrides through
the documented config mechanism, but must not write project files or mutate a user's Codex config
merely to launch a session.

Codex configuration-library management is deferred. The first release reports relevant effective
capabilities and configuration errors; it does not attempt to make Claude commands/hooks/skills
look portable where they are not.

## 12. Quota and account rotation

Replace `Usage(session_pct, weekly_pct, ...)` at the shared selector boundary with:

```text
QuotaSnapshot
  provider
  fetched_at
  availability: known | unknown | unsupported | auth_error
  windows[]:
    key, label, used_pct, resets_at, duration_seconds,
    scope: account | model | feature,
    applies_to_models[]
```

Provider adapters produce the shape:

- Anthropic: session and weekly windows.
- Copilot: one premium-request window, without fabricating a second independent constraint.
- OpenAI ChatGPT: every primary, secondary, or named/model bucket returned by app-server.
- OpenAI API key: unsupported/unknown until a billing-budget source exists.

For a requested model, the selector computes the maximum used percentage among every applicable
window; the highest binding constraint determines rank. Unknown usage retains the current
conservative fallback and is visibly labeled. Accounts are filtered for matching engine, model
availability, host eligibility, enabled state, auth health, and cooldown before ranking.

Rotation never changes engines. A rate-limited Codex session can rotate to another eligible OpenAI
account by starting/resuming the same thread only if the protocol and credential boundary permit
that thread to be accessed under the target account. Until verified, the safe behavior is to start
a new ccswitch continuation session with an explicit transcript summary, mirroring the product's
existing handoff semantics rather than copying private Codex state.

## 13. Fleet and decentralization

### 13.1 Host capabilities

Runner heartbeat/probe data gains:

```json
{
  "engines": {
    "claude_code": {"available": true, "version": "..."},
    "codex": {
      "available": true,
      "version": "0.153.2",
      "protocol_schema_hash": "..."
    }
  }
}
```

Fleet dispatch first filters, then scores. A healthy high-capacity host without the selected engine
or broker-protocol compatibility is not a candidate. The same account may be selected on several
hosts. Protocol/schema incompatibility makes Codex unavailable on that host without taking Claude
sessions offline.

### 13.2 Central credential authority and concurrent runner fan-out

There is no account-to-host affinity or fleet-wide runtime lease. Dobby remains the canonical
credential master. Its authority is the only component that operates Codex managed auth and the
only component with a refresh token. Every runner uses local app-server state plus an external
short-lived access token from that authority.

In concrete terms:

- `openai-main` may have an app-server on `rocky` and another on a second runner concurrently;
- each runner may run several local threads for that account;
- both use the same current access-token generation and consume the same account quota;
- neither runner possesses or rotates the refresh token;
- a 401 from either runner is routed to the one authority; concurrent 401s collapse into one
  managed refresh and all runners receive the new generation;
- each session's native thread state remains on its stamped runner, matching existing fleet
  ownership and avoiding SQLite over NFS.

The Claude NFS credential directory remains unchanged. Codex refresh credentials instead live in
the authority service's non-exported `/var/lib/ccswitch-codex-auth` state root; the shared account
store contains only the non-secret OpenAI manifest entry. This is the crucial difference from
Claude: Codex's public managed-auth guidance forbids multi-machine concurrent use of one
`auth.json`, while the experimental external-token host API is specifically shaped for a host
application to own refresh and answer runner token requests.

### 13.3 RPC and event transport

The edge-to-runner RPC gains engine/account lifecycle methods and engine fields on existing session
messages. Token exchange does **not** use that listener: runners call the dedicated authority mTLS
service, whose route table has no ordinary daemon methods. Native app-server frames never cross
either boundary. Only validated ccswitch DTOs and normalized events leave the runner. Token DTOs
are handled by the separate redacted authority path and are never placed on the event bus,
persisted in the database, or returned to the browser.

## 14. Automation and product-management compatibility

Current automation is not provider-neutral merely because it sends text prompts. It assumes:

- Claude completion/no-PR markers;
- Claude error and limit classifications;
- Claude model aliases and self-model markers;
- Claude permission/question behavior;
- Claude transcript/adoption and subagent shapes;
- some Claude-only commands, hooks, and summarization paths.

Every automation origin therefore gains an explicit `engine_policy`:

- `claude_only` — compatibility default during rollout;
- `codex_allowed` — may select Codex after that origin passes its audit;
- `codex_only` — explicit owner configuration;
- no implicit `any` mode in the first release.

The compatibility audit covers prompt contract, completion detection, PR/no-PR behavior, approval
policy, restart/resume, limit handling, and observability. Backlog and SDLC automation parity is a
required fast follow to interactive v1, not an optional future direction. Each origin becomes
Codex-eligible as its audit and acceptance suite pass; the initiative is not complete until the
agreed backlog/SDLC origins are enabled. The future AI assistant may then choose an engine/model
through configuration. GitHub Project #26 remains the canonical planning surface; a later
implementation-plan materialization should create a new epic and dependent phase issues there
rather than append this work to the unrelated #1088 PR.

## 15. Data model and migration destination

Final schema changes:

- `sessions.engine` — non-null enum/string, backfilled `claude_code`.
- `sessions.engine_session_id` — renamed/backfilled from `claude_session_id`.
- `sessions.reasoning_effort` — nullable engine-native validated choice.
- account manifest — explicit provider, engine, user auth mode, runner auth mode, credential
  authority, enabled/health metadata, and non-secret credential generation/timestamps.
- quota ledger — serialized typed windows rather than only two percentages.
- host registry/probe payload — engine capabilities and versions.

Migration rules:

1. Backfill every existing session as `claude_code` and preserve its native ID exactly.
2. Introduce one canonical model accessor/API representation in the same migration series; do not
   leave new code reading one of two identifier columns.
3. Convert existing account discovery into manifest entries idempotently, then make the manifest
   canonical and retain filesystem scans only as a migration/repair command.
4. Update fixtures, SQLite reconciliation, Alembic, and the hand-maintained Postgres schema suites.
5. Keep wire compatibility for one fleet upgrade window: old clients default missing engine to
   `claude_code`; new Codex dispatch requires all participating edge/runner protocols to advertise
   support. Remove the default after the fleet has converged.

## 16. API and UI design

### 16.1 API additions/changes

- `POST /api/accounts/add/openai` — begin a ChatGPT device flow on a host; a post-v1 extension may
  add API-key enrollment without changing the engine/session contract.
- `GET /api/accounts/add/openai/{flow_id}` — flow status without secrets.
- existing reauth/remove endpoints become provider-dispatched.
- `GET /api/models` accepts engine/account context and returns capabilities.
- session create/update/summary DTOs gain engine, reasoning effort, and capabilities.
- fleet info reports engine availability/version/schema compatibility.

### 16.2 User experience

**Add Account** gains an “OpenAI / Codex” choice. ChatGPT is the recommended path; the dialog shows
the device URL/code and clear completion/error state. Account cards show provider, engine, auth
mode, credential-authority health, active runner count, login health, quota buckets, and model
availability.

**New Session** adds an **Agent** selector before account/model:

1. Agent: Claude Code or Codex.
2. Account: Automatic within this agent, or a named compatible account.
3. Model: runtime-discovered options for the resolved agent/account.
4. Reasoning: shown only when the model advertises choices.
5. Permission profile: common intent plus expandable engine-specific details.

Session cards and the transcript header show agent, provider/account, model, reasoning effort, and
host. Unsupported controls are absent or disabled with a capability explanation; Claude slash
commands are not presented as Codex commands.

The transcript reuses the existing turn/message/status design and adds activity cards capable of
showing commands, file changes, MCP calls, searches, and collaboration items without a separate
Codex transcript page.

## 17. Reliability, security, testing, and observability

### 17.1 Failure behavior

| Failure | Required behavior |
|---|---|
| Codex binary absent/incompatible | Host advertises Codex unavailable; Claude remains healthy |
| app-server crashes | All affected sessions show degraded; manager restarts; drivers resume/read threads |
| malformed/unknown protocol event | Quarantine/redact frame, emit adapter error, continue when safe; never guess an approval |
| auth expires | Account becomes `needs_reauth`; no rotation into it; active session history remains viewable |
| credential authority unavailable | Existing accepted tokens may work until rejected; new manager auth and refresh fail closed with a visible degraded state |
| one runner unavailable | Its sessions show host unavailable; the same account's sessions on other runners continue |
| external-token method absent/incompatible | Codex is disabled on that version/host; never fall back to concurrent managed-auth files |
| simultaneous runner 401s | Authority coalesces them into one managed refresh and returns one newer generation |
| browser disconnects during approval | Request remains pending at runner and replays on reconnect |
| daemon drains during active turns | Wait within the established drain budget; stop managers only after drivers settle |
| quota API unavailable | Mark snapshot unknown; use conservative selection and preserve last-good display with age |

### 17.2 Test strategy

- TDD a fake JSONL app-server transport: interleaved responses/notifications, server requests,
  cancellation, malformed input, backpressure, EOF, and restart.
- Generate JSON schema fixtures from the supported CLI and contract-test all request/response/event
  DTOs against them.
- Golden translation tests for every supported item type and cold replay, including delta/final
  deduplication and strict redaction.
- Approval security tests: wrong session/thread/user, duplicate reply, expired request, unsupported
  decision, command amendment, reconnect replay.
- Account tests: device flow, cancellation, reauth, removal with active sessions, permissions,
  manifest migration, and credential/log redaction.
- Selector tests with multiple OpenAI buckets, model-specific constraints, mixed engines, cooldown,
  and multi-runner consumption of one account. Post-v1 API-key work adds unknown-usage and
  billing-safety cases.
- Credential-broker tests: token-envelope redaction, generation monotonicity, concurrent refresh
  coalescing, wrong-account/workspace rejection, authority restart, 401 storms, and no runner-side
  `auth.json` persistence.
- Fleet tests for capability filtering, external-auth version skew, simultaneous account use, host
  loss, edge/worker rolling upgrade, and unrelated Claude availability during Codex failure.
- Real opt-in smoke: initialize, account/read, model/list, rateLimits/read, one temporary thread and
  one minimal turn, approval decline, resume/read, and archive. It must use an explicitly configured
  test account because it consumes quota and can execute tools.
- Run the full server suite, web Vitest suite, TypeScript check, lint, formatting, migration/schema
  tests, and a real Postgres suite before completion.

### 17.3 Observability

Metrics/log fields should include engine, provider, account alias, host, CLI version, request method,
latency, active threads/turns, queue depth, restarts, auth health, quota snapshot age, and approval
latency. Never include prompts, model output, commands, patches, account IDs, or credentials in
routine metrics. Logs use ccswitch session/thread correlation hashes rather than raw external IDs.

## 18. Governance and documentation impact

Implementation requires a constitution amendment before code lands because the current wording
makes Claude the only native engine:

- Generalize Principle I from “Claude Code” to black-box native agent integration through documented
  supported surfaces; retain the no-fork/no-patch rule.
- Generalize Principle II from `CLAUDE_CONFIG_DIR` to provider-safe credential/runtime isolation,
  explicitly naming Claude and Codex requirements and allowing a single credential authority to
  fan out short-lived access tokens without distributing refresh authority.
- Update the technical constraint that describes every session as a native Claude process so the
  daemon instead owns native engine drivers with normalized event streams.

`CLAUDE.md`, architecture docs, session/account/usage/fleet feature docs, API schemas, operator
runbooks, and deployment prerequisites must change with the implementation. This draft's product
companion is `docs/product/features/openai-codex-integration.md`.

## 19. Delivery slices for the later implementation plan

These are outcome boundaries, not the implementation task plan:

1. **Foundations:** constitution amendment, engine/provider vocabulary, schemas, account manifest,
   engine capabilities, and Claude backfill with zero behavior change.
2. **Codex control plane and credential authority:** supervised app-server transport, a dedicated
   dobby authority service/OS identity/private state root, a limited mTLS external-token broker,
   account health/model/quota APIs, and real-account verification including two simultaneous
   runners.
3. **Interactive sessions:** Codex driver, create/send/steer/interrupt, translation, approvals,
   replay/recovery, archive/delete, and terminal UI.
4. **Fleet and rotation:** engine capability probes, typed quota selector, broker-aware rolling
   compatibility, simultaneous same-account runner dispatch, and settings/session UX.
5. **Required automation fast follow:** origin-by-origin backlog/SDLC audits and enablement,
   completion/handoff semantics, operational hardening, docs, and GitHub Project materialization.

Each slice must delete superseded code and update the product feature source of truth in the same
PR. The later plan should express these as GitHub-native epic/sub-issue/dependency structures in
Project #26, following spec 003.

## 20. Confirmed and open owner decisions

Confirmed:

- **Authentication scope:** v1 is ChatGPT device-code only; API-key authentication is post-v1.
- **Automation launch bar:** interactive sessions form v1; backlog/SDLC Codex parity is a required
  fast follow, enabled origin by origin after compatibility testing.
- **Cross-host concurrency:** one OpenAI account must support simultaneous Codex sessions on
  multiple runners.
- **Experimental dependency:** v1 may depend on the version-pinned experimental
  `chatgptAuthTokens` host API, provided the design's startup probes, two-runner refresh test, kill
  switch, redaction, and fail-closed guards are implemented.
- **Agent selection:** users explicitly choose Claude Code or Codex. V1 does not automatically
  route across engines.
- **Initial control surface:** Codex exposes model, reasoning effort, and permission profile. Service
  tier, collaboration mode, and broader configuration profiles are deferred.
- **Account multiplicity:** the account model and APIs support multiple named OpenAI accounts from
  the beginning. Automatic OpenAI-account rotation is not required for v1.

There are no remaining product decisions blocking the implementation plan. Engineering spikes may
still reopen a contradicted technical decision, especially if the release-blocking two-runner
external-token refresh test fails.

## 21. Acceptance criteria

The native integration is complete when:

- a user can add, reauth, inspect, and remove a named ChatGPT/OpenAI account without credential
  leakage, and refresh credentials remain protected by the accepted canonical storage backend;
- a compatible runner can create a Codex thread in the correct worktree and the browser can send,
  steer, interrupt, approve/decline, reconnect, replay, resume after process restart, and archive;
- the UI shows live account-specific models/reasoning choices and all applicable quota windows;
- automatic account selection never crosses engines; post-v1 API-key work must additionally prove
  that unknown usage is never treated as free;
- the refresh credential remains canonical on dobby, only the authority can rotate it, and two or
  more runners can concurrently authenticate local app-servers using short-lived brokered tokens;
- Codex failures do not take Claude sessions offline;
- no code path scrapes private Codex state or exposes app-server outside the owning daemon;
- automation remains gated until its engine-specific acceptance suite passes;
- canonical docs, constitution, API schema, migrations, and Project #26 tracking all agree with the
  shipped behavior.
