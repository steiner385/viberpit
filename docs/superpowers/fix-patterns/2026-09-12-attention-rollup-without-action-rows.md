# Attention rollup without corresponding action rows

## Symptom

A parent is labeled Needs you, but its attention counter is zero or the detail says nothing needs the user. The overview shows open work without an explicit next action.

## Anti-pattern

The tree rolls up issue, initiative, and descendant attention, while the action-list selector includes only initiatives and proposals. Reusing raw rollups for issue rows also double-counts primary initiatives and descendants.

## Fix shape

Derive attention groups and their badge count through the same selector. For a capability, subtract immediate child rollups and primary non-program initiatives from each attention tier to find its direct issue count. Include blocked issues because the attention list includes stalls a human can clear. Render issue groups at portfolio/product scope and actual actionable issue rows at the selected capability, with links to descendant capabilities. Keep the full product tree for issue reassignment.

Pair status with a visible review action and guidance based on known issue fields. Preserve the canonical issue data owner by writing through the shared GitHub transport, while keeping edits and disposition inside the application. A link to the external tracker is supplemental, not a substitute for an in-app action. Existing dependency controls remain inline. Hide attention actions on closed issues.

## Where to look

Audited `needsYou.ts`, `NeedsYouList`, portfolio/product/capability details, tree row badges, and the shared `LeafList` used by capability and initiative details. Portfolio and product views inherit the corrected selector. Map badges and summary counts share the actionable selector; issue ordering uses the existing tier ranks to put human input and blockers first. Independently fetched incidents remain explicitly separate from the count. Capability attention now includes its subtree and touching initiatives; the Issues tab remains explicitly direct-only.

Regression protection: selector tests for nested counts, primary versus touching initiatives, and programs; capability and issue-row interaction tests; desktop/phone Playwright coverage of the issue-only attention state.

Tracking: steiner385/viberpit#1608 and #1609.

Browser verification must bind an isolated preview server to the checkout under test. A shared default port with `reuseExistingServer` can silently test another worktree and produce misleading results.

## Verified user correction: navigation is not disposition

The first fix exposed issues and labeled a GitHub link as a next action; the user still could
not decide anything in Roadmap. The source-of-truth boundary governs where state is persisted,
not where the user must interact. A review/disposition request needs the report content and
concrete controls that execute a decision, show errors, and update the visible queue. Browser
regressions should assert mutation requests and resulting state, rather than merely asserting
that an external link exists. The follow-up adds in-app edit, resolved, and not-planned decisions
with journaled upstream writes, stale-data guards, and explicit closure semantics. Tracking: #1625.
