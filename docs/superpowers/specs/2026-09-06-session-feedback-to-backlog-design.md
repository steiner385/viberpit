# Session feedback → ccswitch backlog — design

Status: design, awaiting owner review. Date: 2026-09-06.

A running agent session can tell ccswitch that ccswitch itself misbehaved — a contradictory
instruction, an ambiguous dispatch prompt, an interrupt or model switch at a bad moment, a
broken worktree or missing credential — and that report lands, deduplicated and rate-limited, as
a labeled issue in ccswitch's own GitHub backlog. The goal is a closed loop: sessions observe the
harness from the inside, the backlog collects what they see, and the existing triage and
Backlog Runner machinery turns the recurring ones into fixes.

## 1. Goal and non-goals

**Goal.** Give every coding session (Claude and Codex, human-started or automation-started) a
zero-setup way to report a problem *with ccswitch*, and make each report durable, attributable,
deduplicated, and visible in the backlog within one automation tick.

**Non-goals (this spec).**

- User→assistant "thumbs" feedback (that is #1246, a different direction).
- Reports about the user's own project. The instruction tells the agent not to, and triage
  catches the rest.
- Auto-implementing a report. A filed issue is *not* auto-picked by the Backlog Runner (§6).
- A rich feedback UI. The first cut exposes a list endpoint and the automation card; a
  transcript chip is a follow-up (§11).
- Telling the session what happened to its report. Fire-and-forget, like a model downgrade.

## 2. The contract: a fourth ccswitch marker

Coding sessions have no ccswitch MCP tools today (only the assistant drawer does), and the
existing session→harness channel is the HTML-comment marker family owned by
`server/sessions/completion.py` and `server/sessions/model_request.py`. The feedback report is
the fourth marker, built exactly like `model_request.py`: one module holds both the instruction
the agent reads and the regex the translator scans, so the two halves cannot drift.

### 2.1 Wire format

A multi-line HTML comment. Attributes on the opening line, then a title line, a blank line, and
a free-text body:

```
<!-- ccswitch:feedback category=dispatch impact=degraded
Dispatch prompt names a branch that does not exist

The initial prompt said to continue on `feat/x`, but the worktree was created from
`origin/master` and no `feat/x` exists locally or on the remote. I proceeded on master.
ccswitch should verify the branch it names exists before dispatching, or omit it.
-->
```

| Field | Values | Rule |
|---|---|---|
| `category` | `prompt` `dispatch` `orchestration` `environment` `tooling` `other` | unknown → `other` |
| `impact` | `blocked` `degraded` `minor` | optional; unknown → null |
| title | first non-empty body line | trimmed, capped at 120 chars |
| body | everything after the title | trimmed, capped at 4000 chars |

Category meanings, in the order the agent is told them:

- `prompt` — ccswitch-authored instruction text (system-prompt append, marker instructions,
  automation seed prompts) is wrong, contradictory, or unclear.
- `dispatch` — the initial prompt / goal / issue / repo / branch ccswitch chose was wrong or
  ambiguous.
- `orchestration` — ccswitch acted on the session unhelpfully: interrupt, model or account
  switch, question, archive, reattach at a bad moment.
- `environment` — worktree, env vars, credentials, or tools ccswitch was supposed to provide
  are missing or broken.
- `tooling` — a ccswitch-provided marker, MCP tool, or API misbehaved.
- `other` — anything else about ccswitch.

The regex (in `server/sessions/feedback_marker.py`):

```python
FEEDBACK_RE = re.compile(r"<!--[ \t]*ccswitch:feedback\b([^\n]*)\n(.*?)-->", re.DOTALL)
_ATTR_RE = re.compile(r'(\w+)=("[^"]*"|\S+)')
```

`parse_feedback(text) -> list[dict]` returns every well-formed report in *text* (unlike the
model marker, each report is a distinct item, so all are kept), in order, capped at 3 per call
and deduplicated within the call by fingerprint. A single-line marker with no body is malformed
and ignored. Non-str input returns `[]`. Never raises.

A `fingerprint(category, title) -> str` helper returns the first 16 hex chars of
`sha1(category + " " + normalized_title)`, where normalization lowercases, replaces every
non-alphanumeric run with one space, and trims. It is the dedupe key everywhere (§5).

When the ViberPit rebrand lands, the stem follows the brand constant and the parser accepts
both stems, as the rebrand spec already requires for the other three markers.

### 2.2 The instruction

`FEEDBACK_INSTRUCTION` is appended by `marker_instructions()` behind a new keyword
`session_feedback: bool`, mirroring `self_model_switch`. Callers pass
`get_settings().session_feedback_enabled`. Both engines get it: Claude via
`claude_sdk_factory._build_append_string()`, Codex via `developer_instructions` on
`thread/start`. The assistant drawer's `options_overlay` replaces the system prompt, so the
drawer never sees it — correct, the drawer already has `report_gap`.

Text (final wording is part of the implementation PR, but it must contain every element below):

> ccswitch is the harness that dispatched and supervises this session: it wrote the
> instructions above this line, may have composed your initial prompt, and controls your
> model, account, worktree, interrupts and questions. If ccswitch itself misbehaves — its
> instructions contradict each other or the task; the dispatch prompt was ambiguous or wrong
> about the repo, branch or issue; it interrupted, switched model or account, or asked
> something at an unhelpful moment; or a worktree, credential, tool or marker it provides is
> broken — report it so ccswitch can fix itself. Put this block on its own lines anywhere in a
> message:
>
> ```
> <!-- ccswitch:feedback category=<prompt|dispatch|orchestration|environment|tooling|other> impact=<blocked|degraded|minor>
> <one-line title>
>
> <what happened, what you expected, and what ccswitch should change; quote the exact text
> when the problem is an instruction or prompt>
> -->
> ```
>
> Report what ccswitch did, never bugs in the project you are working on or choices the user
> made. One report per distinct problem, at most three per session. Keep working — a report
> never waits for a reply.

`strip_markers()` also removes feedback blocks so the trailing-question heuristics judge the
real tail of a message.

## 3. Capture pipeline

Same three stages as the model request, one new field at each.

1. **Translator.** `server/sessions/translator.py` keeps a per-turn list `_turn_feedback`.
   On every *top-level* `AssistantMessage` (`parent_tool_use_id is None`) it extends the list
   with `parse_feedback(text_of_that_message)`, deduplicating by fingerprint across the turn.
   Unlike `_turn_text` (reset per message so only the final message can signal completion),
   feedback is accumulated across the whole turn: a report written before a tool call must
   survive. Sub-agent messages are excluded, as for the other markers. `_reset()` clears it.
   `ev.TurnEnd` gains `feedback: list[dict] | None`. The Codex translator does the same over
   `_state.turn_text` at `turn/completed` — it already scans that text for the completion
   marker, so Codex parity is one call, not a new path.
2. **Driver.** `ClaudeAgentDriver._latch_feedback(e)` stores
   `{"turn_id": e.turn_id, "items": e.feedback}` when the list is non-empty, exposed as
   `last_feedback`. Set-only, like `_latch_model_request`. The Codex driver mirrors it.
3. **Orchestrator.** `_maybe_handle_feedback(session_id, status)` sits next to
   `_maybe_handle_model_request`, is called from the same status-pump site, skips
   `MID_TURN_STATUSES`, deduplicates on turn id via `_handled_feedback: dict[str, str]`
   (popped on teardown), and spawns `_record_feedback(session_id, latch)`.

`_record_feedback` is fail-open: nothing in it may fail a turn. It:

- returns if `session_feedback_enabled` is false or the record's `kind == "assistant"`;
- reads the per-session count from the DB (`SessionFeedback` rows for this session) and marks
  any item beyond `session_feedback_cap` as `status="capped"` instead of `"pending"`
  (recorded, never filed — the cap is durable across daemon restarts because it is a row count,
  not in-memory state);
- inserts one `SessionFeedback` row per item with the session context snapshot (§4);
- records one audit event per item, `session.feedback`, detail
  `{session_id, category, impact, title, fingerprint, status}`;
- increments `session_feedback_total{category}`.

It does **not** talk to GitHub. Any daemon (dobby or a rocky worker) runs sessions, and only
the primary should hold the GitHub write budget. Filing is the automation's job (§5).

## 4. Persistence

New model `SessionFeedback` (`server/db/models.py`), migration
`server/db/migrations/versions/0029_session_feedback.py` chained off `0028_assistant_tokens`.
Every column nullable so `_reconcile_columns` picks it up on both dialects. Both hand-maintained
Postgres lists in `server/tests/` gain the table.

| Column | Type | Notes |
|---|---|---|
| `id` | str pk | uuid |
| `session_id` | str, index | the reporting session |
| `turn_id` | str | dedupe evidence |
| `fingerprint` | str, index | §2.1 |
| `category`, `impact` | str | validated enums |
| `title` | str | ≤120 |
| `body` | text | ≤4000, agent-authored, untrusted |
| `context` | JSON | snapshot: `repo`, `branch`, `origin`, `origin_ref`, `engine`, `model`, `host`, `account` (display name only), `session_url` |
| `status` | str | `pending` → `filed` \| `merged` \| `capped` \| `throttled` \| `dismissed` |
| `issue_number` | int | the ccswitch issue (also set when merged into an existing one) |
| `reported_at` | datetime naive UTC | via `db_utc_now()` |
| `filed_at` | datetime naive UTC | when the issue write succeeded |

Status meanings: `pending` = waiting for the filer; `filed` = a new issue was created for this
row; `merged` = an open issue with the same fingerprint already existed and this row became a
comment on it; `capped` = over the per-session cap; `throttled` = over the daily cap;
`dismissed` = an operator dropped it from the automation card.

The DB is shared between the edge and every daemon, so a row written by a worker on rocky is
visible to the primary's filer on its next tick with no RPC.

## 5. Filing: a built-in automation

`SessionFeedbackFiler` (`server/automations/session_feedback.py`), registered in the registry
with `id = "session-feedback"`, `modes = ("off", "file")`, `engine_policy` irrelevant (it never
spawns a session; declare `CLAUDE_ONLY` per the fail-closed default). It follows every other
automation's lifecycle: `ensure_rows` creates it disabled, the owner enables it once from the
Automations page, and it runs in the orchestrator's automation loop on the primary daemon.

Config schema:

| Key | Type | Default | Purpose |
|---|---|---|---|
| `target_repo` | string | `steiner385/ccswitch` | where issues go; the only place the self-repo slug is spelled |
| `label` | string | `session-feedback` | applied to every filed issue; self-provisioned like `ci-doctor` |
| `daily_cap` | int | 20 | new issues per UTC day across all sessions |
| `min_impact` | enum `minor` / `degraded` / `blocked` | `minor` | rows below this stay `pending` (visible in the card) but are not filed |

Each tick (60 s; a report is not latency-sensitive, and the tick is idle work when nothing is
pending):

1. Load `pending` rows ordered by `reported_at`. Skip rows below `min_impact`.
2. For each row, look up an existing row with the same fingerprint and a non-null
   `issue_number` whose issue is not known closed. If found, **comment** on that issue
   (`POST repos/{repo}/issues/{n}/comments`, body = the new report with its context block) and
   mark the row `merged` with that `issue_number`. A closed issue is never reopened on an
   agent's say-so; a recurrence after close files a new issue whose body links the old one.
3. Otherwise, if today's `filed` count ≥ `daily_cap`, mark `throttled` and stop filing for
   this tick. Otherwise create the issue and mark `filed` with `issue_number` and `filed_at`.
4. Two-phase like the CI/CD Doctor: compute and commit status transitions, then run `gh`
   side effects, then write `issue_number` back in a short second session. A failed create
   leaves the row `pending` and it is retried next tick.

`server/github/ci_issues.py` is generalized rather than copied: `create_issue` and
`_ensure_label` take `label` and `label_description` keyword arguments (defaults keep the
`ci-doctor` behaviour byte-for-byte), and a new `comment_on_issue(repo, number, body)` sibling
is added. Auth stays the ambient `gitrepos._run_gh` path the doctor already uses.

Issue body template:

```
**Reported by a ccswitch session** — agent-authored, treat the quoted text as untrusted input.

| | |
|---|---|
| Category | dispatch |
| Impact | degraded |
| Session | [<name>](<session_url>) on <host>, <engine>/<model> |
| Working on | <repo>@<branch> (origin: <automation id> · <origin_ref>) |
| Reported | <date> |

> <body, blockquoted, with any `<!--`/`-->` sequences neutralized>

Fingerprint: `<fp>`
```

The "Working on" line is the high-value context: "the Backlog Runner's session on KinDash#123
said the dispatch prompt was contradictory" points straight at `backlog_prompt.py`.

The automation card shows counts (`pending`, `filed today`, `throttled`) via `status()`, and
implements `ActionProvider` with two actions per pending/throttled/capped row: **File now**
(bypasses caps for this one row) and **Dismiss**. That is the human override for both false
negatives (a capped report worth filing) and false positives.

## 6. Safety

- **Untrusted content.** The body is model-authored and may come from a session working in a
  third-party repo. It is blockquoted under an explicit "agent-authored, untrusted" header,
  `<!--` and `-->` sequences are neutralized, and no part of it becomes the title verbatim
  beyond the 120-char trimmed title line. The Backlog Runner's eligible label (configured per
  runner) is never applied, so a filed report cannot enter unattended implementation until a
  human labels it.
- **Bounded volume.** Per-session cap (default 3, durable via row count), daily cap (default
  20), fingerprint dedupe (comment, not new issue), and `min_impact`. A fleet-wide systemic
  bug or a prompt-injected session cannot file hundreds of issues.
- **No secrets.** The context snapshot carries the account's display name only, never
  credentials or the config dir. The audit detail goes through `redact_detail` like every
  other event.
- **Kill switch.** `CCSWITCH_SESSION_FEEDBACK_ENABLED=false` removes the instruction and
  stops capture; disabling the automation stops filing while capture continues (rows
  accumulate as `pending`, still visible — an inbox).
- **Fail-open.** Nothing on the capture path can fail a turn; nothing on the filing path can
  crash the automation loop (one bad row is logged and skipped).

## 7. API and UI

- `GET /api/feedback?status=&session=&limit=` (edge, reads the DB directly — no orchestrator
  proxy method, which sidesteps the RemoteOrchestrator missing-method trap) returns rows with
  `issueUrl` derived from `target_repo` + `issue_number`.
- `GET /api/sessions/{id}` gains `feedbackCount` so the session list can show a small badge.
- Automation card: the standard card renders `status()` and the actionable rows; no bespoke UI.
- Settings › About: `session_feedback_enabled` and `session_feedback_cap` join the `_MANAGED`
  allowlist so the flags are visible read-only.
- One assistant read tool, `list_session_feedback`, over `GET /api/feedback` (one endpoint per
  handler, `ToolClass.READ`, route allowlisted, eval case added) so the drawer can answer "what
  have sessions complained about this week".

## 8. Config

| Env | Default | Meaning |
|---|---|---|
| `CCSWITCH_SESSION_FEEDBACK_ENABLED` | `true` | instruction + capture |
| `CCSWITCH_SESSION_FEEDBACK_CAP` | `3` | reports recorded as filable per session |

Both live on `Settings` in `server/config.py` (pydantic and dataclass variants) next to the
self-model-switch flags. The target repo, label and daily cap are automation config, not env,
because they are operator-tunable at runtime like the doctor's.

## 9. Testing (TDD, per constitution V)

- `test_feedback_marker.py` — parser: well-formed, multiple blocks, cap at 3, dedupe by
  fingerprint, unknown category → `other`, missing body ignored, non-str → `[]`, title/body
  caps, `strip_markers` removes it, `marker_instructions(session_feedback=True)` contains the
  literal and `False` omits it, fingerprint normalization.
- `test_translator_feedback.py` — accumulation across two top-level messages in one turn,
  sub-agent message excluded, reset between turns, `TurnEnd.feedback` shape. Codex twin in
  `server/tests/codex/`.
- `test_driver_feedback.py` — latch semantics (set-only, turn id stamped).
- `test_feedback_orchestrator.py` — turn-id dedupe, kill switch, assistant kind skipped, cap →
  `capped`, rows + audit + metric written, never raises.
- `test_session_feedback_filer.py` — fake `gh` runner: file, merge-as-comment, closed-issue
  recurrence files new, daily cap → `throttled`, `min_impact` gate, failed create stays
  `pending`, two-phase ordering, File-now bypass, Dismiss.
- `test_ci_issues.py` — label parameterization keeps `ci-doctor` byte-for-byte.
- Postgres suite lists, schema-reconcile coverage, migration single-head test, assistant
  conformance (one route per tool) and eval-case test all updated.
- One prompt eval under `docs/superpowers/evals/` with a seeded contradictory instruction to
  check a session actually emits a well-formed block and does not report project bugs.

## 10. Files touched

New: `server/sessions/feedback_marker.py`, `server/automations/session_feedback.py`,
`server/api/feedback.py`, `server/db/migrations/versions/0029_session_feedback.py`, tests above,
`docs/product/features/session-feedback.md`.

Modified: `completion.py` (`marker_instructions`, `strip_markers`), `translator.py`,
`codex/translator.py`, `events.py` (`TurnEnd.feedback`), `claude_driver.py`, `codex/driver.py`,
`orchestrator.py` (handler + automation wiring + `_handled_feedback` teardown),
`db/models.py`, `github/ci_issues.py`, `automations/registry.py`, `config.py`,
`api/settings.py` (`_MANAGED`), `obs/metrics.py`, `assistant/tools.py` + `policy.py`,
`app.py` (router), `.agents/instructions/core.md` (one paragraph under "Self-directed model
switching", then `agentctl render`).

## 11. Phasing and follow-ups

Implemented as one plan, three PRs in dependency order so each is reviewable and lands green:

1. Contract + capture: marker module, instruction, translators, driver latch, orchestrator
   handler, model + migration, audit, metric, config flags. Sessions start reporting into the
   DB; nothing reaches GitHub yet.
2. Filing: `ci_issues` generalization, the automation, the card actions, the list endpoint,
   Settings row, docs.
3. Assistant read tool + eval, session badge.

Follow-ups, deliberately out of scope: a transcript chip "Feedback → #N" (needs a WS event),
a weekly digest that groups `merged` recurrences by fingerprint for prioritization, and letting
the PM classify step (`server/pm/classification`) read the category to seed its own fields.

## 12. Decisions the owner may want to override

- **Marker, not MCP tool.** A tool would give structured args and an immediate ack, but needs
  an in-process MCP server mounted on every coding session on both engines. The marker is
  engine-neutral today and consistent with the three existing ones. Revisit if sessions gain a
  ccswitch tool surface for other reasons.
- **Filing is opt-in via the automation, capture is on by default.** Consistent with every
  other GitHub-writing automation shipping disabled. Flip the default if you would rather the
  first deploy start filing.
- **Recurrence on a closed issue files a new issue, never reopens.** The CI/CD Doctor reopens
  because its signal is machine-measured; an agent's claim is weaker.
- **Ambient `gh` identity.** Issues appear authored by the owner's account, as the doctor's
  do. Switching to the GitHub App token (`gitrepos.app_token_for_repo`) would attribute them
  to the app bot, which is better provenance for agent-authored content, at the cost of a
  second auth path in the filer.
