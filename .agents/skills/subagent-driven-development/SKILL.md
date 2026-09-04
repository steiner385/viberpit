---
name: subagent-driven-development
description: Execute an existing implementation plan with fresh Codex subagents when tasks are sufficiently independent for isolated implementation and review. Use for plan execution, not brainstorming or tightly coupled edits.
---

# Subagent-Driven Development

Keep the main thread focused on coordination, requirements, rulings, and final
verification. Use fresh subagents for bounded implementation and review work.

1. Read the plan and any governing spec. Verify the work is on a feature branch or
   isolated worktree based on current `origin/master`.
2. Record progress in `.superpowers/sdd/<plan-name>/progress.md`. Include the plan
   path, task state, commit or diff boundaries, verification, and any ruling made when
   the plan and spec conflict. Resume from this ledger after compaction.
3. Dispatch one `ccswitch-implementer` for each task that needs independent judgment.
   Batch only small, same-shape edits. Never run parallel implementers that can touch
   the same files or interfaces.
4. Give each implementer only its task, relevant prior interfaces, exact constraints,
   and required checks. Tell it where to write any detailed report; keep its response
   concise.
5. After each task, dispatch `ccswitch-reviewer` against the task's complete diff
   boundary. Require both plan/spec compliance and code-quality review. Send concrete
   findings back for correction, then re-review the correction.
6. After all tasks pass scoped review, run one whole-branch review and the repository's
   required verification gates from `CLAUDE.md`.
7. Follow the completion workflow without asking whether to continue. Stop only for
   an irreversible action, a security-sensitive choice, an external side effect that
   needs new authorization, or a plan so incomplete that every path is guesswork.

If subagent tools are unavailable, execute the same task and review gates serially in
the main thread and preserve the ledger.
