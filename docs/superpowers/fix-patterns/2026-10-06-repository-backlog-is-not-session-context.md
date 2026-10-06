# Repository backlog is not session context

**Symptom:** Unrelated conversations in the same repository show the same next-prompt
chip, including sessions on different hosts.

**Anti-pattern:** Prepending the repository's top open issue to transcript-derived
suggestions makes repository membership stand in for conversational relevance.
A process-local claim map cannot prevent duplicate proposals across fleet daemons.

**Fix shape:** Keep existing-session suggestions scoped to their transcript and own
PR. Remove the repository-wide fallback and its unused cache, ranking, fetching,
and claim bookkeeping. Choosing new backlog work belongs to the explicit backlog
workflow, not the next reply in an existing conversation.

**Where to look:** `SessionOrchestrator.latest_suggestions`, the Codex sibling,
`latest_suggestions_many`, fleet routing, and the composer's first-suggestion ghost
text. The audit found both passive engine paths shared `_dispatch_proposal`;
on-demand generation and live Claude generation already use session context.
The frontend displays the returned first suggestion without ranking it. No other
live consumer of the removed issue picker was found.

Regression coverage exercises both engines, shared and separate orchestrators,
missing transcripts, and preserved session PR follow-ups.
