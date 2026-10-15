---
slug: session-tools
parent: session-workspaces
title: Session Tool Surface (session_context, report_feedback, mark_complete, mark_no_pr, hand_back, set_hold, propose_decision, share_artifact, request_model, dispatch_session, reply_to_child)
state: delivering
links:
  specs:
    - docs/superpowers/specs/2026-09-06-session-feedback-to-backlog-design.md
    - docs/superpowers/specs/2026-09-07-marker-to-tool-migration-design.md
  code:
    - server/mcp_spec.py
    - server/sessions/tools/registry.py
    - server/sessions/tools/tools.py
    - server/sessions/tools/context.py
    - server/sessions/tools/server.py
    - server/sessions/tools/instruction.py
    - server/sessions/tools/codex.py
    - server/sessions/feedback.py
    - server/sessions/artifacts.py
    - server/sessions/completion.py
    - server/sessions/model_request.py
    - server/sessions/engine.py
    - server/pm/holds.py
---

# Session Tool Surface (session_context, report_feedback, mark_complete, mark_no_pr, hand_back, set_hold, propose_decision, share_artifact, request_model, dispatch_session, reply_to_child)

Give coding agents reliable application context and a supported way to report feedback, coordinate child work, signal progress, and put their deliverables in front of the user.

**Problem** — A coding session run by ViberPit has no way to ask "what am I, and why was I
dispatched" or to tell ViberPit itself (not the repo it's working in) that something about the
harness is broken — a wrong dispatch prompt, a contradictory instruction, a mistimed model switch.
Today that only reaches a human as a stuck session or a bad PR, well after the fact. Separately,
until the 2026-09-07 marker-to-tool migration, a session's completion, no-PR resolution and
mid-session model request were all encoded as HTML-comment markers in its own closing prose (`<!--
viberpit:complete -->` and friends) — prose a regex had to recover exactly, rather than a
declaration the session made on purpose.

**Target users** — every coding session ViberPit runs (Claude and, on fresh threads, Codex), and
the operators who triage `session_feedback` rows into the backlog.

**Desired outcome** — every coding session mounts an in-process `viberpit` MCP server with up to
eleven tools (plus the roadmap tools):

| Tool | Class | Args | What it does |
|---|---|---|---|
| `session_context` | READ | none | id, name, repo, branch, cwd, host, engine, model, account, dispatch origin/ref, effective goal, permission mode, `scratchDir`, `issueOwnerMarker` (an issue/epic body carrying it is skipped by the Backlog Runner, with its sub-issues, while this session is live — `server/pm/session_ownership.py`, #3076) |
| `report_feedback` | READ | category, impact, title, body, evidence? | Reports a problem with ViberPit itself (not the project). One call per distinct problem, at most `session_feedback_cap` per session. |
| `mark_complete` | SIGNAL | none | Declares the session's objective is fully done. |
| `mark_no_pr` | SIGNAL | resolution (`not-needed` \| `cancelled` \| `awaiting-decision`), note? | Declares the work is finished but no PR is appropriate — or, with `awaiting-decision`, that it stopped on a `propose_decision` the person must answer. |
| `hand_back` | SIGNAL | reason | Declares the session needs the user (a decision, review, approval or information) before the objective can be finished; lands at `handed_back` (#2321). |
| `set_hold` | SIGNAL | kind (`evidence`\|`stronger_model`\|`owner_evidence`\|null), reason, ref? | Holds (or releases) the issue this session was dispatched for, on the roadmap ladder (spec 2026-09-18 §8). Limited to the three agent-settable kinds — see below. |
| `propose_decision` | SIGNAL | options (2–6 × {key, label, detail?}), recommended_key, rationale | Parks this session's issue on a PRODUCT DECISION only a person can take: draft the options, name the one you recommend, say why. The person picks one in the Inbox (spec 2026-09-18 §6). |
| `share_artifact` | SIGNAL | kind (`url`\|`path`\|`image`), value, label, note? | Puts a deliverable in front of the user as an always-visible transcript card: an http(s) URL, an absolute path on the session host (copyable chip), or an image on the session host, which the handler ingests into the attachment store and the card renders inline. See below. |
| `request_model` | SIGNAL | model (`haiku`\|`sonnet`\|`opus`\|`fable`), reason | Asks to move to a different model tier. |
| `dispatch_session` | MUTATION | name, prompt, model?, permission_mode? | Starts a linked **child session** (same repo, own worktree, same owner) the user can steer like any session; the parent is notified by a `<child-session-notification>` prompt when the child stops. Claude only; gated by `VIBERPIT_CHILD_SESSIONS_ENABLED`, capped by `VIBERPIT_CHILD_SESSION_CAP` / `_MAX_DEPTH`. Raises a permission card in interactive mode. See [Child sessions](../guides/child-sessions.md). |
| `reply_to_child` | SIGNAL | child_session_id, message | Sends one of this session's children a message as its next prompt — answers a hand-back, resumes a restart-stranded child, stops or re-delegates it (#2911). Mounted with `dispatch_session`; refuses a non-child, an ended/archived child, or a refused delivery as the tool result. See [Child sessions](../guides/child-sessions.md#the-parent-answers-back). |

`session_context` and `report_feedback` are `ToolClass.READ` in `server/mcp_spec.py`'s shared
registry — read-only in effect, since neither changes anything the user can see. `mark_complete`,
`mark_no_pr`, `hand_back`, `set_hold`, `propose_decision`, `share_artifact`, `request_model` and `reply_to_child` are the newer `ToolClass.SIGNAL` (below): also no permission
card, but not annotated read-only, because each one moves something the user does see (a status
badge, a completion-bar verdict, a pending model approval). `PermissionRegistry` resolves both
classes silently in every session mode, never raising a permission card (pinned by
`server/tests/test_permissions_session_tools.py`). Handlers close over a `SessionToolContext` built
in the daemon process that owns the session — no loopback HTTP call, no bearer token — so the
surface works identically on any fleet host, including rocky.

**Success metrics** — a session can answer "how was I dispatched" and file a feedback row without
leaving its own turn; `server/tests/test_session_tools_real_binary.py` (gated on
`VIBERPIT_TEST_CLAUDE_MCP=1`) verifies against the real `claude` binary, per Constitution III, that
a project's own `.mcp.json` servers and `viberpit` coexist and `report_feedback` raises no prompt.

**Non-goals (Phase 1)** — filing to GitHub (Phase 2's automation, shipped since — see below), an
API/UI/assistant view of `session_feedback`, and `session_url` in the context snapshot (needs the
edge's public URL, which nothing on the server derives today; the filer names the session and host
instead of linking to it).

## Problem-solving follow-through

Claude and Codex coding sessions receive one shared task policy from
`server/sessions/tools/instruction.py`, including when session tools are disabled.
An ordinary malfunction report or troubleshooting question defaults to resolving the
problem through a verified fix and the repository's PR workflow. Explicit
investigation-only, explanation, review, planning and read-only scopes remain binding,
as do permission boundaries and task-specific automation handoffs. A real blocker
requires a concrete next step and the appropriate hand-back or decision signal.

This is prompting guidance, not enforced proof of completion. New sessions receive
it after deployment; existing Codex threads retain their original instructions on
resume. Behavioral cases are in `docs/superpowers/evals/session-tools/cases.jsonl`.

## Filing: the `session-feedback` automation (Phase 2, #1434)

`SessionFeedbackFiler` (`server/automations/session_feedback.py`, id `session-feedback`, modes
`off`/`file`) ticks on the primary daemon every 60 s and turns `pending` rows into labeled issues.
It ships **disabled**, like every other GitHub-writing automation; until an operator enables it the
rows simply accumulate as a visible inbox.

It does not decide for itself whether a fingerprint has already been filed — that rule lives once, in
`server/automations/issue_filing.py` (`decide` / `apply`), shared with Error Triage and the CI/CD
Doctor. What the filer owns is the row transition each decision implies:

| Decision | Row becomes | Why |
|---|---|---|
| no linked issue | `filed` + `issue_number` | first report of this fingerprint |
| linked issue open | `merged` | the report becomes a comment on it |
| linked issue closed (completed) | `filed` on a NEW `[regression]` issue | never reopened — the new issue links the old |
| linked issue closed `not_planned` | `dismissed` | a human's decision about the fingerprint, not one report |
| over `daily_cap` | `throttled` | retried tomorrow; **File now** overrides |
| below `min_impact` | stays `pending` | visible, deliberately unfiled |

Config: `target_repo`, `label` (`session-feedback`, self-provisioned), `daily_cap` (20 new issues
per UTC day — comments do not count), `min_impact`. The card lists the pending/throttled/capped
inbox with **File now** (bypasses both caps for one row) and **Dismiss**.

### The clustered inbox (#2936)

The Needs you tab shows the inbox as **clusters** — one row per problem, not per report.
`server/automations/feedback_clusters.py` (pure) groups reports: same fingerprint always together,
then average-linkage agglomeration on title tokens (overlap coefficient ≥ 0.4 — single linkage
chained unrelated reports through words like "backlog runner"). Each tick — also while the
automation is off — the loop asks Sonnet once to merge pre-clusters describing the same problem and
name the theme, but only when the pre-cluster partition changed (`llm_clustering`, default on); a
failed or unparseable pass keeps the deterministic clusters. On the real 50-report fixture: 26
pre-clusters, 18 after the recorded model reply (five themes holding 37 reports, 13 one-offs).

Per cluster, all through `SessionOrchestrator.automation_inbox` → `GET|POST
/api/automations/{id}/clusters[/draft|/file|/dismiss|/attach]`: **File as one issue** (a generated,
editable title/body with every report's session, working-on, quoted body and evidence, fingerprints
footer; the oldest report becomes `filed`, the rest `merged`), **Dismiss** (optionally "fixed by
#PR"), and **Attach** to an existing issue (one comment) or a live session (sent as a prompt through
the same path as `POST /api/sessions/{id}/prompt`). Every op works while the automation is off and
records how the row was settled in `session_feedback.resolution`.

The ops take arbitrary report ids, so the tab also has **bulk select**: checkboxes on clusters and on
member rows, and a bar ("N selected — File as one issue · Dismiss") that settles the whole selection
with ONE confirmation and ONE request. A cluster can carry **possibly fixed by #N**: up to two merged
PRs from the mirrored `WorkItem` rows (`kind=pr`, `state=merged`, the target repo, merged at or after
the cluster's first report) whose title (or title + body head) shares ≥ 2 words and scores ≥ 0.6 on
`feedback_clusters.similarity` against the cluster's pooled titles — one bounded query per view, and
"Dismiss as fixed by #N" records the PR. **Age-out** (`age_out_days`, default 14, 0 = off) runs each
tick before the mode check: an inbox report older than that whose fingerprint was not reported again
(in any status) and whose cluster has no report newer than the cutoff becomes `archived` (resolution
`aged_out`) — never badged, listed in a collapsed "Aged out (N)" digest, and **Restore**
(`POST …/clusters/restore`) puts it back for good (resolution `restored`, never aged out again).
`session_link_base` (default empty) is this install's https address: when set, every filed issue,
comment and session prompt links each report's session as `<base>/sessions/<id>`.

Two safety properties are worth knowing when reading the code, because the two halves are
deliberately *not* symmetric. A **create** claims its row before calling `gh`, so a failed create
leaves it claimed-but-unlinked and is retried next tick outside the cap — a duplicate issue in a
public backlog is permanent noise. A **comment** posts first and only then marks the row `merged`,
because the opposite order would mark a row handled with nothing posted and lose the report
outright. Report bodies are agent-authored and treated as untrusted: blockquoted under an explicit
header, run through the shared credential redactor, and `<!--`/`-->` neutralized so a body cannot
smuggle a marker into the issue. The Backlog Runner's eligible label is never applied, so a report
cannot enter unattended implementation until a human labels it.

## The `SIGNAL` tool class, and why it renders no card

`ToolClass.SIGNAL` (`server/mcp_spec.py`) exists for a tool that reports the session's OWN state
back to the harness — completion, a no-PR resolution, a hand-back, a model request — as opposed to `READ`
(changes nothing) or `MUTATION`/`DESTRUCTIVE` (changes something in the project or account). Two
things distinguish it from `READ`:

- **No permission card**, same as `READ` (`server/sessions/permissions.py:159` short-circuits both
  classes together). This is the *conservative* choice, not the permissive one: these four tools
  replace HTML-comment markers, which had no gate whatsoever, and a card at the end of every
  session would block an unattended run's completion behind a prompt nobody is there to answer.
- **Not annotated read-only.** `annotations_for` sets `readOnlyHint=False` for `SIGNAL` (only
  `READ` gets `True`) — a signal moves a status badge or a completion-bar verdict, and claiming
  that is read-only would be false, even though it gets the same no-card treatment as `READ` for
  the unattended-completion reason above.

### `set_hold`: the kinds an agent may set itself

`set_hold` is SIGNAL for the same reason as `request_model`: it moves a roadmap chip (a hold badge
on the work item, spec 2026-09-18 §3/§8), never something in the project, and an unattended runner
session must be able to say so without a permission card nobody is there to answer. Its handler is a
thin wrapper over the ONE in-process path, `server/pm/holds.py:apply_session_hold` — resolve
`owner/repo#N` (default the session's own `originRef`) to the mirrored issue, then `set_hold` /
`clear_hold`.

A session may only set (or clear) the two kinds `server/pm/holds.py:AGENT_SETTABLE` allows —
`evidence` (someone must reproduce or correlate before anyone builds) and `stronger_model` (this
session is under-powered for the issue). The hold is no longer only a record: series item 4 turned
both kinds into dispatch behaviour — 4a spawns the next attempt one model tier up (the ladder), 4b
spawns an investigate-instead-of-implement run — so setting one is a request the Backlog Runner
acts on. Both remain the auto-exiting kinds: nothing about them requires a human to release the
hold — a later run (agent or system) clears it once the evidence lands or a stronger model picks the
item up.

The third agent-settable kind, `owner_evidence` (#3113), is the one owner-facing park a session
writes itself: the work is done as far as any agent can take it and only the owner can supply what
is left — verify on a real device, record a legal date, confirm a physical fact. The reason must say
exactly what evidence is needed and that it goes in a comment on the issue. Unlike the two above it
parks the card and reads as needs-you; the owner digest lists it in its own group, and the reply
closes it through the hygiene loop's recommend-only `evidence_posted` rule. It is an ASK for a fact,
never a hand-off: a session that is out of moves still calls `hand_back`.

The two kinds that DO need a person to decide are not this tool's, and they are not one job either.
`owner_action` is `hand_back`'s: the session is out of moves and the item belongs to its owner.
`decision` is `propose_decision`'s (below): the session knows exactly what it would do, it just may
not choose between two defensible answers — so it drafts them and parks the item until somebody
picks, which is a different thing from giving up. `set_hold` refuses `decision` for EVERY actor
class ahead of the class gate (only `decisions.propose` may write one, because the hold is the
shadow of a pending proposal), and refuses `owner_action` to an agent one.
`holds.py:may_set`/`HoldPermissionError` enforce the kind restriction, not the tool schema or
the registry spec, so the rule holds for every caller (session, overlay route, or otherwise), not
just this one.

### `propose_decision`: the question an agent may ask without a human turn

Mounted beside `set_hold`, on purpose: the two are the pair a session chooses between when it
cannot proceed — the evidence is missing (`set_hold` kind `evidence`) versus the question is a
product choice (`propose_decision`) — and mounting one without the other leaves only `hand_back`
for the half that is missing. It takes two to six options (`key`, `label`, optional `detail`), the
`recommended_key` among them, and a `rationale`. It parks the issue THIS session was dispatched
for — there is no `ref` argument, because letting a model name a third-party issue would park work
nobody asked it about — and with no such issue (a session the user started themselves, who is right
there to answer) the call is refused and `hand_back` is the way to ask.

It is the one SIGNAL tool whose handler ACTS, like `set_hold`: it calls
`server/pm/decisions.py:propose`, the same function `POST /api/pm/work-items/{id}/decisions` calls,
writing the proposal row and the `decision` hold in one transaction. It is also NOT a turn signal
(`TURN_SIGNAL_NAMES`): the turn ends afterwards with `mark_no_pr("awaiting-decision")`, and the
proposal itself deserves the ordinary ToolCall card as the transcript's only record of it. Its
`ToolSpec` is the first with `idempotent=False` — one pending proposal per item is a partial unique
index, so a second call is a hard conflict rather than a no-op, and the class default would tell
the model exactly the wrong thing about retrying.

`awaiting-decision` is the third `mark_no_pr` resolution beside `not-needed` and `cancelled`
(`server/sessions/completion.py:NO_PR_RESOLUTIONS`), and the parser's error message names when each
applies. The Backlog Runner reads it as neither a bounce nor a hand-off — see
`docs/product/features/autopilot-backlog-runner.md` § The awaiting-decision ending.

### `share_artifact`: the deliverable the user must actually see

The problem it exists for (2026-09-27): a session says "I've put the screenshot at
`/tmp/settings.png` and opened the PR" and the user sees neither. The path was in a Bash tool's
output, the PR URL in a `gh` result, and the transcript collapses every tool row the moment the
next tool follows it (`ToolCallCard` line mode); prose links do render, but a bare path in prose
is plain text and an image on the session host is a URL the browser cannot resolve at all.

`share_artifact(kind, value, label, note?)` makes the deliverable a first-class card
(`web/src/features/sessions/transcript/ArtifactCard.tsx`) that `groupBlocks` lifts OUT of the
tool run — it is never a "tool details" toggle. Three kinds:

| kind | value | The card shows |
|---|---|---|
| `url` | an absolute http(s) URL — nothing else, so a `javascript:` value can never become an anchor; the card re-checks the scheme before the server verdict lands | the label, an anchor opening in a new tab |
| `path` | an absolute path on the session host | the label, a copyable chip (the browser cannot open a host path; the card says where it is) |
| `image` | an absolute path to a png/jpeg/gif/webp on the session host | the image inline, "Open full size", and the source path |

`image` is the one kind whose handler ACTS, like `set_hold`: the daemon that owns the session has
the file and the browser does not, so `server/sessions/artifacts.py:read_image` reads it (regular
file, under the attachment store's `MAX_BYTES`, sniffed by content — a text file named `.png` is
refused), `attachments.write_content` stores it content-addressed under `attachments_dir()`, and an
`Attachment` row bound to the session is created so the edge serves it at `/api/attachments/{id}`
and `delete_for_session` reclaims it with the session's other attachments. On a worker host the
attachments dir is the sshfs-shared account root (`docs/runbooks/rocky-session-host.md`), which is
why an ingest on rocky is servable from dobby with no RPC. The served URL rides the handler's
result envelope; the card reads it from the `ToolResult` (`web/src/session/artifacts.ts`) and
shows "Uploading image…" until it lands, or the handler's refusal if it never does — a broken
share is visible, not silent. Every call is audited as `session.artifact` with its fields.

It is SIGNAL (no permission card: an unattended session must be able to hand over its PR link)
and deliberately NOT in `TURN_SIGNAL_NAMES`: the card is the whole feature, so both transcript
builders render the call, live and on replay, and nothing new is persisted for the transcript —
the CLI's own JSONL carries the call and its result, which is what makes the card survive a reload
for free. On Codex the card is the `dynamicToolCall` item, rendered by its translator.

## Where the signal is read — the Translator, not the handler

A tool **handler** for `mark_complete` / `mark_no_pr` / `hand_back` / `request_model` only validates its
arguments (via the shared parsers in `server/sessions/completion.py` and
`server/sessions/model_request.py`) and returns `{"acknowledged": true, ...}`. It does **not** act
on the signal. The authoritative read is the Claude **Translator** (`server/sessions/translator.py`)
observing the `ToolUseBlock` directly, the same place it already scans turn text for markers.

This split exists for the sub-agent guard: the Translator only honors a signal when
`parent_tool_use_id is None`, because a `Task` sub-agent calling `mark_complete` means its own
sub-objective finished, not the session's. Session tools are mounted for sub-agents too, and an SDK
tool handler only ever sees its own arguments — it cannot tell who called it. A sub-agent's
signalling call still renders as an ordinary tool card and has no effect on the session's status.

Tool-call state is turn-scoped, not message-scoped, and precedence is tool-over-marker: if a turn
carries both a signalling tool call and a stray marker in its prose, the tool call wins.

**`hand_back` also tells the session what the user sees.** Thinking is never rendered as more
than a collapsed "Thinking" row, and a session cannot tell: on 2026-10-02 one wrote five options
inside its thinking, called `hand_back` with a one-sentence reason, and closed with "the five
options above" (session 6ab5c602). So the acknowledgement carries a `userSees` block — the
character count of the prose in the message that made the call (`Translator.message_prose`,
read through `ClaudeAgentDriver.signal_message_prose`, which waits for the Translator to have
recorded the call before answering) and a note: with prose, that `reason` and the prose are
what the user sees; without, that the user sees ONLY `reason` and the session should write its
question and options out now. No block at all when the prose is unknowable (no driver probe,
the call never reached the Translator): a guessed "nothing was shown" would make every hand-back
repeat itself. The description says the same up front, so the question goes in prose before the
call rather than in thinking.

The persisted ask survives a daemon restart: `_record_from_row` maps `hand_back_reason` and
`suspected_complete_reason` back onto the rebuilt record (dropped until 2026-10-02 — a deploy
turned every handed-back card and banner on the host into "the session stopped and is waiting on
you" while the row still held the sentence).

`set_hold` (and `share_artifact`'s image ingest) do NOT follow this split: the handler acts
immediately (calls `apply_session_hold` / stores the image) rather than deferring to the
Translator, because a hold or a shared deliverable is not a claim about the session's own
top-level completion status — the sub-agent guard above exists to stop a `Task` sub-agent's own
sub-objective from being mistaken for the session's, which does not apply to "this issue needs
evidence" or "here is the screenshot" (a sub-agent's shared artifact is just as worth seeing).
They get the SIGNAL class for the same no-permission-card treatment as the turn signals, not for
the Translator-reads-it behavior.

That is why the two transcript builders (`server/sessions/translator.py` and the JSONL
`server/sessions/replay.py`) key on `registry.TURN_SIGNAL_NAMES` — the four names the Translator
reads as a turn outcome — and **not** on the wider `SIGNAL_TOOL_NAMES` permission class. A
`mark_complete` card would be noise on top of the status change it causes; a `set_hold` card is the
only record in the transcript that the session parked or released a roadmap item, so it renders
like any other tool call.

## Three ways a turn ends

| The session… | Signal (Claude) | Marker (Codex / fallback) | Wire status | Board |
|---|---|---|---|---|
| declared the objective met | `mark_complete` / `mark_no_pr` | `<!-- viberpit:complete -->` / `<!-- viberpit:no-pr -->` | `complete` | "Complete"; auto-archive once the completion bar is met |
| needs the user | `hand_back(reason)` | `<!-- viberpit:hand-back reason="…" -->` | `handed_back` | "Handed back" plus the one-sentence reason, amber, push alert |
| just stopped | nothing | nothing | `done` | "Your turn", calm |

Precedence within one turn: a failed turn > `hand_back` > completion > plain success; tool
beats marker. A turn carrying both a hand-back and a completion signal reads as a hand-back and
logs `sessions.hand_back_over_completion` — keeping a session on the board is the recoverable
error. `hand_back` has its own `handed_back` status since #2326: it used to borrow
`waiting_question`, which promised a card to tap that was never rendered. The two cardless
prose paths (the parked-`done` overlay and the completion open-question hold) land there too.

**A hand-back lands when the turn ends — but the ask shows the moment it is made (#2647).**
Prod 2026-10-02 (session bfc8ccbf): the session called `hand_back("Approve a lock-gated
dequeue…")`, wrote "While waiting for your approval, I'm starting the gate fix", and kept
issuing tool calls in the same turn for 13+ minutes. `handed_back` is derived from the turn's
`ResultMessage`, so the card read "Working…" and nothing — no banner, no push, no audit row —
reached the owner. Two halves fixed it. The Translator now also emits an internal
`HandBackRequested` when it reads a top-level `hand_back` tool-use block; the driver parks the
reason as `pending_hand_back_reason` and re-derives status, and `_persist_status` writes it to
`sessions.hand_back_reason` beside the busy status (`MID_TURN_STATUSES` only), audits it, and
fires the needs-you push with the ask in the body. The card shows "asked you — …" over the
Working state, the in-session panel pins the ask above the composer, and the status frame
carries `handBackReason` so both update without the poll. The audit and push fire once per
ask (when the reason changes), so the turn-end `handed_back` that follows does not repeat
them; the next turn's first write clears the ask. The other half is the contract: every
`hand_back` acknowledgement carries `note` = `HAND_BACK_TURN_NOTE` ("finish this message
after the call and start no further work in this turn"), and the description says the same —
`request_model` next to it says "keep working", so the contrast is explicit. Eval case
`hand-back-then-stop`.

**Autopilot never answers a hand-back (#2639).** `autopilot_policy.pre_decide` rule 1b
escalates every `handed_back` session before the decider runs: a hand-back asks for what only
the owner can give (a decision, an override of the dispatch's own constraints, approval), so no
nudge or auto-answer is ever sent. The boundary is deliberate and visible — the card goes amber
with the reason and a push alert fires — which is also why a hand-back that sits unanswered is
waiting on the owner, not on a stalled autopilot. Autopilot does answer structured
`question`/`permission`/`plan` cards. The hand-back in #2639 was provoked by a misfired completion
nudge: `SessionRecord` carried no `initial_prompt`, so every live `met_bar` call received
`instructions=None` and the investigate-only dispatch check (#2480) never ran (fixed in #2653).

**The suspected-complete overlay.** When a turn ends in `hand_back`, the orchestrator judges
the hand-back off the pump (`_judge_suspected_complete`): if the reason asks for nothing
(`completion.hand_back_reason_asks` is the precision guard, `background_tasks.text_awaits_user`
the recall floor, both consulted) AND a merged or armed PR meets the same `met_bar` the
completion path runs, the bar's reason is written to `sessions.suspected_complete_reason`
beside the hand-back reason, served as `suspectedCompleteReason`, and audited as
`session.suspected_complete`. It has the hand-back's lifetime: every status write clears it.
The card renders a "Likely complete" pill with the bar reason on hover and two one-tap
answers — **Mark complete** archives through the ordinary archive path, **Not done** clears
the hint (`POST /api/sessions/{id}/suspected-complete`, audited as
`session.suspected_complete.resolve`). Autopilot names the verdict in its escalation and
never archives on it; the confirm/reject ratio in the audit trail is the evidence any
future auto-archive on suspicion would have to cite.

**What "finished" means.** The completion bar (`completion_bar.met_bar`) reads a PR that is
open, ready, auto-merge armed and without a failing required check as the goal met, so the
`mark_complete` description and the completion marker instruction say so in the same words
(`completion.FINISHED_STATE_CLAUSE`): a session must not wait for CI or the merge, and must
not hand back a status report about it. Its twin, `completion.STATUS_ONLY_CLAUSE`, rides the
`hand_back` description and marker instruction: a hand-back must ask for something. Both
came from prod 2026-09-25, when a session with one PR merged and the next armed under a
still-running CI ended with `hand_back("Status only, no action needed: …")` — the tool
contract had offered it no finishing signal it was allowed to use. The reason rides `turnEnd` as
`handBackReason` and is audited as `session.hand_back`; nothing in the web reads it yet.
Spec: `docs/superpowers/specs/2026-09-08-hand-back-signal-tool-design.md`.

## The engine split: which sessions get tools, which keep markers

Which contract a session gets is decided by `AgentCapabilities.durable_tool_surface`
(`server/sessions/engine.py`):

| | Claude | Codex |
|---|---|---|
| `durable_tool_surface` | `True` | `True` |
| Signalling tools (`mark_complete`, `mark_no_pr`, `hand_back`, `set_hold`, `propose_decision`, `share_artifact`, `request_model`) declared | yes | yes (as `dynamicTools` on `thread/start`) |
| Instruction taught | tool descriptions | tool descriptions |
| `session_context` / `report_feedback` | yes | yes |

Both engines' declared surfaces survive a resume. Claude's system-prompt append rides every
resume. Codex's `thread/resume` has no field to re-declare `dynamicTools`, and needs none:
codex-cli 0.153.2 saves the tools a thread declared on `thread/start` with its session metadata and
restores them on resume (`docs/superpowers/spikes/2026-09-06-codex-resume-dynamic-tools.md`,
2026-10-05 addendum). Codex was on the marker contract until 2026-10-05 because the original spike
could only check the schema, not a resumed thread, and inferred the tools were dropped.

A Codex thread keeps the set it **started** with, so a thread started before 2026-10-05 resumes
with only `session_context`/`report_feedback` and its marker instructions — the permanent marker
fallback below is what still lets it finish. The Codex driver reads the instruction flags off the
tools it actually declares (`CodexAgentDriver._fresh_thread_params`), so no session is ever told
about a surface it doesn't have.

## The permanent marker fallback

The marker parsers (`server/sessions/completion.py`, `server/sessions/model_request.py`) are not
scaffolding for this migration — they stay wired permanently, for three reasons:

1. **Codex threads started before 2026-10-05 emit markers** — they keep the instructions and tool
   set they started with (the engine split above).
2. **Stored transcripts are re-parsed on replay.** Every session that ran before this migration
   wrote markers, and replay must still recover them.
3. **A Claude session resumed across a deploy** carries the system prompt it started with for its
   remaining turns — if that was the marker-instruction prompt, the harness must still be able to
   read a marker from it.

`strip_markers()` also stays: prose heuristics elsewhere in the orchestrator still need to see past
a marker even on a Claude session that mostly signals via tools now. Which contract a session was
*taught* changed with this migration; the harness's ability to *read* either one did not.

**Notes** —
- **Kill switch** — `VIBERPIT_SESSION_TOOLS_ENABLED` (default `true`); off drops the overlay at the
  next session build (no in-flight session is torn down) and, with it, the four signalling tools —
  at which point every session falls back to marker instructions.
  There is no separate kill switch for the signalling tools alone (spec §10): this migration is
  reversible today only by disabling the whole surface.
- **Feedback row lifecycle (Phase 1)** — `record_feedback` (`server/sessions/feedback.py`) dedupes
  exact repeats by `(session_id, fingerprint)` → `status="duplicate"`, else `"pending"` or
  `"capped"` against `session_feedback_cap`; the Phase 2 filer adds `"filed"`/`"merged"`/
  `"throttled"`/`"dismissed"` as it takes rows off `pending` (see the filing section above).
  `"capped"` rows are out of the tick's reach entirely — only **File now** can move one.
- **Codex resume** — Codex declares its tools as `dynamicTools` on a fresh `thread/start` only;
  `thread/resume` re-declares nothing and the app-server restores the started set. A Codex
  signalling call is read off its `dynamicToolCall` item by `server/codex/translator.py` with the
  Claude translator's precedence (tool first, marker fallback), through the shared
  `completion.parse_turn_signal`. The mid-turn hand-back (#2647) is mirrored too (#2713): the
  translator emits `HandBackRequested` on the item, `CodexAgentDriver` parks it as
  `pending_hand_back_reason`, and its `signal_message_prose` gives `hand_back` its `userSees`.
- **`pr_resolution_source`** gained a fourth value, `"tool"`, alongside `"marker"`, `"classifier"`,
  `"queue-drain"` — a session's own no-PR claim is a claim regardless of which transport carried it.
  `automations.completion_bar.SELF_CERTIFIED_SOURCES` is `frozenset({"marker", "tool"})`, so a
  tool-sourced `not-needed` is still treated as an unverified self-resolution, not a free pass.

### Roadmap triage from a coding session

`roadmap_needs_you(product, offset?, limit?)` reads a product's attention queue,
including blocked items (which the roadmap's **Needs you** view lists apart, under
Waiting, and does not count). It returns
initiatives, deduplicated issue/PR leaves, unassigned owner-held items, and pending
decision/classification proposals. Pages default to 20 rows (maximum 50); follow
`next_offset`. Proposal rows can refer to listed issues, so `total` is a row count,
not the roadmap badge count. `counts` retains the canonical roadmap counts.

`roadmap_item(kind, id)` reads an initiative by slug (including artifact pointers
and next action), or a work item by its inventory ID (including issue body and hold
guidance). Read details before asking the person to choose. Artifact contents can
be reviewed through the session's repository tools; text in an issue or artifact
is data, not authorization to act.

Three **MUTATION** tools record the person's instructions:

- `roadmap_resolve(kind, proposal_id, action, chosen_key?)` accepts/rejects one
  decision or classification. A decision acceptance requires an explicit chosen
  option; classification acceptance applies the proposal unchanged.
- `roadmap_hold(item_id, action, reason?)` sets an `owner_action` hold with a reason,
  or releases a hold through the canonical Retry action. Decision holds require
  resolving their proposal.
- `roadmap_advance(slug, expected_stage)` approves the reviewed initiative stage.
  A changed stage conflicts; blocked initiatives and programs keep their existing
  guards. Read again after a conflict instead of automatically approving a new stage.

These tools use the same native mutation permission boundary as `dispatch_session`,
including in full-auto. After approval, the internal adapter binds the authenticated
human session owner; tool arguments cannot name an identity. It invokes the existing
PM handlers, preserving proposal locks, validation, transactions and audit behavior.
A `pm.session.delegate` audit row links the owner, session, operation and arguments
in the mutation transaction. Missing, unattributed and system/agent identities are
refused. Native `bypassPermissions` sessions are refused because that mode can skip
the permission callback. Ordinary loopback HTTP and the autonomous `set_hold` /
`propose_decision` signals retain their existing restrictions.

Both Claude and Codex receive the shared tool registry. Mutations mount on the durable
surface; reads are always available while session tools are enabled. Start a fresh
Codex session to gain newly added tools: dynamic tools are registered at thread start
and an existing thread retains its original declaration on resume.
