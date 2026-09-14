# Retained ownership needs independent observation

## Symptom
Completed automation work keeps blocking later work after its PR disappears from the open population, or after its launch response failed to update the display state.

## Anti-pattern
Run completion reconciliation only while the original work remains eligible for new dispatch; use a UI/retry-state session ID as the authority; validate stopped execution without atomically releasing the standalone claim. A related exclusion gap resolves only PR nodes while ignoring verified linked issues owned by another workflow.

## Fix shape
Observe durable operations independently of open-population, mode and schedule gates. Recover session identity from the operation's reserved/bound association and validate current generation proof under the ownership transaction. Release standalone work with its matching state update; retain runner-handoff ownership until its monitor completes. Ordinary stopped-work observation preserves failure budgets; actual recovery clearing preserves lifetime history. Never replay mutations while observing. Resolve legacy PR and consolidation claims with the same complete linked-issue identity set used for new work.

## Where to look
PR sweep, consolidation launch tail, recovery clearing, target claim retries and legacy reconciliation. Runner stopped-work and terminal-closeout paths use operation proofs and have separate coverage. Manual map update recovery already observes its retained operation on subsequent requests independently of its cached behind flag; it does not promise background observation.

## Observer versus acknowledgment race
A fast actor can finish before its successful launch/update callback returns. If the observer releases ownership first, callback-only attempt accounting loses history or throws a stale-token error after success. Persist the intended accounting result before the effect; both callbacks and observers settle it exactly once from durable acceptance/completion evidence under the ownership transaction. Release only after settlement. A late callback recognizes the retained operation's settled acknowledgment and cannot overwrite a later attempt. This also recovers a callback lost to restart. Mechanical response handling must recognize observed completion that raced its HTTP response, rather than regressing completed/released work to uncertain. New attempt transfers remove old acknowledgment and mechanical-completion evidence.

Settlement precedes transfer as well as release. A manual successor and a runner terminal-closeout can otherwise bypass observer accounting while the old callback is delayed. Commit predecessor accounting before evaluating a new policy snapshot; stale automatic decisions must refresh using the newly recorded budget. A reserved UUID can recover a lost bind only when a matching persisted session, actor/origin provenance, and current stopped-generation proof agree. Missing or mismatched evidence retains ownership and cannot transfer an unaccounted accepted attempt.

When another workflow transfers or releases a PR operation, settle AND detach its associated PR state in the same transaction. Leaving a transferred/released token in the PR row permanently poisons future claims even though target locks were released correctly. Lock the PR row, verify it still references this predecessor, preserve its accounted counters, and remove that token/session association. A newer row's token is not yours to clear.
