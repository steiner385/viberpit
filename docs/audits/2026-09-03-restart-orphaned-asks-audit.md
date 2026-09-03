# Restart-orphaned interactive asks audit — 2026-09-03

Captured while fixing "a session's open question/permission/plan approval is unanswerable
after a daemon restart" (dead read-only cards, or affordances that silently vanish).

## Symptom

After a restart, a session that was mid-`AskUserQuestion`, mid-permission-prompt, or
mid-plan-approval comes back in an unknown state: the question renders but nothing lets
the user answer it; a permission prompt or plan-approval card is simply gone with no
explanation.

**Prod evidence** — session `b37aba5d-6d3d-4501-a74d-e3561234f618`, transcript
`~/.claude/projects/-home-tony-GitHub--ccswitch-worktrees-KinDash-b37aba5d/fa1cd767-c8a6-43ed-a6d7-6b02ec7e3c67.jsonl`:

- line 127, `2026-09-03T07:03:41Z` — assistant emits `AskUserQuestion`
  `toolu_01BRXfxzxajebTr8a3tXQpxx`, header "Disposition".
- ~3.5h gap; four `ccswitch-daemon.service` lifecycle events in the window. The question
  sat on screen the whole time, unanswerable.
- line 128, `2026-09-03T10:29:37.850Z` — `tool_result` `is_error=True`: *"The user
  doesn't want to proceed with this tool use. The tool use was rejected... STOP what you
  are doing and wait for the user to tell you how to proceed."* Then line 129,
  `[Request interrupted by user for tool use]`.
- `systemctl show ccswitch-daemon.service -p ActiveEnterTimestamp` →
  `2026-09-03 06:29:39 EDT` = `10:29:39Z`, **two seconds after** that synthesized
  rejection.

The user's actual decision was never delivered. Instead the assistant was told the user
**rejected** the tool use — a false statement about the user's intent, and one that is
indistinguishable in the transcript from a genuine dismissal. That is the "unknown state":
the CLI closes out a dangling `tool_use` on resume by synthesizing a rejection, and
nothing downstream can tell the difference between "the user said no" and "the daemon
died mid-ask."

## Anti-pattern

A human-facing request modelled **only** as a live in-process `asyncio.Future`, with the
UI's answering affordance gated directly on that future's existence. A restart destroys
both the future and the SDK subprocess blocked in `can_use_tool` awaiting it, so the
request becomes unresolvable **by construction** — it can never be resolved again, only
re-asked, with the user's choice delivered as a new prompt.

**The three instances**, all normalizing to `idle` at rehydrate and all excluded from
#938's turn-resume path (`server/sessions/types.py` — "awaiting-human ... must NOT be
nudged"):

| Pre-restart status | Transcript-replayable | Recoverability |
|---|---|---|
| `waiting_question` | yes — real `AskUserQuestion` `tool_use` | re-offerable as an answerable card |
| `waiting_input` (permission) | no — `can_use_tool` leaves no transcript record | not re-offerable; can only say so |
| `ready_review` (plan) | yes — but only as a raw `ExitPlanMode` tool card | not re-offerable as a choice; can only say so |

## Fix shape

- **One detector, dependency-free.** `orphaned_ask_kind(interrupted_by_restart,
  interrupted_from_status) -> "question" | "permission" | "plan" | None` in the new
  `server/sessions/orphaned_asks.py` is the single definition of "this ask died with the
  daemon." No new DB columns: `SessionRecord.interrupted_by_restart` and
  `interrupted_from_status` already carry the signal (set by `rehydrate()`, cleared
  together by `_persist_status` on any non-`idle` status), so the condition self-clears
  the moment the session does anything again.
- **Never resurrect a dead future.** An orphaned question is answered by sending a
  **new prompt** (`orphaned_answer_prompt` → `send_prompt`), never by resolving a Future
  that no longer exists. The live (non-orphaned) answer path is unchanged; orphan
  handling is strictly a fallback after it declines.
- **Explicitly disown the CLI's synthesized rejection.** Because the assistant reads
  "the user doesn't want to proceed... STOP" immediately before the delivered answer,
  `orphaned_answer_prompt` states in-band that the restart — not the user — closed out
  the tool use, and that the answer that follows is genuine. Without this the assistant
  stops instead of acting on it (this is why the prod transcript shows the same "STOP"
  language and then nothing).
- **Re-offer what's replayable; say so honestly about what isn't.** A question is a real
  `tool_use`, so `replay.py`'s `last_question_events` reconstructs it (reusing the
  existing `AskUserQuestion` parser — no second parser) and `ws.py` re-emits it with
  `orphaned=True` after the live pending-question re-emit; `QuestionCard.tsx` renders an
  explanatory banner and keeps the card answerable. A permission or plan approval has no
  such replayable shape, so `InterruptedAskNotice.tsx` states plainly that the prompt was
  lost, nothing was approved, and offers "Ask again" (re-asking, not re-answering).
- **NOT fixed here — autopilot still cannot unstick an orphaned ask.** An earlier draft
  of this note claimed autopilot's stuck-session handler previously called
  `answer_question` and got `False` for orphaned questions, and that the fallback made
  it succeed. Re-reading the code, that is wrong on both halves. The handler's answer
  branch (`server/sessions/orchestrator.py`, `_autopilot_apply`) is gated on
  `stuck.get("question_id") is not None`, and `stuck` is assembled from
  `self._registry.pending(...)` / `self.pending_questions(...)` — the LIVE registries,
  which are empty by construction for an orphaned session (that is what "orphaned"
  means). So the branch never fires for one, and never did: no `answer_question` call
  was being rejected, because none was being made. Autopilot's route to an orphaned ask
  would be a separate change (build `stuck` from `orphaned_interaction` + the transcript
  re-offer); it is deliberately out of scope here, where the answer path is driven by a
  human at the card.

## Sweep result for other future-backed registries

Checked both future-backed registries in `server/sessions/` for the same shape:

| Registry | State | Verdict |
|---|---|---|
| `QuestionRegistry._pending` (`server/sessions/questions.py`) | `{future, session_id, identity, turn_id, header, question, options, multi_select}` per question | **Covered** — this is the `waiting_question` instance above; `orphaned_ask_kind` + the transcript re-offer path handle it. |
| `PermissionRegistry._pending` (`server/sessions/permissions.py`) | `{future, identity, session_id, tool, input, turn_id}` per permission request | **Covered** — this is the `waiting_input` instance; `InterruptedAskNotice` surfaces it (no replayable transcript form, so it is a "lost, ask again" notice rather than a re-offered card). |
| `PermissionRegistry._grants` (`server/sessions/permissions.py`) | `(session_id, fingerprint) -> expires_at`, TTL cache for "Always allow" decisions | **Not covered, and does not need to be.** This is a pure performance cache, not a pending request — nothing awaits it, and losing it on restart only means the next matching tool call re-prompts for approval instead of auto-approving. Re-prompting is the safe default (it never silently grants something it shouldn't), and the grant simply gets rebuilt the next time the user says "always allow." No future, no waiter, no unresolvable state — this is the harmless case the anti-pattern above does not describe. |

`ready_review` (plan approval) is not a registry at all — it is a session status with no
backing `_pending` map, replayed only as a raw `ExitPlanMode` tool card — so it was folded
into the same fix (`InterruptedAskNotice`) rather than needing a separate sweep entry.

## Reference

- Plan: `docs/superpowers/plans/2026-09-03-restart-orphaned-interactive-requests.md`
- New module: `server/sessions/orphaned_asks.py` (detector + prompt text), consumed by
  `server/sessions/orchestrator.py` (`orphaned_interaction`, `_deliver_orphaned_answer`)
  and `server/bridge/ws.py` (`_orphaned_question_frames`).
- Frontend: `web/src/session/reducer.ts` (`orphaned` on the `question` block),
  `web/src/components/session/QuestionCard.tsx` (orphaned-question banner),
  `web/src/components/session/InterruptedAskNotice.tsx` (permission/plan lost-prompt
  notice), `web/src/components/session/SessionBody.tsx` (renders it above the composer).
- Prior art on the same boundary: `docs/audits/2026-09-02-restart-stranded-state-audit.md`
  — same root shape, process-local state (there: an in-memory dict and a queue row
  deleted too early; here: an `asyncio.Future`) lost across a restart that runs on every
  deploy.
