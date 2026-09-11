# Autopilot failure-modes audit — 2026-09-11

**Trigger.** The operator reported that autopilot had been "causing a lot of confusion" over the
preceding two weeks: (1) prompting sessions to open a PR when one was inappropriate or already
done, (2) telling sessions their reply was cut off when it was not, (3) a suspicion that it was
injecting prompts into non-ViberPit sessions (plain `claude` CLI), (4) other odd failure modes.

**Method.** Read the whole autopilot surface (`server/automations/autopilot*.py`,
`nudge_composer*`, `pr_intent.py`, `completion_bar.py`, the orchestrator seams), then checked every
claim against the live system rather than the code alone: `automation_event` /
`automation_target_state` / `audit_events` rows for the last 14 days, the daemon journal, the
on-disk Claude transcripts of the affected sessions, and two direct reproductions of helper calls
with the daemon's exact options. Every finding below has a verified root cause; the two design
questions at the end do not, and are left as recommendations.

## Findings (verified)

### F1. "Your reply cut off mid-sentence" — the harness cut the excerpt, not the session

`SessionOrchestrator._autopilot_transcript_tail` shortened each transcript entry with a bare head
slice (`text[:max_chars]`, 400 chars for the decider, 1500 for the composer) and no marker. The
composer then read a reply that stopped at exactly 1500 chars and directed the session to "post
the remainder". Session c07bbc41 (2026-09-10) got that nudge **four times**; on disk every one of
those replies was complete (2036, 2428, 2995, 3137 chars, each ending in a full sentence —
"Nothing further is actionable until you answer."). The session obligingly regenerated the whole
answer each time, which was cut again, which was nudged again.

The head slice also dropped the *ending* of every long reply — exactly where a session states its
hand-off — so the decider judged from the least informative part of the text.

**Fix (this change).** `server/sessions/excerpt.py::elide_middle` keeps head + tail and inserts an
explicit `[… N chars omitted here by the ViberPit harness … not the reply ending …]` marker; the
merged assistant reply is budgeted once, not per delta; the composer budget rose from 1500 to 2500;
both guidance files tell the model what the marker means and forbid the "cut off" conclusion.

### F2. The PR-intent classifier never worked in production — every "no PR needed" claim was rejected

`_autopilot_resolve_pr_intent` called `classify_pr_intent(tail, goal)` with no `_config_dir`. The
classifier then fell back to the daemon's `CLAUDE_CONFIG_DIR`, which the daemon does not export, so
the helper ran with an empty config dir. Reproduced with the daemon's exact options: the CLI answers
`Not logged in · Please run /login` and the SDK raises `Claude Code returned an error result:
success`. Journal: **44 of 44** `pr_intent.classify` calls in 14 days failed this way.

Consequence: the salvage that is supposed to accept a legitimate `mark_no_pr` (or a research-only
session) never ran once. Every self-declared no-PR on a repo-attached goal fell to the low-confidence
"code goal but no PR opened" branch and was nudged with the verbatim template *"open a PR and arm
auto-merge before finishing"* — up to `COMPLETE_NUDGE_CAP` (3) times from the completion path, plus
the autopilot loop's own gate. This is the direct mechanism behind complaint (1). The 2026-09-11
hand-back from session 81ef1d32 reads: *"The instruction to 'open a PR and arm auto-merge' has
repeated four times, but there is nothing to PR."*

**Fix (this change).** The orchestrator passes `_autopilot_helper_config_dir(rec.account)` (the same
engine-safe identity the decider and composer use, #1262). Independently, `decide`, `compose_nudge`
and `classify_pr_intent` now fail fast on an empty config dir (`decider-skipped:no-helper-account`)
instead of spawning a CLI that is certain to say "Not logged in".

### F3. Every autopilot judgement carried the ccswitch repo's CLAUDE.md — for every governed repo

The decider, composer and PR-intent classifier ran with `cwd=os.getcwd()`, i.e. the daemon's
checkout (`/home/tony/GitHub/viberpit-deploy`). `setting_sources=[]` does **not** stop Claude Code
from loading the working directory's `CLAUDE.md`. Reproduced: a helper call with the daemon's exact
options, asked what project instructions it holds, quoted `## Completion workflow` — *"ALWAYS mark
the PR ready for review … ALWAYS flip auto-merge on … Both are required on every completed PR"*.

So the model deciding whether a KinDash, ghostdriver-mcp or cairnea session was finished had this
repo's "always open a PR and arm auto-merge" workflow in its context. A second, unrelated cause of
complaint (1), and one the guidance files could never override.

**Fix (this change).** `helper_call.helper_cwd()` — a dedicated scratch directory
(`~/.cache/ccswitch/helper-cwd`, override `CCSWITCH_HELPER_CWD`) with no CLAUDE.md and no `.git` —
is now the working directory for every stateless one-shot helper (decider, composer, PR intent,
open-question judge, model pick, prompt refiner, backlog grouping). A source-scan test keeps
`os.getcwd()` out of those modules.

### F4. "Injecting prompts into regular claude code sessions" — no injection; helper transcripts in the CLI's namespace

Searched every Claude transcript on disk (`~/.claude/projects`, which every ViberPit account config
dir symlinks to) for the 77 nudge texts autopilot sent in the window. **All 44 direct deliveries
landed in ViberPit worktree sessions** (`…/.ccswitch/worktrees/<repo>-<session>`); none reached a
transcript in a non-ViberPit working directory.

What the operator almost certainly saw instead: the helper calls in F3 each write a transcript
under `~/.claude/projects/<cwd>`. In 14 days that put **4,867** helper transcripts in the deploy
checkout's entry and **7,534** in `/home/tony/GitHub/ccswitch`'s (the daemon's previous cwd). Run
`claude` in either directory and `/resume` lists thousands of "sessions" whose first user message is
autopilot's decision payload. Not injection, but indistinguishable from it in the picker.

**Fix (this change).** F3's `helper_cwd()` moves all of that under one scratch path no one opens
a CLI in. Existing files are not touched (see recommendations).

### F5. Handed-back sessions were nudged onward — and the "Answer needed" state was erased

`hand_back` (tool or marker) derives to `waiting_question` with no structured question card.
`ACTIONABLE_STATUSES` includes `waiting_question`, `_autopilot_get_context` built
`stuck={"kind": "waiting_question"}`, and the decider — with no card to answer — chose `nudge`. The
composer then wrote, for session 8d12c89d (2026-09-11 10:01, one minute after its `hand_back`):
*"You wrote 'Handed back… forks that are yours to call' and went idle — no operator reply is coming
in this async channel, so don't wait on it."* That is false, contradicts the tool's contract, and
flips the session out of the "Answer needed" status the operator was meant to see. Audit join of
`session.hand_back` against autopilot `acted` events shows the loop shape: session 0898bcd5 handed
back at 16:55, 16:56 and 16:56 and was nudged at 16:55, 16:56 and 16:56, then archived at 16:57.

**Fix (this change).** `pre_decide` rule 1b: `waiting_question` with no `question` in `stuck` is
escalated (feed line "needs human — handed back to the user: <reason>") and never reaches the
decider. The reason comes from the live driver's `last_hand_back_reason` and keys the fingerprint,
so a *new* hand-back reason re-surfaces in the feed. Guidance names the rule as well.

### F6. The 2026-09-09 outage: 270 escalation events for one session, nothing in the journal

05:35–10:02 EDT the decider failed on every call for two **Codex-account** sessions (3e3de4db,
f7cbbc63): `decider-failed:Exception` ×74 and `decider-skipped:circuit-open` ×196 for one of them.
Mechanism: `_autopilot_helper_config_dir` falls back to `pick_helper_config_dir()` for a
non-Anthropic account; when that returns `None`, `decide` used `os.environ.get("CLAUDE_CONFIG_DIR",
"")` → `""` → "Not logged in" (F2's failure shape, `type(exc).__name__ == "Exception"`). The daemon
restart at 10:02 coincided with recovery. The daemon journal holds **five** lines for that window
(systemd only) — the process's own logging was not reaching journald, so the per-call warnings the
decider emits were unread and unreadable.

**Fix (this change).** The fail-fast in F2 turns this into one `decider-skipped:no-helper-account`
escalation with no spawn. The de-spam gap (270 feed events despite the `escalated` flag) and the
logging gap are **not** fixed here — see recommendations.

## Recommendations (not in this change)

R1. **Completion-path nudge should go through the composer.** `_auto_archive_completed` sends
`verdict.remedy` verbatim ("open a PR and arm auto-merge before finishing"), bypassing the
composer, its near-duplicate suppression and its prior-nudge memory. With F2 fixed this fires far
less, but when it fires it is still a template that names nothing from the session.

R2. **Output-only dispatches should declare it.** `pm-classify` sessions deliver a JSON array and
have no PR to open, yet `met_bar` treats any repo-attached non-research goal as code work. An
`expects_pr=False` on the dispatching automation (carried on the record, honoured by `met_bar`)
would stop the guard demanding a PR that the dispatch itself said not to open. Related memory:
"pm-classify is output-only".

R3. **Escalation de-spam by reason class and time, not fingerprint alone.** F6 shows the
`escalated` flag did not hold across alternating `decider-failed` / `circuit-open` reasons. A
per-session cap (e.g. one feed event per reason per hour) is a two-line guard in `_escalate`.

R4. **Daemon logging to journald.** The 09-09 window has no Python log lines at all. Confirm the
unit's stdout/stderr → journal path and the logging config on startup; a silent decider outage is
the worst kind.

R5. **Clean the historical helper transcripts.** ~12,400 files under
`~/.claude/projects/-home-tony-GitHub-viberpit-deploy` and `…/-home-tony-GitHub-ccswitch` from
before this change. They are inert but pollute `/resume` in those checkouts; a one-off delete of
files whose first user line is an autopilot payload is safe (nothing reads them). The title and
goal-inference helpers still run in the session's worktree (`cwd=rec.cwd`) and leave transcripts
there too; moving them onto `helper_cwd()` is a follow-up (it also removes the "working directory
does not exist" skip for sessions hosted on another runner).

R6. **Human-owned interactive sessions.** `human_defer_seconds` (120 s) counts only prompts,
answers, permissions and typing — not reading. A `done` reply the operator is still reading gets
nudged two minutes later. Consider deferring longer (or entirely) for sessions with no automation
`origin` while a viewer is attached.

## Evidence index

- Nudge texts: `automation_target_state.state->'recent_nudges'` (77 entries, 2026-09-09..11).
- Event mix: `automation_event` where `automation_id='autopilot'` — 62 nudges, 12 completions,
  356 escalations (217 circuit-open, 77 decider Exception) in 14 days.
- Cut-off claims vs. reality: `~/.claude/projects/-home-tony-GitHub--ccswitch-worktrees-ghostdriver-mcp-c07bbc41/ca99e4ec-….jsonl`
  rows 131/450/457/580 (lengths 3137/2995/2036/2428, each a complete sentence) vs. nudges at rows
  135/454/469/588.
- Classifier failures: `journalctl -u viberpit-daemon.service | grep pr_intent.classify` — 44 failed, 0 succeeded.
- CLAUDE.md leak and "Not logged in": reproduced with `claude_agent_sdk.query` using
  `helper_env(...)`, `allowed_tools=[]`, `setting_sources=[]` and the daemon's cwd / an empty config dir.
- Hand-back overrides: `audit_events.type='session.hand_back'` joined to autopilot `acted` events
  within 60 minutes — 70+ pairs, 8 sessions.
