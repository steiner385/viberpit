# Native OpenAI / Codex Integration Implementation Plan

> **For agentic workers:** REQUIRED SKILL: use `subagent-driven-development` to execute this
> plan task by task, with a fresh implementer and review between tasks. Check boxes are the
> execution record. Do not begin runtime code until the owner-controlled constitution amendment
> and the Phase 0 external-auth release gate are complete.

**Goal:** Ship native, interactive Codex sessions in ccswitch using named ChatGPT accounts, explicit
Claude Code versus Codex selection, runtime-discovered models/reasoning effort, provider-neutral
quota display, approvals, replay/recovery, and simultaneous use of one OpenAI account on multiple
runners. Then enable the backlog runner and AI-native SDLC automations as a required fast follow.

**Architecture:** Preserve ccswitch's edge/runner split. A provider-neutral `AgentDriver` contract
feeds the existing normalized session event stream. The current implementation becomes a Claude
adapter; a new runner-local Codex adapter supervises one `codex app-server` process per OpenAI
account and owns many local threads. A dedicated service on dobby is the only refresh authority: it
runs under a separate OS identity, owns managed ChatGPT login and the canonical refresh credential
outside every shared mount, and exposes only a narrow mTLS auth API. Runners authenticate
app-server with the accepted, version-pinned experimental `chatgptAuthTokens` API and keep access
tokens in memory.
Native app-server frames and tokens never reach the browser or the ordinary event bus.

**Tech stack:** Python 3 / FastAPI / SQLAlchemy 2.x / asyncio subprocess JSONL · existing mTLS daemon
RPC · React / TypeScript / TanStack Query / Vitest · native `codex app-server` 0.153.2 contract at
plan authoring time (re-pin from the real installed binary during Phase 0).

**Accepted spec:** `docs/superpowers/specs/2026-09-04-native-openai-codex-integration-design.md`.
Product source: `docs/product/features/openai-codex-integration.md`. Decisions D1–D12 in that spec
are settled. In particular: device-code auth only in v1; explicit engine choice; model, reasoning
effort, and permission profile are the only Codex launch controls; multiple named OpenAI accounts
are represented from day one; automatic OpenAI account rotation is not a v1 requirement; the
experimental external-token dependency is accepted subject to the release gate below.

**Tracking issues:** created in GitHub Project #26 per Task 0.1. Every phase issue is a sub-issue of
the epic; list order is presentation only after materialization. Native blocked-by relationships are
the execution source of truth: Phase 1 follows Phase 0; Phases 2 and 3 both follow Phase 1 and may run
in parallel; Phase 4 follows both; Phases 5–7 then form a chain. Task PRs reference the epic and their
phase tracker. Only the final PR that satisfies a phase's exit criterion closes that phase tracker;
the epic remains open through Phase 7.

| Scope | Issue | Plan section |
| --- | --- | --- |
| Epic | [#1146](https://github.com/steiner385/ccswitch/issues/1146) | whole plan |
| Phase 0 | [#1147](https://github.com/steiner385/ccswitch/issues/1147) | Governance and external-auth release gate |
| Phase 1 | [#1148](https://github.com/steiner385/ccswitch/issues/1148) | Provider-neutral foundations with no behavior change |
| Phase 2 | [#1149](https://github.com/steiner385/ccswitch/issues/1149) | Codex app-server protocol and process supervision |
| Phase 3 | [#1150](https://github.com/steiner385/ccswitch/issues/1150) | Credential authority, OpenAI accounts, models, and quota |
| Phase 4 | [#1151](https://github.com/steiner385/ccswitch/issues/1151) | Interactive Codex runtime |
| Phase 5 | [#1152](https://github.com/steiner385/ccswitch/issues/1152) | Fleet routing and browser experience |
| Phase 6 | [#1153](https://github.com/steiner385/ccswitch/issues/1153) | Interactive v1 hardening and launch |
| Phase 7 | [#1154](https://github.com/steiner385/ccswitch/issues/1154) | Required backlog and SDLC automation fast follow |

Official boundary references:

- [Codex app-server protocol](https://developers.openai.com/codex/app-server)
- [Codex authentication](https://developers.openai.com/codex/auth)
- [Non-interactive authentication guidance](https://developers.openai.com/codex/auth/ci-cd-auth)

## Global constraints

- **Governance first:** the present constitution defines Claude as the only native engine and its
  managed rules prevent an agent from editing the constitution. The owner must approve and land the
  provider-neutral amendment in Task 0.1 before runtime implementation starts.
- **Branching:** for each PR, fetch and branch from `origin/master`, never local `master`. Before
  opening a PR, use the `github-via-graphql` skill to inspect in-flight PRs touching the same files.
  PR #1088 is contributor-governance work, not the runtime integration; do not append these changes
  to it or base feature work on its branch.
- **Completion:** one coherent PR per PR group below. Open ready-for-review, arm squash auto-merge,
  and wait for the required checks. No AI attribution trailer unless the owner supplies one.
- **TDD:** write and observe each focused test fail before implementation. Security- and
  money-adjacent auth/quota paths require at least 85% branch coverage plus the negative cases named
  in this plan.
- **Real-boundary evidence:** mocks establish application behavior, not app-server truth. Every
  phase that adds a real Codex boundary ends with the named opt-in smoke. Do not start a model turn
  except with the explicitly configured smoke account.
- **No private-state parsing:** do not read Codex rollout/thread SQLite or JSONL to implement replay,
  status, models, quota, or auth. Use app-server methods. The only direct credential-file read is in
  the authority, after a managed app-server login/refresh, to extract the documented current auth
  cache into a redacted in-memory envelope; isolate that read behind one module.
- **No shared Codex runtime home:** runners get a local `CODEX_HOME`. Never put Codex SQLite, WAL,
  history, shell snapshots, thread state, or refresh credentials on NFS. Dobby's canonical managed
  homes live under `/var/lib/ccswitch-codex-auth`, outside every exported/mounted tree, owned by a
  dedicated `ccswitch-auth` user that session agents cannot read.
- **Separate auth plane:** runner token requests never share the session daemon's connection-wide
  mTLS listener or `/rpc/{method}` dispatcher. The authority has its own listener, certificate
  identities, limited route table, and deny-by-default per-route ACLs.
- **One source per concept:** one account registry, one quota DTO, one engine registry, one model
  catalog facade, and one normalized event vocabulary. Provider adapters convert into those
  contracts; they do not introduce parallel session APIs.
- **Rolling compatibility:** old edge/runner messages default a missing engine to `claude_code` for
  exactly one fleet-upgrade window. Codex dispatch requires the new protocol and matching schema
  capability. Remove the compatibility default in the final interactive-v1 cleanup PR.
- **Feature flags:** all default false until the named launch task:
  `CCSWITCH_CODEX_ENABLED`, `CCSWITCH_CODEX_EXTERNAL_AUTH_ENABLED`, and
  `CCSWITCH_CODEX_AUTOMATIONS_ENABLED`. Absence/incompatibility disables only Codex.
- **Pinned boundary settings:** exact names:
  `CCSWITCH_CODEX_BIN` (default `codex`), `CCSWITCH_CODEX_SUPPORTED_VERSIONS`,
  `CCSWITCH_CODEX_IDLE_GRACE_S` (default `300`),
  `CCSWITCH_CODEX_AUTHORITY_LISTEN_ENDPOINT`, `CCSWITCH_CODEX_AUTHORITY_TLS_CERT`,
  `CCSWITCH_CODEX_AUTHORITY_TLS_KEY`, `CCSWITCH_CODEX_AUTHORITY_TLS_CLIENT_CA`,
  `CCSWITCH_CODEX_AUTHORITY_CLIENT_ALLOWLIST`, `CCSWITCH_CODEX_AUTHORITY_ENDPOINT`,
  `CCSWITCH_CODEX_AUTHORITY_CLIENT_CERT`, `CCSWITCH_CODEX_AUTHORITY_CLIENT_KEY`,
  `CCSWITCH_CODEX_AUTHORITY_CA`, and
  `CCSWITCH_CODEX_AUTH_REFRESH_DEADLINE_S` (default `8.0`, below the native callback timeout).
  The runner-to-authority client identity must be distinct from the edge client identity.
- **Tests:** unset all `CCSWITCH_SESSIOND_*` and `CCSWITCH_DAEMON_ROLE` variables for ordinary
  server tests. When touching `src/ccswitch`, prepend this checkout's `src` to `PYTHONPATH`; the
  shared venv otherwise imports the deploy worktree. Long full-suite output goes through `tee` to a
  descriptive `/tmp` log, and only one full server run runs at a time.
- **Schema:** all ORM additions are nullable unless a reviewed migration/backfill proves otherwise.
  Add an Alembic revision chained from the live head at execution time, plus `init_db` reconciliation
  and data backfill because production does not run `alembic upgrade`. Run SQLite and real Postgres
  schema suites after any model change.
- **Time:** every API timestamp is UTC internally. Convert to `America/New_York` only at a
  user-facing presentation boundary.

## Fixed contracts

These names keep independently implemented slices from drifting.

```text
AgentEngine       = "claude_code" | "codex"
AccountProvider   = "anthropic" | "copilot" | "openai"
UserAuthMode      = "claude_oauth" | "github_device" | "chatgpt_device"
RunnerAuthMode    = "config_dir" | "copilot_proxy" | "chatgpt_external_tokens"

SessionLaunch
  engine, account?, cwd/repo/branch, model?, reasoning_effort?, permission_mode,
  initial_prompt?, attachments?, origin?, origin_ref?

QuotaSnapshot
  provider, fetched_at, availability, windows[]
QuotaWindow
  key, label, used_pct, resets_at?, duration_seconds?, scope, applies_to_models[]

CredentialEnvelope (RPC only; never persisted/logged)
  account, access_token, chatgpt_account_id, plan?, generation, expires_at?
```

`AgentDriver` is the single lifecycle protocol:

```python
class AgentDriver(Protocol):
    engine: AgentEngine
    engine_session_id: str | None
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send(self, prompt: str, *, attachment_paths: Sequence[str] = ()) -> None: ...
    async def steer(self, prompt: str) -> None: ...
    async def interrupt(self) -> None: ...
    async def set_model(self, model: str | None) -> None: ...
    async def set_reasoning_effort(self, effort: str | None) -> None: ...
    async def set_permission_mode(self, mode: str) -> None: ...
    async def respond_to_approval(self, request_id: str, decision: dict) -> None: ...
    def subscribe(self, callback: EventCallback) -> Unsubscribe: ...
```

Engine capabilities, not `if engine == ...` checks in the UI, decide which controls/actions render:

```text
models, reasoning_effort, steer, interrupt, approvals, questions, fork, adopt,
self_model_switch, subagents, attachments
```

## Dependency graph and delivery summary

```text
0 Governance + auth gate
          │
          ▼
1 Provider-neutral foundations ───────┐
          │                            │
          ├───────────────┐            │
          ▼               ▼            │
2 App-server transport   3 Auth/quota/account control plane
          └───────────────┬────────────┘
                          ▼
4 Codex interactive driver + normalized events
                          ▼
5 Fleet integration + browser UX
                          ▼
6 Interactive v1 hardening and launch
                          ▼
7 Required backlog/SDLC automation fast follow
```

| Phase | PR groups | Estimate | Exit criterion |
|---|---:|---:|---|
| 0 Governance and release gate | 2 | 2–3 d | owner amendment landed; two real runner app-servers share one account and recover through one authority refresh |
| 1 Provider-neutral foundations | 3 | 6–8 d | existing Claude/Copilot behavior is green behind canonical engine/account/quota contracts |
| 2 Codex protocol | 2 | 4–5 d | fake server contract suite and real read-only probe green; process manager restart-safe |
| 3 Auth/account/quota | 3 | 6–8 d | named OpenAI account can login/reauth/remove; runners receive only short-lived token envelopes |
| 4 Interactive runtime | 3 | 7–10 d | create/send/steer/interrupt/approve/replay/recover works through normalized events |
| 5 Fleet and web | 3 | 6–8 d | explicit Codex dispatch works on compatible runners; complete responsive UI and account/model/quota controls |
| 6 v1 launch | 2 | 3–4 d | real acceptance suite green, runbooks/telemetry complete, v1 flags enabled |
| 7 Automation fast follow | 3 | 6–9 d | backlog runner and agreed SDLC origins are Codex-eligible with origin-specific acceptance tests |
| **Total** | **21** | **~40–55 engineer-days** | interactive v1 plus required automation parity |

Estimates include review and integration fallout in the 10k-line orchestrator and broad usage-test
surface. They are sequencing estimates, not a promise that experimental app-server compatibility
will remain stable.

---

## Phase 0 — Governance and external-auth release gate

### Task 0.1: Owner-controlled constitution amendment and planning materialization

**PR group:** `docs/codex-governance` — owner/human authored and explicitly acknowledged.

**Files:**

- Modify: `.specify/memory/constitution.md`
- Modify: `CLAUDE.md`
- Modify: `.codex/config.toml`
- Modify as required: `.codex/rules/*`, `.claude/managed-settings.json`
- Modify: `server/sdlc/governance.py`
- Test: `server/tests/test_sdlc_governance.py`
- Test: `server/tests/test_sdlc_governance_config.py`

**Work:**

- [x] Generalize Principle I to unmodified native agent binaries and supported/documented
  integration surfaces, naming Claude Code and Codex without weakening the no-fork/no-patch rule.
- [x] Generalize Principle II to provider-safe identity/runtime isolation: Claude config-dir
  isolation remains intact; Codex allows one refresh authority to distribute short-lived access
  tokens while runtime homes remain host-local.
- [x] Generalize the technical session constraint from “one native Claude PTY” to daemon-owned
  engine drivers emitting normalized events.
- [x] Update the constitution version and Sync Impact Report; obtain the owner's explicit
  acknowledgement in the PR body.
- [x] Update governance rules/tests so the active Claude and Codex agents receive equivalent
  protection without pretending their permission syntaxes are identical.
- [x] After the amendment lands, use the `gh-issue-workflow` skill to create one epic plus dependent
  phase issues in GitHub Project #26. Record issue numbers in this plan before runtime execution.

**Landed:** #1098 (constitution v1.2.0, provider-neutral Principles I–IV), #1102 (review corrections
to Principle II and the session model), #1103 (shell-write gap, hook fail-open, CI amendment gate),
#1111 (guardrail changes gated on an owner acknowledgment check). Tracking issues #1146–#1154 are
recorded in the table above and linked as epic sub-issues.

**Verification:**

```bash
pytest server/tests/test_sdlc_governance.py server/tests/test_sdlc_governance_config.py
python scripts/run_evals.py
```

Expected: governance tests/evals pass for both agent configurations; no rule grants Codex broader
release authority than Claude.

### Task 0.2: Commit a sanitized two-runner auth/refresh spike

**PR group:** `spike/codex-external-auth`.

**Files:**

- Create: `scripts/spike_codex_external_auth.py`
- Create: `docs/superpowers/spikes/2026-09-04-codex-external-auth-findings.md`
- Create: `server/tests/test_codex_spike_redaction.py`

**Work:**

- [x] Generate and inspect `codex app-server generate-json-schema` output for the exact installed
  version. Record CLI version, protocol methods, experimental capability requirement, and a SHA-256
  of the relevant schema subset; never commit the entire user config or credential payload.
- [x] Start one managed-auth authority app-server on dobby and one external-auth app-server on
  each of two distinct registered runner hosts (initially dobby and rocky), each with a separate
  host-local temporary `CODEX_HOME`. Send the short-lived test envelope over authenticated stdin;
  never place it in SSH arguments, environment variables, shell history, or a file.
- [x] Initialize both runner processes with `experimentalApi=true`, inject the same current external
  access-token envelope, and verify `account/read`, `model/list`, and
  `account/rateLimits/read` concurrently.
- [x] Exercise the refresh callback. Give a runner an invalid/expired access token, observe
  `account/chatgptAuthTokens/refresh`, answer it from the single authority, and verify the runner
  recovers without a runner-side `auth.json`.
- [x] Trigger simultaneous refresh requests from both runners and prove they coalesce to one
  authority refresh and one monotonically newer generation.
- [x] Prove both temporary homes contain only local runtime state and that no access/refresh token,
  ChatGPT account id, or raw native thread id appears in stdout/stderr/findings.
- [x] Make the script refuse to run unless `CCSWITCH_TEST_CODEX_ACCOUNT` and
  `CCSWITCH_TEST_CODEX_EXTERNAL_AUTH=1` are set. Default mode performs no model turn.
- [x] Record pass/fail evidence and cleanup behavior in the findings doc.

**Release gate:** stop here if refresh recovery, coalescing, or redaction fails. Do not fall back to
sharing `auth.json` or a common `CODEX_HOME` across hosts. Revisit the architecture with the owner.

**Landed:** #1092 (spike harness), #1095 (hardened evidence), #1116 (two-runner refresh proof). The
gate **passed** on the real boundary — `codex-cli 0.153.2`, two distinct runner hosts, one successful
callback per runner coalesced to `refresh_calls: 1` and `credential_generation: 1`, clean runner
homes, clean redaction, complete cleanup. Full record:
`docs/superpowers/spikes/2026-09-04-codex-external-auth-findings.md`.

**Verification:**

```bash
pytest server/tests/test_codex_spike_redaction.py
CCSWITCH_TEST_CODEX_EXTERNAL_AUTH=1 \
CCSWITCH_TEST_CODEX_ACCOUNT=openai-main \
python scripts/spike_codex_external_auth.py 2>&1 | tee /tmp/codex-external-auth-spike.log
```

Expected: two runner identities show healthy read-only calls; one refresh generation is issued;
the script's redaction scanner reports zero secret matches; temp homes are removed.

---

## Phase 1 — Provider-neutral foundations with no behavior change

### Task 1.1: Canonical engine vocabulary and driver factory

**PR group:** `refactor/agent-engine-contract`.

**Files:**

- Create: `server/sessions/engine.py`
- Rename: `server/sessions/driver.py` → `server/sessions/claude_driver.py`
- Rename: `server/sessions/sdk_factory.py` → `server/sessions/claude_sdk_factory.py`
- Modify: `server/sessions/orchestrator.py`
- Modify: `server/sessions/facade.py`
- Modify: `server/sessions/remote.py`
- Modify: `server/ipc/orchestrator_rpc.py`
- Test: `server/tests/test_agent_driver_contract.py`
- Modify all existing driver imports/tests in the same PR

**Work:**

- [ ] Write contract tests for the required lifecycle, subscription, capability, and native-id
  members. Prove the Claude adapter satisfies them structurally.
- [ ] Add `AgentEngine`, `AgentCapabilities`, `AgentDriver`, `AgentDriverFactory`, and one engine
  registry. Reject unknown engines; default only legacy deserialization to `claude_code`.
- [ ] Rename the concrete implementation to `ClaudeAgentDriver` and update every caller. Do not
  leave a compatibility class or a second registry.
- [ ] Route all construction through the injected factory. Preserve every Claude option and event
  byte-for-byte where the wire contract is unchanged.
- [ ] Add `engine` to remote/facade/RPC DTOs and bump the daemon protocol only if the compatibility
  test proves the change cannot be additive.

**Verification:** focused driver/orchestrator/RPC tests, then the full four-part backend gate. Use
`PYTHONPATH=$PWD/src` for core tests.

### Task 1.2: Session schema and native-id migration

**PR group:** `refactor/engine-session-schema`.

**Files:**

- Modify: `server/db/models.py`
- Add migration: `server/db/migrations/versions/<next>_session_engine.py`
- Modify: `server/db/__init__.py` reconciliation/backfill helpers
- Modify: `server/sessions/types.py`
- Modify: `server/sessions/orchestrator.py`
- Modify: `server/api/sessions.py`
- Modify: `web/src/api/types.ts`
- Modify: archived/adopt/export/session tests referencing `claudeSessionId`
- Test: `server/tests/test_session_engine_migration.py`
- Test: `server/tests/test_schema_upgrade_from_baseline.py`
- Test: `server/tests/test_postgres_compat.py`
- Test: `server/tests/test_schema_reconcile_coverage.py`

**Work:**

- [ ] Add nullable `sessions.engine`, `sessions.engine_session_id`, and
  `sessions.reasoning_effort`; backfill all existing rows to `claude_code` and copy the native id
  exactly from `claude_session_id`.
- [ ] Change the ORM/record/API to the canonical names and update all call sites in this PR. The
  runtime reconcile path must add/copy because production does not apply Alembic migrations.
- [ ] Keep the physical legacy column only as an ignored database artifact where reconcile cannot
  drop safely; no Python/TypeScript code may read or write it after this PR. The migration's upgrade
  may rename/drop it on managed migration paths.
- [ ] Add `engine` and `engineSessionId` to live/archived wire shapes and remove
  `claudeSessionId` from the web contract. Adoption remains a Claude-only capability and keeps its
  explicitly Claude-named candidate field.
- [ ] Prove a baseline SQLite database upgrades idempotently and a real Postgres database preserves
  values and indexes.

### Task 1.3: Explicit account registry and provider-neutral quota

**PR group:** `refactor/account-quota-contract`.

**Files:**

- Create: `src/ccswitch/account_registry.py`
- Create: `src/ccswitch/quota.py`
- Modify: `src/ccswitch/paths.py`
- Modify: `src/ccswitch/accounts.py`
- Modify: `src/ccswitch/oauth.py`
- Modify: `src/ccswitch/copilot.py`
- Modify: `src/ccswitch/ledger.py`
- Modify: `src/ccswitch/selector.py`
- Modify: `src/ccswitch/launcher.py`
- Modify: `src/ccswitch/cli.py`
- Modify: `server/api/accounts.py`
- Modify: `server/usage/poller.py`
- Modify: `web/src/api/types.ts`
- Modify: account/usage/selection tests throughout `tests/` and `server/tests/`

**Work:**

- [x] Introduce a versioned, non-secret `accounts.json` manifest under the existing account-store
  root. Dobby/primary is its only writer; runners read snapshots. Do not overload the cross-host
  multi-writer `ledger.json` with identity metadata. *(`src/ccswitch/account_registry.py` +
  `paths.accounts_manifest_path()`. `locked_manifest()` refuses to write unless
  `is_manifest_writer()` (reuses `CCSWITCH_DAEMON_ROLE`); a runner reconciles in memory only.
  Same flock + temp-file + `os.replace` discipline as `ledger.locked_ledger`.)*
- [x] Define entries with name, provider, engine, user/runner auth modes, enabled flag, credential
  authority, health metadata, a monotonic manifest revision, and non-secret generation/timestamps.
  Account names remain globally unique in v1. *(Plan vocabularies verbatim. `engine` is NULLABLE:
  the engine vocabulary's single definition is `server/sessions/engine.py`, which `src/ccswitch`
  cannot import — `null` means "resolve through `parse_agent_engine`", reusing that one default
  branch instead of copying the strings. `assert_no_secrets()` runs on every write.)*
- [x] Add an idempotent importer for existing Anthropic credential slots and Copilot token dirs.
  After import, `all_accounts()` and `provider_of()` read the manifest; filesystem discovery is
  callable only from migration/repair commands. Unknown names no longer fall back to Anthropic.
  *(`import_filesystem_accounts()` is setdefault-shaped and `locked_manifest` skips the write when
  the body is unchanged, so a re-scan is byte-identical and does not bump the revision. Discovery
  reachable from `ccswitch accounts import|show` and from the additive reconcile-on-read.
  `provider_of()` raises `UnknownAccountError`; `provider_or_none()` is the explicit
  degrade-to-None door.)*
- [x] Replace shared `Usage` with canonical `QuotaSnapshot`/`QuotaWindow`. Anthropic produces
  session+weekly; Copilot produces one premium window; no fabricated duplicate. *(`src/ccswitch/
  quota.py`; `oauth.Usage` DELETED, no adapter. `availability` (available|unknown|auth_error) is
  how a failed read is expressed — an unavailable snapshot has NO windows rather than a
  fabricated 0%.)*
- [x] Replace selector math with `binding_window(model)` and make engine/provider/model filtering
  happen before scoring. Preserve conservative unknown-usage behavior and Claude reserve semantics.
  *(`max(..., key=used_pct)` returns the FIRST maximal element, reproducing the old
  strictly-less pair math including its tie direction. `eligible_names()` runs first, so a
  filtered-out account is inapplicable, not busy, and never supplies `earliest_reset`. All five
  tiers pinned by `tests/test_selector_filtering.py`.)*
- [x] Version the cached quota blob in the ledger and migrate old Claude usage entries on read.
  Update the CLI/API/UI to enumerate windows. *(`"v": 2`; `ledger.usage_from_blob` is the single
  reader and migrates the live pre-versioning shape in place without a rewrite — including
  collapsing a legacy Copilot entry's duplicated dimensions back to one `premium` window.
  `web/src/lib/usageWindows.ts` is the browser-side single reader; `_usage_to_dict` emits
  `windows[]`.)*
- [x] Add mixed-engine, model-scoped, unknown, auth-error, reset-null, and old-ledger migration tests.
  *(`tests/test_selector_filtering.py`, `tests/test_quota.py`, `tests/test_ledger_usage_migration.py`,
  `tests/test_account_registry.py`, `web/src/lib/usageWindows.test.ts`.)*

**Verification:** run every account/usage/selection suite plus the complete backend and web gates.
This is deliberately one PR because leaving old and new usage contracts alive concurrently would
violate the single-source-of-truth rule.

---

## Phase 2 — Codex app-server protocol and process supervision

### Task 2.1: Typed JSONL client and pinned schema contract

**PR group:** `feat/codex-app-server-client`.

**Files:**

- Create: `server/codex/__init__.py`
- Create: `server/codex/schema.py`
- Create: `server/codex/protocol.py`
- Create: `server/codex/redaction.py`
- Create: `server/tests/fakes/fake_codex_app_server.py`
- Create: `server/tests/fixtures/codex_app_server/0.153.2/*.json`
- Create: `server/tests/test_codex_schema.py`
- Create: `server/tests/test_codex_protocol.py`
- Create: `scripts/refresh_codex_contract_fixtures.py`

**Work:**

- [x] Extract the minimal supported request/response/notification/server-request schema from the
  real generated app-server schema. Commit curated fixtures and their upstream hash, not secrets or
  user configuration.
- [x] Implement one async JSONL client with monotonically unique request ids, response futures,
  notification dispatch, server-request dispatch, timeout/cancellation, bounded queues, and strict
  EOF/process-exit behavior.
- [x] Require `initialize`/`initialized` before calls. Advertise `experimentalApi=true` only when
  external auth is enabled.
- [x] Validate every inbound/outbound supported DTO. Unknown notifications are redacted and counted;
  unknown server requests fail closed and never auto-approve.
- [x] Centralize token/account-id/prompt/output/command redaction. `repr`, exception messages, and
  structured logs must be secret-safe.
- [x] Test interleaving, partial lines, malformed JSON, duplicate ids, request timeout, cancellation,
  queue pressure, EOF, secret-shaped values, and schema drift.

### Task 2.2: Per-account runner process manager

**PR group:** `feat/codex-app-server-manager`.

**Files:**

- Create: `server/codex/process.py`
- Create: `server/codex/manager.py`
- Create: `server/codex/capabilities.py`
- Modify: `server/sessions/orchestrator.py` lifecycle only
- Modify: `server/ipc/daemon.py`
- Test: `server/tests/test_codex_process.py`
- Test: `server/tests/test_codex_manager.py`
- Test: `server/tests/test_codex_capabilities.py`

**Work:**

- [x] Resolve `CCSWITCH_CODEX_BIN`, execute `--version`, compare against the explicit supported
  version set/schema hash, and advertise a typed unavailable reason on mismatch.
- [x] Create a local `CODEX_HOME` per runner account with mode 0700. Assert it is not on a network
  filesystem before starting; never symlink it to the authority home.
- [x] Supervise one app-server per `(runner, OpenAI account)` and multiplex thread drivers on it.
  Serialize startup/login, ref-count active threads, idle-reap after the grace period, and bound
  crash restart with jitter/backoff.
- [x] On restart, reinitialize/relogin and notify thread drivers to recover through `thread/read`;
  never claim an in-flight turn succeeded.
- [x] Integrate manager drain after driver turns settle and before daemon shutdown completes.
- [x] Prove a Codex process crash cannot stop or degrade a Claude driver.

**Real read-only smoke:** opt-in initialize, external login, account/read, model/list, and
rateLimits/read against the supported binary. No turn yet.

---

## Phase 3 — Credential authority, OpenAI accounts, models, and quota

### Task 3.1: Dedicated ChatGPT credential-authority service

**PR group:** `feat/codex-credential-authority`.

**Files:**

- Create: `server/codex/auth_types.py`
- Create: `server/codex/auth_authority.py`
- Create: `server/codex/auth_client.py`
- Create: `server/codex/auth_store.py`
- Create: `server/codex/auth_server.py`
- Create: `server/codex/auth_main.py`
- Create: `deploy/ccswitch-codex-auth.service`
- Create: `deploy/ccswitch-codex-auth.sysusers`
- Test: `server/tests/test_codex_auth_authority.py`
- Test: `server/tests/test_codex_auth_rpc.py`
- Test: `server/tests/test_codex_auth_redaction.py`

**Work:**

- [x] Run the authority as its own systemd service on dobby under a dedicated `ccswitch-auth` OS
  user. Take an exclusive instance lock in its private state directory and fail a second authority
  start loudly; v1 has no automatic authority failover.
- [x] Give every OpenAI account an authority-only managed `CODEX_HOME` under
  `/var/lib/ccswitch-codex-auth/accounts/` (directories 0700; auth files 0600), outside the Claude
  account-store export and every runner mount. Prove the session-daemon user cannot traverse it.
- [x] Implement device-login start/status/cancel/logout and state transitions without returning
  tokens to the edge/browser.
- [x] Implement `get_envelope(account, minimum_generation)` and coalesced `refresh(account,
  rejected_generation)`. Exactly one refresh may run per account; stale rejection cannot overwrite
  or downgrade a newer generation.
- [x] Read the refreshed managed cache only through `auth_store.py`; validate required fields and
  immediately form an in-memory `CredentialEnvelope`. Never persist an envelope outside the
  authority cache.
- [x] Serve a separate versioned mTLS API whose route table contains only Codex-auth operations—no
  import or mounting of the session daemon's general `/rpc/{method}` dispatcher. Use distinct
  `codex-edge` and `codex-runner:<host>` client certificates and deny-by-default per-route ACLs:
  edge may administer login; runners may only request envelope/refresh for enabled accounts.
- [x] Load the listener only from `CCSWITCH_CODEX_AUTHORITY_LISTEN_ENDPOINT`,
  `CCSWITCH_CODEX_AUTHORITY_TLS_CERT`, `CCSWITCH_CODEX_AUTHORITY_TLS_KEY`,
  `CCSWITCH_CODEX_AUTHORITY_TLS_CLIENT_CA`, and
  `CCSWITCH_CODEX_AUTHORITY_CLIENT_ALLOWLIST`. Use authority-specific key material; never reuse the
  session-daemon server key or edge session-RPC client key. Fail startup on missing, unreadable, or
  non-mTLS configuration.
- [x] Add an admin-only edge→authority authorization sync containing only account name, provider,
  engine, enabled state, and monotonic manifest revision. Persist it as a private non-secret
  projection with a short lease; full-sync on edge startup and periodically. Fail issuance closed
  on expired sync, unknown account, disabled account, or revision rollback. Do not grant the
  authority user read/traverse access to the Claude account store.
- [x] Implement explicit revoke before disable/remove. Revoke increments the authority generation,
  rejects future envelope/refresh calls, and asks the edge to fan out manager invalidation to every
  runner so cached envelopes are dropped and the account's app-servers are stopped. Enable updates
  the canonical manifest first, then syncs authority; a failed sync stays safely unavailable.
- [x] Add negative integration tests proving a runner certificate cannot call login/logout, session
  create/delete, automation, deployment, or any arbitrary method path.
- [x] Audit only account alias, runner identity, action, outcome, generation, and timing. Hash raw
  native identifiers if correlation is required.
- [x] Test concurrent refresh storms, authority restart, duplicate-start/lock loss, expired sync
  lease, disable/remove revocation, stale runner cache invalidation, wrong account, wrong CN,
  untrusted client CA, missing/unreadable TLS material, revision/generation rollback, missing fields,
  401 loop, and log/exception redaction.

### Task 3.2: Runner external-token handshake and refresh callback

**PR group:** `feat/codex-runner-auth`.

**Files:**

- Create: `server/codex/external_auth.py`
- Modify: `server/codex/manager.py`
- Modify: `server/codex/protocol.py`
- Create: `server/codex/auth_endpoint.py`
- Test: `server/tests/test_codex_external_auth.py`
- Test: `server/tests/test_codex_multi_runner_auth.py`

**Work:**

- [x] Fetch an envelope over the dedicated authority mTLS client, call app-server external login,
  and discard request-local token strings after serialization.
- [x] Handle `account/chatgptAuthTokens/refresh` only when account, manager instance, and rejected
  generation match. Ask the authority for a newer generation and return the documented response
  inside `CCSWITCH_CODEX_AUTH_REFRESH_DEADLINE_S` (default 8.0 seconds total, including pool,
  connect, write, and read time).
- [x] Deduplicate refresh requests within the runner as well as at the authority. Bound retries and
  mark auth degraded after repeated rejection; never loop indefinitely.
- [x] Use native client timeout/deadline controls rather than an outer `asyncio.wait_for` that can
  strand a pooled connection. A timed-out waiter must detach safely from a shared coalesced refresh;
  the authority's upstream operation is itself bounded. Test a slow authority, pool starvation,
  callback cancellation, and a late response that must not answer an expired app-server request.
- [x] Prove no `auth.json` appears in runner homes and tokens are absent from DB, ledger, event ring,
  HTTP payloads, metrics, logs, tracebacks, and process arguments/environment.
- [x] Fail startup when experimental auth is disabled, schema capability is absent, or authority
  mTLS config is incomplete. Do not fall back to managed runner auth.

### Task 3.3: OpenAI account, model, and quota APIs

**PR group:** `feat/openai-account-control-plane`.

**Files:**

- Modify: `src/ccswitch/account_registry.py`
- Create: `server/codex/catalog.py`
- Create: `server/codex/quota.py`
- Modify: `server/api/accounts.py`
- Modify: `server/api/models.py`
- Modify: `server/sessions/facade.py`
- Modify: `server/sessions/remote.py`
- Modify: `server/ipc/orchestrator_rpc.py`
- Test: `server/tests/test_accounts_api_openai.py`
- Test: `server/tests/test_codex_catalog.py`
- Test: `server/tests/test_codex_quota.py`
- Test: `server/tests/test_api_models.py`

**Work:**

- [x] Add `/api/accounts/add/openai` start/status/cancel, provider-dispatched reauth/logout/remove,
  and manifest writes only after successful login. Preserve name validation/global uniqueness.
- [x] Route OpenAI enable/disable/remove through the safe authority ordering: revoke and fan out
  runner-manager invalidation before disabling/removing; manifest-first then authority sync for
  enable. Surface partial failures as degraded and retryable, never as silently enabled.
- [x] Return provider, engine, user/runner auth modes, authority health, active runner count, and
  typed quota windows—never native account id or tokens.
- [x] Make `GET /api/models?engine=&account=` canonical. Claude delegates to its existing catalog;
  Codex calls `model/list` and returns supported efforts/modalities/defaults with a short cache keyed
  by account, CLI version, and effective config.
- [x] Map all app-server rate-limit buckets into quota windows, including null resets and
  model-scoped buckets. Cache last-good with age; distinguish unknown, unsupported, and auth error.
- [x] Validate model+effort as one pair at dispatch. Refresh once on drift, then return a typed 409
  with current choices; silently substitute only when the request says engine default.

---

## Phase 4 — Interactive Codex runtime

### Task 4.1: Codex-to-ccswitch event translation

**PR group:** `feat/codex-event-adapter`.

**Files:**

- Create: `server/codex/translator.py`
- Modify: `server/codex/schema.py` (the thread/turn/item vocabulary the translator parses)
- Create: `server/tests/fixtures/codex_app_server/transcripts/*.jsonl`
- Test: `server/tests/test_codex_translator.py`
- *Not modified, as it turned out:* `server/sessions/events.py`, `server/sessions/status.py`,
  `web/src/session/events.ts`, `web/src/session/reducer.ts`, transcript components and
  `web/src/session/reducer.codex.test.ts` — the normalized vocabulary did not expand, so there was
  nothing for the web to learn. See the Work outcome below.

**Work:**

- [x] Golden-map thread/turn notifications and supported items to `TurnStart`, `TextDelta`,
  `ThinkingDelta` (summary only), `ToolCall`, `ToolResult`, `PermissionRequest`, `Status`, `Error`,
  and `TurnEnd`.
- [x] Add only genuinely new normalized activity kinds needed for command executions, file changes,
  MCP calls, web searches, and collaboration items. Do not expose Codex wire names to React.
  **Outcome: none were needed.** Every Codex item maps onto an existing `events.py` member and an
  existing `ToolKind`, usually a `ToolCall`/`ToolResult` pair whose `name` is the Claude tool name
  the presenter already renders. The web reducer therefore needed no change in this phase.
- [x] Deduplicate deltas versus final items, preserve ordering/correlation, and redact raw commands,
  patches, paths, prompts, output, and native ids from ordinary logs. The translator has no logger,
  no I/O and no clock at all, which is a stronger guarantee than per-call-site redaction.
- [x] Map token usage, context window, completion/no-PR markers, rate limits, cancellation, and
  failures without Anthropic-specific messages. `turn/completed` carries no usage, so `TurnEnd.usage`
  comes from the last `thread/tokenUsage/updated` **keyed by its own turn id** — a frame naming
  another turn is not billed to the live one.
- [x] Unknown item types create a safe adapter notice/metric and do not crash the session. Unknown
  approval requests fail closed.

### Task 4.2: Codex thread driver and approval bridge

**PR group:** `feat/codex-agent-driver`.

**Files:**

- Create: `server/codex/driver.py`
- Create: `server/codex/permissions.py`
- Create: `server/sessions/driver_stream.py` (`DriverStream` extracted from `claude_driver.py`;
  both engines need its atomic-open semantics and a second copy would drift)
- Modify: `server/sessions/engine.py`, `server/sessions/permissions.py`,
  `server/sessions/completion.py`, `server/codex/manager.py` (per-thread stream routing)
- Test: `server/tests/test_codex_driver.py`, `test_codex_approvals.py`,
  `test_codex_permissions.py`, `test_permission_registry_external.py`,
  `test_codex_manager_routing.py`
- *Not created:* `server/codex/approvals.py` — see the Work note on reusing `PermissionRegistry`.

**Work:**

- [x] Implement thread start/read/resume, turn start/steer/interrupt, event fan-out, model/effort
  settings, and clean stop against the shared per-account manager. A mid-turn prompt is a
  `turn/steer` fenced by `expectedTurnId`, falling back to the queue only if the turn ended first.
- [x] Map common permission profiles to documented Codex sandbox/approval policy. Keep engine
  descriptions explicit; never claim exact semantic parity with Claude modes. The three tool-scoped
  middle modes (`allowlist`/`auto`/`acceptEdits`) are inexpressible in Codex's per-command model and
  collapse onto the strict profile rather than widening to `on-request`.
- [x] Register every server-initiated approval by `(ccswitch session, native thread, request id,
  authenticated user, expiry)`. Validate ownership/pending/decision before replying. **Deviation:
  no `server/codex/approvals.py`.** `PermissionRegistry` already owns exactly this, and the whole
  pipeline above it is engine-neutral, so it gained `request_external` instead of growing a second
  registry with a second set of WebSocket frames and a second UI card.
- [x] Support allow/decline and command amendment only where offered. “Always allow” is allowed only
  for a native session-scoped decision; never create a persistent global exception. `request_external`
  writes NO entry in the ccswitch grant cache: that cache is never consulted for an engine that
  enforces approvals itself, so an entry there would be a second, invisible policy.
- [x] Decline/cancel pending requests during stop, crash, auth loss, or session archive so no future
  remains stranded.
- [x] Satisfy the canonical `AgentDriver` contract and capability tests without engine conditionals
  in shared UI/session code. Members Codex cannot support are documented refusals, not silent stubs.

### Task 4.3: Orchestrator lifecycle, replay, and recovery

**PR group:** `feat/codex-session-orchestration`.

**Files:**

- Modify: `server/sessions/orchestrator.py`
- Modify: `server/sessions/replay.py` into an engine facade with Claude and Codex adapters
- Modify: `server/sessions/facade.py`
- Modify: `server/sessions/remote.py`
- Modify: `server/ipc/orchestrator_rpc.py`
- Modify: `server/bridge/ws.py`
- Test: `server/tests/test_codex_orchestrator.py`
- Test: `server/tests/test_codex_replay.py`
- Test: `server/tests/test_codex_recovery.py`
- Test: affected WS gap/reconnect/drain/archive suites

**Work:**

- [ ] Pass engine/model/effort/account into the canonical factory; ensure worktree and attachment
  paths remain inside the assigned cwd before app-server sees them.
- [ ] Persist the native thread id from the first authoritative response. Never mix an id from a
  different engine/account/session.
- [ ] Make replay a facade: Claude retains JSONL replay inside its adapter; Codex uses `thread/read`
  and translates the returned items. Preserve existing window/cursor semantics at the browser
  boundary.
- [ ] Rehydrate Codex rows on the owning runner, restart the account manager if needed, read the
  thread, and mark ambiguous in-flight turns interrupted/needs-attention rather than successful.
- [ ] Implement archive/delete without parsing Codex private state. Archive removes ccswitch
  ownership; any native delete operation is used only if documented and explicitly required.
- [ ] Keep adopt, transcript-file lookup, Claude rotation, self-model switching, and Claude
  subagent recovery capability-gated to Claude.
- [ ] Run disconnect, ring-gap, daemon-drain, process-crash, authority-loss, host-loss, and repeated
  restart tests.

---

## Phase 5 — Fleet routing and browser experience

### Task 5.1: Runner capabilities and engine-aware dispatch

**PR group:** `feat/fleet-engine-capabilities`.

**Files:**

- Modify: `server/ipc/daemon.py`
- Modify: `server/sessions/host_probe.py`
- Modify: `server/sessions/fleet_router.py`
- Modify: `server/sessions/fleet_orchestrator.py`
- Modify: `server/api/fleet.py`
- Modify: `server/db/models.py` (only if capability snapshots are persisted)
- Add migration if the host schema changes
- Modify: `web/src/api/fleet.ts`
- Modify: `web/src/components/settings/FleetSection.tsx`
- Test: fleet/probe/protocol/skew suites

**Work:**

- [x] Add `/info.engines` with availability, CLI version, app-server schema hash, and external-auth
  compatibility. An absent field means legacy Claude-only during the one-window upgrade.
- [x] Filter candidates by requested engine and required protocol/schema before capacity scoring.
  `prefer=host` is a preference, not permission to dispatch to an incompatible host.
- [x] Allow the same OpenAI account on multiple eligible runners; add no account affinity/lease.
  Continue stamping one owning host per session.
- [x] Show Codex availability/reason in Fleet settings. A Codex mismatch must leave host health and
  Claude dispatch intact.
- [x] Test mixed-version rollout, missing binary, schema mismatch, authority unavailable, host
  preference, all-incompatible error, and concurrent same-account dispatch.

**As built** (2026-09-05, one PR covering all three Phase 5 groups):

- `server/sessions/host_engines.py` is the single parser + verdict for engine advertisement,
  consumed by the daemon writer, the probe, the fleet candidate filter, the live pre-dispatch
  gate, and both API surfaces. The compatibility default is applied there and nowhere else, so
  removing it in the interactive-v1 cleanup PR is a one-file change.
- Two gates, one parser: the persisted `Host.engines` snapshot filters candidates (no RPC per
  host during ranking), and the chosen daemon's live `/info` is re-checked before a non-Claude
  launch. Claude never pays for the round trip.
- `detail` is deliberately absent from the advertisement: `CodexAvailability.detail` is scrubbed
  to a fixed `[REDACTED]` on construction, so the typed `reason` is the whole contract.
- `NoEngineHostAvailable` subclasses `NoHostAvailable` (every existing handler still catches it)
  and carries per-host reasons, surfaced as HTTP 409 `no_engine_host_available`.
- `/api/config.engines` is where the browser learns which agents to offer, including the
  edge-side `blockedReason: "authority_unconfigured"`.
- Fixed on the way through: `app.state.orchestrator` was never set on the daemon app, so
  `/info.activeSessions` always reported 0 and the ranker's headroom term was dead weight.

### Task 5.2: Engine-aware account and launch UI

**PR group:** `feat/codex-account-launch-ui`.

**Files:**

- Modify: `web/src/api/types.ts`
- Modify: `web/src/api/hooks.ts`
- Modify: `web/src/components/AddAccountDialog.tsx`
- Modify: `web/src/components/AccountsManager.tsx`
- Modify: `web/src/components/DispatchBar.tsx`
- ~~Modify: `web/src/features/sessions/NewSessionDialog.tsx`~~ — retired in a36c49c3; see below
- ~~Modify: `web/src/features/sessions/NewSessionDialogHost.tsx`~~ — retired in a36c49c3
- Modify: related account/dispatch/new-session tests
- Add targeted a11y coverage in `web/src/components/__tests__/a11y.test.tsx`

**Work:**

- [x] Add an Agent selector with explicit `Claude Code` and `Codex`; default to the persisted user
  choice only after the user has made one, otherwise preserve Claude Code for upgrade safety.
- [x] Filter account/model/host choices by engine. Changing engine clears incompatible account,
  model, and reasoning selections visibly.
- [x] Query models with engine/account context. Show reasoning only when the selected model offers
  choices and initialize it to the advertised default.
- [x] Show the common permission profile plus expandable engine-specific sandbox/approval details.
- [x] Add OpenAI device-code enrollment/status/cancel/reauth/remove UI. Never place device or token
  material in query-cache persistence, analytics, or error telemetry.
- [x] Render quota windows generically and show authority health/active runner count. Keep Anthropic
  extras/reserve controls provider-specific.
- [x] Update optimistic session placeholders with engine so list cards never briefly mislabel Codex
  as Claude.
- [x] Verify keyboard flow, screen-reader labels/live states, 44px mobile targets, narrow layout,
  and reduced motion.

**As built:**

- **The plan's two dialog files no longer exist.** `a36c49c3` retired the New Session dialog in
  favour of one create control in the prompt dock, so the launch UI is
  `PromptDock → NewSessionBar → DispatchBar → useCreateSession` and that is where the Agent and
  reasoning selectors landed.
- `useEngineOffers()` is a SELECTOR over the existing `/api/config` query, not a second query;
  `useEngineCapabilities()` sits beside it so every capability-gated surface reads one hook.
- The engine choice is **derived, not stored**: a persisted choice the server has withdrawn falls
  back to Claude Code on every render.
- `CopilotAddState` was renamed `DeviceCodeState` and the panel extracted to `DeviceCodePanel`,
  rather than adding an OpenAI twin — the backend made the two payloads identical on purpose.
- Reauth for an existing OpenAI account runs **in place** in `AccountsManager`, not through
  `AddAccountDialog`: `POST /accounts/add/openai` 409s on an existing name.

### Task 5.3: Session identity and Codex activity UI

**PR group:** `feat/codex-session-ui`.

**Files:**

- Modify: `web/src/components/SessionCard.tsx`
- Modify: `web/src/features/sessions/SessionHeader.tsx`
- Modify: `web/src/components/session/SessionMeta.tsx`
- Modify: `web/src/components/session/ToolCallCard.tsx`
- Modify: `web/src/components/session/GenericResult.tsx`
- Modify: `web/src/components/session/ModelSwitchDialog.tsx`
- Modify: `web/src/session/modelName.ts` into an engine-aware display helper
- Modify: related component/reducer/runtime tests

**Work:**

- [x] Show agent, provider/account alias, runner, model, and reasoning effort in session identity
  surfaces without crowding mobile cards.
- [x] Render normalized command/file/MCP/search/collaboration activities using existing transcript
  primitives. Add a component only where an existing generic/tool card cannot truthfully render it.
- [x] Hide unsupported Claude-only commands/actions for Codex using server capabilities.
- [x] Route approvals through the existing pending-permission UX while displaying the exact Codex
  decision choices. Reconnect must not resurrect resolved requests.
- [x] Add visual/a11y regression tests for mixed Claude/Codex session lists and long account/model
  names.

**As built:**

- `modelName.ts` gained `modelFamily()` plus an OpenAI formatter; Claude behaviour is unchanged and
  `isGatedTier` is explicitly false for a non-Claude engine (the gate it mirrors only matches
  Claude ids). `engineBadge()` returns null for `claude_code`, so "only chip a non-default engine"
  has one implementation across card, header and meta.
- **No new event kinds and no new transcript component.** Two truthfulness gaps were closed in the
  existing primitives instead: `toolSummary` (`web/src/session/toolSummary.ts`) is now THE one-line
  tool summary for both the transcript card and the approval card — they had a copy each and had
  already drifted, so a Codex `Edit` summarised in the transcript and not on the card an operator
  has to act on. `DiffView` lists a Codex `changes: [{path, kind}]` rather than rendering the empty
  diff `buildLines` produces for it.
- Approvals needed no new vocabulary: `CodexAgentDriver` already normalizes an approval onto the
  same permission event, and ccswitch's allow/deny/always is exactly what it relays as the
  app-server's `accept`/`decline`/`acceptForSession`.
- `canUseEngine` lives in `web/src/session/engineCapabilities.ts`, NOT in `@/api/hooks`: ~50 suites
  replace that module with a hand-written `vi.mock` factory, and anything exported from it must be
  restated in every one of them.

---

## Phase 6 — Interactive v1 hardening and launch

### Task 6.1: Operational install, security, and observability

**PR group:** `ops/codex-runner-support`.

**Files:**

- Create: `scripts/fetch-codex.sh` or document a vetted package install if no repository script is needed
- Modify: `deploy/ccswitch-daemon.service`
- Modify: `deploy/ccswitch-daemon-remote.service`
- Modify: `deploy/README.md`
- Modify: `docs/runbooks/onboarding-a-session-runner.md`
- Modify: `docs/runbooks/rocky-session-host.md`
- Create: `docs/runbooks/codex-credential-authority.md`
- Modify: health/doctor/metrics modules and tests

**Work:**

- [ ] Pin and verify the supported Codex CLI distribution, checksum/signature where available, and
  license. A host lacking it advertises unavailable instead of failing daemon boot.
- [ ] Document separate authority and runner homes, permissions, backup/restore, reauth, cert minting,
  runner client CNs, version upgrade, rollback, and kill-switch procedures.
- [ ] Provision `ccswitch-auth` declaratively and prove the `tony` session-daemon user cannot read
  the authority state. The service gets only its private state, Codex binary, network, and required
  certificate access—not repository/worktree write access.
- [ ] Provision authority-specific server key/certificate and client-verification CA with
  least-privilege ownership for `ccswitch-auth`; provision separate `codex-edge` and
  `codex-runner:<host>` client certificates plus the authority server CA on their callers. Validate
  SAN/hostname, expiry, file modes, untrusted-client rejection, and rotation independently from the
  session-daemon PKI.
- [ ] Add startup doctor checks for binary version/schema, local filesystem, authority reachability,
  mTLS identity, manifest entry, and external-auth capability. Never print token-bearing values.
- [ ] Add metrics for engine/provider/host/version/method latency, active threads/turns, restarts,
  auth generation/health, quota age, and approval latency. Hash native ids; exclude content.
- [ ] Exercise secrets scanning against logs, crash traces, `/info`, audit rows, and support bundles.

### Task 6.2: Real interactive acceptance and controlled flag enablement

**PR group:** `feat/codex-interactive-v1-launch`.

**Files:**

- Create: `scripts/smoke_codex_interactive.py`
- Create: `docs/superpowers/spikes/2026-09-04-codex-interactive-acceptance.md`
- Modify: `docs/product/features/openai-codex-integration.md` → `state: shipped` only after pass
- Modify: accepted design/plan checkboxes and runbooks
- Remove: one-window missing-engine compatibility default

**Work:**

- [ ] Gate the script on `CCSWITCH_TEST_CODEX_INTERACTIVE=1` and an explicit test account/repo.
- [ ] On dobby and rocky, run: add/reauth read, simultaneous local managers for the same account,
  model+effort selection, two minimal threads, one minimal tool-free turn, safe command approval
  decline, steer, interrupt, browser reconnect, `thread/read` replay, manager crash/restart, daemon
  drain/rehydrate, archive, and quota refresh.
- [ ] Confirm a Claude and Copilot session remain usable during Codex crash/auth/version failures.
- [ ] Confirm runner homes contain no `auth.json`, authority refresh count coalesces during a 401
  storm, and no secret appears in gathered logs/audit/metrics.
- [ ] Run the entire backend gate, real Postgres suite, entire frontend gate, Playwright session
  flows, and prompt evals; attach results to the acceptance doc.
- [ ] Enable `CCSWITCH_CODEX_EXTERNAL_AUTH_ENABLED` and `CCSWITCH_CODEX_ENABLED` in the private
  deployment only after all gates pass. Keep automation flag false.
- [ ] Remove upgrade-only engine defaults/deprecations and close the interactive-v1 project issue.

**Full gates:**

```bash
ruff check src/ server/
mypy server/ --ignore-missing-imports
PYTHONPATH=$PWD/src pytest tests/
env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_SESSIOND_ENDPOINT \
    -u CCSWITCH_SESSIOND_CLIENT_CERT -u CCSWITCH_DAEMON_ROLE \
    CCSWITCH_DEV=1 pytest server/tests/ -n auto --dist loadfile --timeout=300 \
    2>&1 | tee /tmp/codex-v1-server-suite.log
(cd web && npm run typecheck && npm run lint && npm run test && npm run build)
(cd web && npm run test:e2e)
CCSWITCH_TEST_PG_URL="$CCSWITCH_TEST_PG_URL" pytest server/tests/test_postgres_compat.py
python scripts/run_evals.py
```

Expected: every gate passes; the acceptance record identifies binary/schema versions and runner
hosts but contains no account ids, tokens, prompts, commands, patches, or raw native thread ids.

---

## Phase 7 — Required backlog and SDLC automation fast follow

### Task 7.1: Provider-neutral automation launch and completion contracts

**PR group:** `refactor/automation-engine-policy`.

**Files:**

- Modify: `server/db/models.py` (`Automation` configuration row)
- Modify: `server/automations/config_store.py`
- Modify: `server/automations/base.py`
- Modify: `server/automations/context.py` (the canonical spawn helper)
- Modify: `server/sessions/completion.py`
- Modify: `server/sessions/prompt_refiner.py`
- Modify: `server/sessions/title_generator.py`
- Modify: `server/sessions/goal_inference.py`
- Modify: `server/api/automations.py`
- Modify: `web/src/api/automations.ts`
- Modify: `web/src/automations/AutomationsTab.tsx`
- Modify: `web/src/components/AutomationCard.tsx`
- Modify: related automation configuration tests

**Work:**

- [ ] Add explicit `engine_policy = claude_only | codex_allowed | codex_only`; migrate all existing
  origins to `claude_only`. There is no implicit `any`.
- [ ] Make the one automation spawn helper resolve engine/account/model/effort/capabilities and pass
  them to normal session creation. Do not build a Codex-only automation spawner.
- [ ] Generalize completion/no-PR markers and error classes at the normalized event boundary.
  Engine adapters own detection details; loops consume one result contract.
- [ ] Audit helper model calls (assessment, refinement, title, goal, summaries). Leave each
  Claude-only or explicitly give it an engine-aware implementation; do not silently route it to the
  automation's account.
- [ ] Add config/API/UI migration and negative tests proving unaudited origins cannot choose Codex.

### Task 7.2: Backlog runner Codex compatibility

**PR group:** `feat/backlog-runner-codex`.

**Files:**

- Modify: `server/automations/backlog_runner.py`
- Modify: backlog selection/spawn/action modules under `server/automations/`
- Modify: `server/sessions/completion.py` only through the shared contract from Task 7.1
- Test: `server/tests/test_backlog_runner_automation.py`
- Test: affected automation spawn/action/reconciliation suites
- Create: `server/tests/test_backlog_runner_codex_acceptance.py`

**Audit and work:**

- [ ] Verify the Codex prompt contract contains the issue scope, repo/worktree rules, required
  tests, PR completion marker, no-PR behavior, and permission tier without Claude-only syntax.
- [ ] Test claim → spawn → active reconciliation → PR association → completion/archive, plus
  rate-limit/auth failure, approval wait, daemon restart, no-PR completion, and needs-human give-up.
- [ ] Verify shared-account concurrent backlog sessions on different runners do not collide in auth,
  branch/worktree, origin refs, or quota handling.
- [ ] Run one opt-in low-risk repository issue through a Codex session and record sanitized evidence.
- [ ] Change only backlog runner's policy to `codex_allowed` after its acceptance suite passes.

### Task 7.3: AI-native SDLC Codex compatibility and initiative closeout

**PR group:** `feat/sdlc-codex`.

**Files:**

- Modify: `server/automations/sdlc_intake.py`
- Modify: `server/automations/sdlc_synthesizer.py`
- Modify: `server/sdlc/prompts.py`
- Modify: `server/automations/registry.py`
- Modify: `server/sessions/orchestrator.py` (existing SDLC loop wiring)
- Modify: `server/sdlc/governance.py` only if a provider-neutral mapping gap remains
- Test: `server/tests/test_sdlc_intake_loop.py`
- Test: `server/tests/test_sdlc_synthesizer_loop.py`
- Test: `server/tests/test_sdlc_prompts.py`
- Test: `server/tests/test_sdlc_wiring.py`
- Create: `server/tests/test_sdlc_codex_acceptance.py`
- Modify: `docs/product/features/openai-codex-integration.md`

**Audit and work:**

- [ ] Audit intake/design/plan artifact prompts for Claude-specific commands, skills, hooks,
  completion markers, and review assumptions. Provide Codex-native wording while sharing one
  semantic template/data model.
- [ ] Map environment permission tiers to Codex sandbox/approval policy without weakening release
  separation of duties or managed deny rules.
- [ ] Test propose-only human gates, artifact PR creation, restart/resume, approval waits, failure
  counters, loop guards, build handoff to backlog runner, and a mixed-engine initiative chain.
- [ ] Run one opt-in initiative through at least intent → design → plan using Codex, keeping every
  human gate intact. Record sanitized acceptance evidence.
- [ ] Mark only the audited SDLC origins `codex_allowed`, enable
  `CCSWITCH_CODEX_AUTOMATIONS_ENABLED`, update the feature state to include automation parity, and
  close the epic only when Project #26 dependencies show both interactive v1 and these fast-follow
  tasks complete.

---

## Per-PR review checklist

- [ ] Scope matches one PR group and its Project #26 issue; unrelated dirty work is untouched.
- [ ] Existing source-of-truth search recorded; replaced paths removed in the same PR.
- [ ] Tests were observed failing before implementation and now pass.
- [ ] Engine/provider/account/native-id values are validated at every boundary.
- [ ] No token, raw native account/thread id, prompt, model output, command, or patch enters routine
  logs, metrics, errors, audit detail, event ring, browser payload, or test fixture.
- [ ] Codex failure is isolated from Claude/Copilot availability.
- [ ] New RPC methods have mTLS identity/authorization, deadline, backpressure, error mapping, and
  rolling-version tests.
- [ ] Any schema change has ORM, Alembic, reconcile/backfill, SQLite, and Postgres coverage.
- [ ] Any UI change passes keyboard/mobile/WCAG checks and the full Vitest suite.
- [ ] Boundary behavior has real-binary evidence when required by Constitution III.
- [ ] Product feature doc, accepted design, plan checklist, runbook, and GitHub issue state agree.

## Definition of done

Interactive v1 is done only when a user can enroll a named ChatGPT account, explicitly choose
Codex, select a live model/reasoning effort/permission profile, dispatch to either compatible
runner, use the same account concurrently on both, interact and approve safely, reconnect/replay,
recover from app-server/daemon restart, inspect all quota windows, and archive—while Claude and
Copilot remain unaffected and the authority alone retains refresh credentials.

The overall initiative is done only after the backlog runner and agreed AI-native SDLC origins have
passed their Codex-specific acceptance suites and are enabled through explicit `engine_policy`.
API-key authentication, automatic OpenAI-account rotation, Codex Cloud tasks, service-tier controls,
collaboration-mode controls, arbitrary session adoption, and config/plugin management remain
separate post-v1 work.
