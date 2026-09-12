# ViberPit hierarchy 5S completion audit

This pass completes the remaining hierarchy cleanup after [PR #1598](https://github.com/steiner385/viberpit/pull/1598).
The canonical model remains [Product → Capability → Initiative → Issue](../product/README.md),
with optional programs grouping initiatives. Delivery acceptance remains separate
from taxonomy and backlog classification.

## Retire the temporary capability

Rebranding is a finite initiative, rather than a standing product capability.
The existing [rebrand initiative #1365](https://github.com/steiner385/viberpit/issues/1365)
and all 15 work-item references were moved to `operator-experience` through the
supported initiative and work-item overlay APIs before removing the indexed
capability document. The affected work items are #1365, #1368–#1378, #1591,
#1592, and #1594. Their existing scores and triage states were preserved during
this migration.

The overlay API also updates `classified_at` for a capability-only edit. Because
the reconciler uses that timestamp in board ownership decisions, preservation of
historical board eligibility was not established by the score/triage comparison.
No pre-migration board snapshot exists. Retained `parked` labels also occur on
current Ready records, so they are not sufficient evidence to overwrite the
canonical board status. Explicit operational holds remain applicable; no bulk
status restoration or dependency changes were made from those labels.

The [migration guide](../product/guides/viberpit-rebrand.md) retains the complete
original body and evidence links. The existing brand-ratchet allowance follows
the moved file; no allowance or ceiling was added. The product map contains
34 capabilities under six roots, with no parser or index-planner warnings.

A read-only reference audit after migration found zero work-item references,
initiative-capability links, capability links, or pending proposals targeting
the retiring capability. One already accepted proposal remains as historical
evidence. The normal index planner retains the old feature row as orphaned;
there is no destructive database deletion or rewriting of historical decisions.

## Close already repaired link reports

The seven reports below describe links to the former product slug. The current
versioned documents use `project: viberpit`, and each exact source/target edge
was present in the live `feature_links` table on September 12, 2026. Both ends
resolve to active capabilities in the ViberPit product. These reports can close
as resolved without scheduling seven additional repairs.

| Issue | Source capability | Resolved target | Link kind |
|---|---|---|---|
| [#1531](https://github.com/steiner385/viberpit/issues/1531) | credential-store-encryption | account-reauthentication | integrates_with |
| [#1532](https://github.com/steiner385/viberpit/issues/1532) | credential-store-encryption | config-dir-isolation | powered_by |
| [#1533](https://github.com/steiner385/viberpit/issues/1533) | config-dir-isolation | session-engine-terminal | integrates_with |
| [#1534](https://github.com/steiner385/viberpit/issues/1534) | config-dir-isolation | multi-account-management | powered_by |
| [#1535](https://github.com/steiner385/viberpit/issues/1535) | command-line-interface | multi-account-management | integrates_with |
| [#1536](https://github.com/steiner385/viberpit/issues/1536) | command-line-interface | credential-store-encryption | powered_by |
| [#1537](https://github.com/steiner385/viberpit/issues/1537) | command-line-interface | config-dir-isolation | powered_by |

The live roadmap health readout at 2:39 p.m. EDT on September 12 reported no
unparented capabilities, unlinked initiatives, stale proposals, or stale inbox
entries. This is a point-in-time observation of that readout, not a claim that
all engineering or operational work is finished.

## Prioritize and sustain

The [backlog priority audit](2026-09-12-viberpit-backlog-priorities.md) records the
remaining items, the existing RICE-lite convention, per-item rationale, and
explicit holds. Scores estimate priority; they do not waive dependencies,
owner decisions, design gates, or maintenance windows. The seven repaired
reports above are excluded from repair dispatch.

The live API verified 61 score updates: 23 classified records, 37 explicit
decision/evidence holds, and one scores-only update for the already in-progress
#1521. Its current triage value was retained to avoid overriding the human board
decision; a subsequent GitHub read still showed In progress. No held record
appeared in the Ready queue snapshot. The explicit maintenance hold on #1372
also remained Needs human on GitHub.

After the existing reconciler ran, all 23 classified records appeared in the
live Ready queue with the reviewed scores, and none of the 37 held records
appeared there. This verifies propagation beyond the overlay write response;
it does not waive the runner's subsequent live eligibility checks.

Immediately after scoring, onboarding still reported eight unclassified records:
the seven reports awaiting PR closure and #1521's deliberately retained triage
value. There were zero pending proposals. The onboarding state was not advanced
to `live` by pretending these counters were zero. New work and independent
in-flight PRs can change these point-in-time counts after this audit.

The [assistant acceptance audit](2026-09-12-assistant-acceptance.md) records fresh
verification and precise remaining exit criteria. In particular, a merged
implementation or closed GitHub tracker is not evidence of deployment acceptance.
Observation periods remain open until the required time and measurements exist.

The previously missing #1056 live smoke passed against the existing edge, daemon,
and database using only newly created disposable sessions. A real read tool
produced a fresh successful audit event; an archive permission request was denied,
the target remained idle, and both test sessions were then archived through the
normal API. Independent review supported exactly one `build` → `deploy` advance,
which was applied and verified. A separate deployed browser check rendered the
assistant model picker, starter prompts, successful read tool, and answer; its
temporary assistant and browser tab were also cleaned up. These observations do
not establish external CLI access, every tool/browser interaction, or long-term
metrics.

The planning consolidation epic [#997](https://github.com/steiner385/viberpit/issues/997)
remains open for its actual unfinished children: [#988](https://github.com/steiner385/viberpit/issues/988)
requires board reconciliation acceptance, and [#996](https://github.com/steiner385/viberpit/issues/996)
requires operational cleanup verification. Neither is closed merely to make
the hierarchy appear complete.

The documentation child [#995](https://github.com/steiner385/viberpit/issues/995)
is completed by consolidating Phase A/B findings, ownership/data flow, and the
nine steady-state hygiene outcomes into the existing [spec 003 architecture](../../specs/003-gh-projects-integration/spec.md).
Its prerequisite #985 is closed by merged PR #1015; the capability document
already marks the earlier Phase 4 model superseded. The consolidation removes
the stale deleted dependency-resolver reference and explains the actual
fail-closed behavior for unknown cross-repository blockers. It adds no parallel
runtime implementation or architecture document. Independent review confirmed
the issue's acceptance criteria; this PR can close #995.

A read-only check narrowed #996: the live database already contains
`ix_gh_write_journal_mutation_id` on `gh_write_journal(mutation_id)`, so no index
DDL is needed. The archived [Phase 0 scratch repository](https://github.com/steiner385/gh-projects-spike-003)
still exists. Its irreversible deletion remains an operational action, not a
documentation or hierarchy mutation.

## Verification

The unchanged application code passed the complete local Python gate: Ruff,
Mypy, 681 core tests, and 9,158 server tests (33 skipped). The full server run
used parallel file distribution and a clean test environment. Existing focused
product-map and brand checks passed (60 tests), as did 137 assistant conformance,
grader, permission, and external-token tests. The brand-ratchet ceilings remain
unchanged and local documentation links resolve. Independent task and branch
reviews found no remaining qualifying findings after two stale backlog premises
were moved to verification holds.

The architecture consolidation also passed 92 existing focused tests for the
journal, mutations, reconciler, queue, shared configuration, and API behavior.

Use the canonical product maintenance rules for subsequent changes: preserve
stable identities, verify references before retirement, distinguish parentage
from dependency order, and attach acceptance evidence before advancing a stage.
This cleanup does not create a recurring automation or manually dispatch backlog work.
