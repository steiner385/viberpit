# Session tool surface + session feedback → ccswitch backlog — design

Status: design, awaiting owner review. Date: 2026-09-06 (rev 2: tool surface instead of a marker).

Two things, in dependency order:

1. **A per-session ccswitch MCP surface for coding sessions.** Every coding session (not just
   the assistant drawer) mounts an in-process `ccswitch` MCP server whose tools act on *this*
   session, in the daemon that owns it. No HTTP hop, no token, works on any fleet host.
2. **Session feedback as its first tool.** A session that sees ccswitch itself misbehave — a
   contradictory instruction, an ambiguous dispatch prompt, an ill-timed interrupt or model
   switch, a broken worktree or missing credential — calls `report_feedback`, and the report
   lands, deduplicated and rate-limited, as a labeled issue in ccswitch's own backlog.

The owner's direction (2026-09-06): a tool surface is cleaner than the HTML-comment markers
ccswitch has used for session→harness signalling so far. This spec builds the surface so the
existing markers *can* migrate onto it later (§11), but migrating them is out of scope here.

## 1. Goal and non-goals

**Goal.** Give every coding session, on any host, a typed, acknowledged way to talk to ccswitch
about itself; make the first use of it (feedback) durable, attributable, deduplicated, and
visible in the backlog within one automation tick.

**Non-goals (this spec).**

- Migrating `ccswitch:complete`, `ccswitch:no-pr`, `ccswitch:model=` to tools (§11).
- User→assistant "thumbs" feedback (#1246, a different direction).
- Reports about the user's own project. The instruction and tool description say not to;
  triage catches the rest.
- Auto-implementing a report. A filed issue is *not* auto-picked by the Backlog Runner (§7).
- A rich feedback UI. First cut: a list endpoint and the automation card (§8).

## 2. Why a tool surface, and what already exists

The only in-process MCP server today is the assistant's (`server/assistant/server.py`). Its
tools are thin adapters over ONE edge API endpoint each, called over loopback with a
session-bound `ccas1:` bearer token. That design is right for the drawer, which acts *as the
user* against the API, and it is why assistant sessions are pinned to the primary host
(`fleet_orchestrator.py`): a worker daemon has no edge API on its loopback.

Coding sessions are placed on any healthy host, so the session surface cannot reuse that hop.
It does not need to: the MCP server runs inside the daemon that owns the session, which already
holds the `SessionOrchestrator`, the shared DB, and the audit writer. Session tools therefore
close over an in-process **`SessionToolContext`** and call orchestrator seams and the DB
directly. No token to mint, verify, or re-mint; nothing to pin.

What the surface reuses unchanged:

- `create_sdk_mcp_server` (Claude) and `dynamicTools` on `thread/start` (Codex) as transports.
- The `ToolSpec` / `ToolClass` registry pattern and the `guarded` wrapper shape (audit +
  metric + never-raise) from `server/assistant/tools.py`.
- `PermissionRegistry.request`'s `mcp__ccswitch__*` branch, which is annotation-driven and
  kind-agnostic: READ passes silently in every mode, MUTATION prompts once with a grant cache,
  DESTRUCTIVE always prompts. A coding session in `acceptEdits` or `default` gets correct card
  behaviour with no permission work.
- The overlay registry + `merge_options_overlay` path that already feeds an assistant's
  `ClaudeAgentOptions`, generalized to any kind (§3.3).

## 3. The session tool surface

### 3.1 Package and registry

New package `server/sessions/tools/`:

- `spec.py` — nothing new: re-exports `ToolSpec`, `ToolClass` from a new neutral module
  `server/mcp_spec.py`, which becomes THE home of `ToolClass`, `ToolSpec`, `MCP_PREFIX`
  (`mcp__ccswitch__`), `SERVER_NAME` (`ccswitch`) and a surface-aware
  `annotation_for(mcp_name)`. `server/assistant/tools.py` imports these instead of defining
  them, collapsing the five literal copies of the prefix the rebrand spec counted into one.
  `annotation_for` consults every registered surface; the permissions branch calls it via the
  neutral module rather than `server.assistant.tools`.
- `registry.py` — `SESSION_TOOL_SPECS: dict[str, ToolSpec]`, registered as a surface at import.
- `context.py` — `SessionToolContext` (frozen): `session_id`, `identity` (the owner), `kind`,
  `engine`, a `record()` accessor returning the live `SessionRecord`, and the seams a tool may
  call. Built by the orchestrator per driver build (§3.3).
- `tools.py` — `build_session_tools(ctx) -> list[SdkMcpTool]` with the `guarded` wrapper:
  audit `session.tool_call` (`via="session"`), metric `session_tool_calls_total{tool,class,
  outcome}`, every exception mapped to an error result.
- `server.py` — `build_session_server(ctx)` → `create_sdk_mcp_server(name=SERVER_NAME,
  version=running_git_sha()[:12], tools=...)`.
- `codex.py` — the Codex twin: `FunctionDynamicToolSpec`s + handlers via the same
  `input_schema_for` translation the assistant uses, as a `CodexSessionOverlay`.

Both the assistant and the session surface use the wire name `ccswitch` and prefix
`mcp__ccswitch__`. They never coexist in one session (a session has exactly one kind), so the
shared name is fine and keeps the permission branch, `ev.tool_kind`, and the UI's app-action
rendering unchanged. Tool names are kept globally unique across the two surfaces anyway.

### 3.2 v1 tools

| Tool | Class | Does |
|---|---|---|
| `session_context` | READ | Returns what ccswitch knows about *this* session: id, name, repo, branch, cwd, host, engine, model, origin automation, `origin_ref`, effective goal, permission mode. |
| `report_feedback` | READ | Records a report about ccswitch (§4). READ because, like the assistant's `report_gap`, it changes nothing a user can see — a row, an audit event, a counter. No card, in any mode. |

`session_context` is the cheap second tool that makes the surface a surface: sessions are
currently told nothing about how they were dispatched, and a feedback body that can cite
"origin=backlog-runner, origin_ref=KinDash#123" is worth far more than one that cannot.

### 3.3 Mounting — Claude

Generalize the assistant's overlay plumbing rather than add a parallel one:

- `register_assistant_overlay` → `register_overlay(session_id, overlay)`; `_assistant_overlays`
  → `_overlays`. Same registry, same `_session_factory` pickup, same "dict means Claude" rule.
- `_setup_assistant_overlay` keeps its body and gains a sibling `_setup_session_overlay(
  session_id, *, identity, engine)` for coding kinds; one dispatcher `_setup_overlay_for_kind`
  is what the three driver-build sites call: `_spawn_session`, `ensure_driver`, and
  `_reassign_locked`. `adopt` is exempt (external process). The fix-pattern doc
  `docs/superpowers/fix-patterns/2026-09-04-per-process-state-armed-only-on-the-create-path.md`
  is the reason there is one helper and three callers, not three copies.
- Release folds into `_teardown_driver_state`, which all three teardown paths already use.
- The coding overlay is exactly `{"mcp_servers": {"ccswitch": server}}`. It must **not** set
  `strict_mcp_config` (that would drop the repo's own `.mcp.json` servers, which
  `enableAllProjectMcpServers` pre-trusts today) and must **not** set `system_prompt` (the
  factory's preset + `marker_instructions()` append carries the completion / no-PR / model
  marker contracts). A test pins both absences.
- The system-prompt append gains a short `SESSION_TOOLS_INSTRUCTION` (composed next to the
  marker instructions in `claude_sdk_factory._build_append_string` and Codex's
  `developer_instructions`): the `ccswitch` tools exist, what `report_feedback` is for and is
  not for, and "keep working; a report never waits for a reply". Detail lives in the tool
  descriptions, where the model reads it at call time.
- Gate: `CCSWITCH_SESSION_TOOLS_ENABLED` (default `true`). Off = no server, no instruction.

### 3.4 Mounting — Codex

`thread/start` takes `dynamicTools`; the pinned `thread/resume` does not, which is why an
assistant thread is always started fresh. A coding session cannot pay that price (its model
context is the work), so:

- A fresh Codex coding session starts with the session tools declared.
- A **resumed** Codex coding session keeps its thread and has no session tools until the
  contract gains them. Accepted gap for v1, tracked as an issue.
- First implementation task is a spike against the real binary (constitution III; memory
  `codex-request-user-input-declared-not-absent`: a "not on the pinned contract" comment has
  been wrong before) to check whether `thread/resume` silently accepts `dynamicTools` or the
  binary has grown a `thread/tools/set`-style method. If either exists, the gap closes in the
  same PR.

### 3.5 Real-binary verification

Two gated integration tests (`CCSWITCH_TEST_CLAUDE_MCP=1`, like the STT one) against the real
`claude` binary: (a) a coding session with a project `.mcp.json` server sees BOTH that server
and `mcp__ccswitch__session_context`; (b) `report_feedback` from a session in `default` mode
produces no permission card. Unit tests cannot prove either.

## 4. `report_feedback` — the contract

Input schema:

| Field | Values | Rule |
|---|---|---|
| `category` | `prompt` `dispatch` `orchestration` `environment` `tooling` `other` | required; unknown → `ToolArgError` |
| `impact` | `blocked` `degraded` `minor` | required |
| `title` | string | trimmed, 8–120 chars |
| `body` | string | trimmed, ≤4000 chars; what happened, what was expected, what ccswitch should change; quote the exact text when the problem is an instruction |
| `evidence` | string, optional | ≤1000 chars; the offending prompt excerpt, command, or error |

Categories, as the description lists them:

- `prompt` — ccswitch-authored instruction text (system-prompt append, tool descriptions,
  automation seed prompts) is wrong, contradictory, or unclear.
- `dispatch` — the initial prompt / goal / issue / repo / branch ccswitch chose was wrong or
  ambiguous.
- `orchestration` — ccswitch acted on the session unhelpfully: interrupt, model or account
  switch, question, archive, reattach at a bad moment.
- `environment` — worktree, env vars, credentials, or tools ccswitch should have provided are
  missing or broken.
- `tooling` — a ccswitch-provided tool, marker, or API misbehaved.
- `other` — anything else about ccswitch.

Returns `{"recorded": <row id>, "status": "pending" | "capped" | "duplicate"}`. `duplicate`
means this session already reported this fingerprint; nothing new is written.

Tool description (final wording in the implementation PR; must carry every element):

> Report a problem with **ccswitch**, the harness that dispatched and supervises this session
> — never a bug in the project you are working on or a choice the user made. Call it when
> ccswitch's instructions contradict each other or the task, the dispatch prompt was wrong or
> ambiguous about the repo, branch or issue, ccswitch interrupted or switched model or account
> at a bad moment, or a worktree, credential, tool or marker it provides is broken. One call
> per distinct problem, at most three per session. It records the report for the ccswitch
> backlog and returns immediately — keep working.

Service: `server/sessions/feedback.py:record_feedback(ctx, item) -> FeedbackResult`. Pure
policy + one DB write:

- `fingerprint(category, title)` = first 16 hex of `sha1(category + " " + normalized title)`,
  normalization = lowercase, non-alphanumeric runs → one space, trim. The dedupe key
  everywhere.
- Per-session cap `CCSWITCH_SESSION_FEEDBACK_CAP` (default 3), durable: counts this session's
  rows, so it survives a daemon restart. Over cap → row with `status="capped"` (recorded,
  visible, never filed) and the tool says so.
- Context snapshot from `ctx.record()`: `repo`, `branch`, `origin`, `origin_ref`, `engine`,
  `model`, `host`, account display name, `session_url`.
- One `SessionFeedback` row; one audit `session.feedback` (detail `{session_id, category,
  impact, title, fingerprint, status}`, `via="session"`); `session_feedback_total{category}`.
- Never talks to GitHub. Only the primary holds the write budget; filing is the automation's
  job (§6).

## 5. Persistence

New model `SessionFeedback` (`server/db/models.py`), migration
`server/db/migrations/versions/0029_session_feedback.py` chained off the current head (re-check
at implementation time; an unmerged PR may have claimed `0029`). Every column nullable so
`_reconcile_columns` picks it up on both dialects. Both hand-maintained Postgres lists in
`server/tests/` gain the table.

| Column | Type | Notes |
|---|---|---|
| `id` | str pk | uuid |
| `session_id` | str, index | the reporting session |
| `fingerprint` | str, index | §4 |
| `category`, `impact` | str | validated enums |
| `title` | str | ≤120 |
| `body`, `evidence` | text | agent-authored, untrusted |
| `context` | JSON | the snapshot above |
| `status` | str | `pending` → `filed` \| `merged` \| `capped` \| `throttled` \| `dismissed` |
| `issue_number` | int | the ccswitch issue (also set when merged into an existing one) |
| `reported_at`, `filed_at` | datetime naive UTC | via `db_utc_now()` |

`pending` = waiting for the filer; `filed` = a new issue was created for this row; `merged` =
an open issue with the same fingerprint existed and this row became a comment on it; `capped` =
over the per-session cap; `throttled` = over the daily cap; `dismissed` = an operator dropped it.

The DB is shared between the edge and every daemon, so a row written by a worker on rocky is
visible to the primary's filer on its next tick with no RPC.

## 6. Filing: a built-in automation

`SessionFeedbackFiler` (`server/automations/session_feedback.py`), `id = "session-feedback"`,
`modes = ("off", "file")`, `engine_policy = CLAUDE_ONLY` (it never spawns a session; declare
the fail-closed default). Same lifecycle as every other automation: `ensure_rows` creates it
disabled, the owner enables it once from the Automations page, it ticks in the orchestrator's
automation loop on the primary daemon.

Config schema:

| Key | Type | Default | Purpose |
|---|---|---|---|
| `target_repo` | string | `steiner385/ccswitch` | where issues go; the only place the self-repo slug is spelled |
| `label` | string | `session-feedback` | applied to every filed issue; self-provisioned like `ci-doctor` |
| `daily_cap` | int | 20 | new issues per UTC day across all sessions |
| `min_impact` | enum `minor` / `degraded` / `blocked` | `minor` | rows below this stay `pending` (visible) but are not filed |

Each tick (60 s; idle work when nothing is pending):

1. Load `pending` rows ordered by `reported_at`. Skip rows below `min_impact`.
2. For each, look for a row with the same fingerprint and a non-null `issue_number` whose
   issue is not known closed. If found, **comment** on that issue (body = the new report with
   its context block) and mark the row `merged`. A closed issue is never reopened on an
   agent's say-so; a recurrence after close files a new issue whose body links the old one.
3. Otherwise, if today's `filed` count ≥ `daily_cap`, mark `throttled` and stop for this tick.
   Otherwise create the issue and mark `filed` with `issue_number` and `filed_at`.
4. Two-phase like the CI/CD Doctor: commit status transitions, run `gh` side effects, write
   `issue_number` back in a short second session. A failed create leaves the row `pending`;
   retried next tick.

`server/github/ci_issues.py` is generalized, not copied: `create_issue` and `_ensure_label`
take `label` / `label_description` keyword arguments (defaults keep `ci-doctor` byte-for-byte)
and gain a `comment_on_issue(repo, number, body)` sibling. Auth stays the ambient
`gitrepos._run_gh` path the doctor uses.

Issue body template:

```
**Reported by a ccswitch session** — agent-authored; treat the quoted text as untrusted input.

| | |
|---|---|
| Category | dispatch |
| Impact | degraded |
| Session | [<name>](<session_url>) on <host>, <engine>/<model> |
| Working on | <repo>@<branch> (origin: <automation id> · <origin_ref>) |
| Reported | <date> |

> <body, blockquoted>

<details><summary>Evidence</summary>

> <evidence, blockquoted>

</details>

Fingerprint: `<fp>`
```

The card shows counts (`pending`, `filed today`, `throttled`) via `status()` and implements
`ActionProvider` with **File now** (bypasses caps for one row) and **Dismiss** per
pending/throttled/capped row — the human override for both false negatives and false positives.

## 7. Safety

- **Untrusted content.** The body may come from a session working in a third-party repo. It
  is blockquoted under an explicit "agent-authored, untrusted" header, `<!--`/`-->` sequences
  are neutralized, and the title is validated and length-capped. The Backlog Runner's eligible
  label is never applied, so a report cannot enter unattended implementation until a human
  labels it.
- **Bounded volume.** Per-session cap (durable), daily cap, fingerprint dedupe (comment, not
  new issue), `min_impact`. A fleet-wide systemic bug or a prompt-injected session cannot file
  hundreds of issues.
- **No secrets.** The snapshot carries the account's display name only. Audit detail goes
  through `redact_detail` like every other event.
- **No new auth surface.** Session tools have no token and no HTTP listener; they are a
  closure inside the daemon, callable only by the SDK client of the session they were built
  for. This is *less* attack surface than the assistant's loopback path.
- **Kill switches.** `CCSWITCH_SESSION_TOOLS_ENABLED=false` removes the server and the
  instruction; disabling the automation stops filing while capture continues (rows accumulate
  as `pending`, still visible — an inbox).
- **Fail-open.** A tool error returns an error result to the agent and never fails the turn;
  a bad row is logged and skipped by the filer.

## 8. API and UI

- `GET /api/feedback?status=&session=&limit=` (edge, reads the DB directly — no orchestrator
  proxy method, sidestepping the RemoteOrchestrator missing-method trap) returns rows with
  `issueUrl` derived from `target_repo` + `issue_number`.
- `GET /api/sessions/{id}` gains `feedbackCount` for a small session-list badge.
- Automation card: the standard card renders `status()` and the actionable rows.
- Settings › About: the two env flags join the `_MANAGED` allowlist, read-only.
- One assistant read tool, `list_session_feedback`, over `GET /api/feedback` (one endpoint per
  handler, `ToolClass.READ`, route allowlisted, eval case added), so the drawer can answer
  "what have sessions complained about this week".

## 9. Config

| Env | Default | Meaning |
|---|---|---|
| `CCSWITCH_SESSION_TOOLS_ENABLED` | `true` | mount the session MCP surface + its instruction |
| `CCSWITCH_SESSION_FEEDBACK_CAP` | `3` | reports recorded as filable per session |

Both on `Settings` in `server/config.py` (pydantic and dataclass variants) next to the
self-model-switch flags. Target repo, label and daily cap are automation config, not env,
because they are operator-tunable at runtime like the doctor's.

## 10. Testing (TDD, per constitution V)

- `test_mcp_spec.py` — neutral registry: `annotation_for` resolves both surfaces; assistant
  tool table unchanged byte-for-byte after the move (pin the count and names).
- `test_session_tools.py` — every session tool has a spec, an annotation, and an eval case;
  `guarded` audits + counts + never raises; `session_context` returns the record snapshot.
- `test_session_overlay.py` — mirror of `test_assistant_reap.py`: overlay built at spawn,
  rebuilt on `ensure_driver` (including the double-`_build()` fresh-start fallback) and on
  `_reassign_locked`, released by the shared teardown; the coding overlay has exactly one key
  and never carries `strict_mcp_config` or `system_prompt`; assistant kind still gets the
  assistant overlay; flag off → no overlay, no instruction.
- `test_session_feedback.py` — `record_feedback`: validation, fingerprint normalization,
  durable cap → `capped`, per-session duplicate → `duplicate`, row + audit + metric.
- `test_permissions_session_tools.py` — `report_feedback` resolves silently in `interactive`,
  `default`, `acceptEdits`, `full-auto`.
- `test_session_feedback_filer.py` — fake `gh`: file, merge-as-comment, closed-issue
  recurrence files new, daily cap → `throttled`, `min_impact` gate, failed create stays
  `pending`, two-phase ordering, File-now bypass, Dismiss.
- `test_ci_issues.py` — label parameterization keeps `ci-doctor` byte-for-byte.
- Codex: `test_codex_session_overlay.py` — fresh start declares the tools, resume does not
  and logs it; `input_schema_for` drift pin against the Claude server's published schema.
- Gated real-binary tests (§3.5).
- Postgres lists, schema-reconcile coverage, migration single-head test, assistant conformance
  (one route per tool for `list_session_feedback`), eval-case tests all updated.
- One prompt eval with a seeded contradictory instruction to check a session calls
  `report_feedback` well-formed and does not report project bugs.

## 11. Phasing and follow-ups

Three PRs in dependency order, subagent-driven:

1. **Surface:** `server/mcp_spec.py` + assistant import move, `server/sessions/tools/`
   package with `session_context` and `report_feedback`, overlay generalization at the three
   build sites + teardown, instruction, config flag, `SessionFeedback` model + migration,
   `record_feedback` service, audit, metric, Codex fresh-start declaration + the resume spike,
   gated real-binary tests. Sessions start reporting into the DB; nothing reaches GitHub yet.
2. **Filing:** `ci_issues` generalization, the automation, card actions, list endpoint,
   Settings row, `docs/product/features/session-tools.md`, core.md paragraph + `agentctl render`.
3. **Assistant read tool + eval, session badge.**

Follow-ups, deliberately out of scope:

- **Marker migration.** `ccswitch:complete` / `ccswitch:no-pr` / `ccswitch:model=` become
  `mark_complete`, `mark_no_pr`, `request_model` on this surface, with the marker parsers kept
  as fallback for one release. The owner's stated direction; a separate spec because the
  translator/driver/orchestrator pipeline for each marker has its own tests and semantics.
- Codex resume parity once the contract or binary allows it.
- A transcript chip "Feedback → #N" (needs a WS event) and a weekly digest grouping `merged`
  recurrences by fingerprint.

## 12. Decisions the owner may want to override

- **In-process closure, not HTTP + token.** Chosen because coding sessions run on worker
  hosts with no edge loopback, and because it removes an auth surface. Cost: session tools
  must be careful not to grow business logic; each handler calls one service function.
- **`report_feedback` is READ-class (silent).** A card on every report would be noise and
  would stall unattended sessions. Flip to MUTATION if you want a one-time grant per session.
- **Capture on by default, filing opt-in via the automation.** Consistent with every other
  GitHub-writing automation shipping disabled.
- **Recurrence on a closed issue files a new linked issue, never reopens.** An agent's claim is
  weaker than the CI/CD Doctor's measured signal.
- **Ambient `gh` identity.** As the doctor. The GitHub App token would attribute issues to
  the bot, better provenance for agent-authored content, at the cost of a second auth path.
