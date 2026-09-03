# 003 Phase 0 Findings — Projects v2 platform verification

**Date:** 2026-09-03 · **Spike:** `scripts/spike_gh_projects_v2.py` · raw logs at
`.superpowers/sdd/plan/spike-logs/spike-003-{auth,introspect,mutations}.log` (gitignored).

---

## Q1 — Credential story

| Probe | Result |
|---|---|
| Ambient gh user credential reads user Projects v2 | **No** — INSUFFICIENT_SCOPES. Token has `gist, read:org, repo, workflow`; `read:project` is absent. All `projectsV2` fields (id, number, title, closed, updatedAt) returned the same error. (`spike-003-auth.log` lines 8–61) |
| Required scope for ambient credential | `read:project` — not present and not added (no `gh auth refresh` run during spike). |
| GitHub App installation token reads user Projects v2 | **Yes** — `token_for_owner("steiner385")` succeeded; `user(login:).projectsV2 { totalCount nodes }` returned `{"totalCount":0,"nodes":[]}`. No errors. (`spike-003-auth.log` lines 65–74) |

**Decision:** Phase A reads route through the **GitHub App installation token** (minted via
`server/github/app_auth.py:token_for_owner`). The ambient gh credential lacks `read:project` and
cannot be used.

Phase B writes have a **credential precondition** (see Spec impact). The App installation
(`pr-dashy[bot]`) returned FORBIDDEN on `createProjectV2` — it lacks user-Projects write
permission. The ambient credential returned INSUFFICIENT_SCOPES on the same mutation (needs
`project` OAuth scope). Both credentials are unverified for ProjectV2 mutations; they must be
re-probed at Phase B start after provisioning one of: (a) `Projects: read/write` permission added
to the pr-dashy App installation, or (b) a user PAT with `project` scope stored as a secret.

No code change is needed for Phase A: `pm_sync`'s existing gh-CLI seam already calls
`token_for_owner`; Projects v2 reads plug in at that same seam.

---

## Q2 — Mutations

| Mutation | Confirmed name | Works | Notes |
|---|---|---|---|
| Native dep add | `addBlockedBy` | **Yes** | Input: `issueId` (NON_NULL ID) + `blockingIssueId` (NON_NULL ID). `issueId` = the blocked issue; `blockingIssueId` = the blocker. Response: `{clientMutationId: null}` (no value passed). Read-back confirmed: `blockedBy.totalCount:1, nodes:[{number:3}]`. (`spike-003-mutations.log` lines 173–197) |
| Native dep remove | `removeBlockedBy` | **Yes** | Same input shape as `addBlockedBy`. Response: `{clientMutationId: null}`. **No read-back captured after remove** — implementation should verify removal via a follow-up `blockedBy` query. (`spike-003-mutations.log` lines 198–205) |
| Sub-issue add | `addSubIssue` | **Yes** | `issueId` (NON_NULL), `subIssueId` (optional scalar), `subIssueUrl` (optional), `replaceParent` (optional). Two adds succeeded: i1←i2 and i1←i3, both returning `{"issue":{"number":1}}`. Ambient credential. (`spike-003-mutations.log` lines 135–153) |
| Sub-issue reorder | `reprioritizeSubIssue` | **Yes** | **Requires `afterId` or `beforeId`** — passing neither returns VALIDATION error (first run). Input: `issueId` (NON_NULL), `subIssueId` (NON_NULL), `afterId`/`beforeId` (optional but platform-enforced: at least one required). Read-back confirmed order: `[{number:2},{number:3}]`. Ambient credential. (`spike-003-mutations.log` lines 154–172) |
| Aliased batched field updates | `updateProjectV2ItemFieldValue` ×N | **Unverified** | SKIP — `createProjectV2` failed under both credentials, so no project existed. Must re-probe at Phase B start. |
| Field creation via API | `createProjectV2Field` | **Unverified** | SKIP — same reason. Must re-probe at Phase B start. |
| Project creation | `createProjectV2` | **No** (both creds) | App token: FORBIDDEN — "pr-dashy[bot] does not have permission to create projects on ownerId …". Ambient: INSUFFICIENT_SCOPES — needs `project` scope. Phase B precondition. (`spike-003-mutations.log` lines 99–133) |

Mutation names confirmed by schema introspection (`spike-003-introspect.log` lines 1–41): all of
`addBlockedBy`, `removeBlockedBy`, `addSubIssue`, `removeSubIssue`, `reprioritizeSubIssue`,
`addProjectV2ItemById`, `createProjectV2`, `createProjectV2Field`, `updateProjectV2ItemFieldValue`
are present in the live schema.

**Field name correction vs. spec placeholder:** the spec does not name the `blockingIssueId` field
explicitly. The brief used `blockedByIssueId` as a placeholder; the introspected field is
`blockingIssueId` (both `AddBlockedByInput` and `RemoveBlockedByInput`). All implementation code
must use `blockingIssueId`.

---

## Q3 — Delta reads

`ProjectV2.items` args (introspected): `after`, `before`, `first`, `last`, `orderBy`,
`archivedStates`, `query`. (`spike-003-introspect.log` lines 125–176)

Updated-time/since filtering is **not available** → Phase A pages the full item list each cycle.
Acceptable at current scale: largest product has <100 open issues, so a single page (first:100)
covers the full set without multi-page overhead. The `query` arg (String) exists but does not
expose a timestamp predicate; it is text-search only.

---

## Q4 — Limits

| Limit | Documented | Binds at our scale? |
|---|---|---|
| Sub-issues per parent | 100 (GitHub documented limit) | No — largest epic today ≈ 10 sub-issues |
| Blocked-by relations per issue | 50 (GitHub documented limit) | No — no issue approaches this; <100 open issues total across all repos |
| Items per Project | 1,200 (GitHub documented limit for the fields we query) | No — largest product has <100 open issues |

GraphQL node limit: 500,000 nodes per call (introspected from `docs.github.com/en/graphql/overview/resource-limitations`). A single-product sync page (first:100, 5–10 fields each) is far below this. Rate budget: 5,000 points/hour for App installations; pm_sync shares this budget and already tracks it.

---

## Spec impact

None — spec stands as approved.

Two implementation consequences (not contradictions — both are spec-consistent, just more specific):

**(a) Phase A credential wiring:** `pm_sync` extensions that read Projects v2 must use the App
installation token (`token_for_owner(owner_login)`), not the ambient gh credential. The existing
gh-CLI seam in `pm_sync` already calls this path; new query helpers plug in there.

**(b) Phase B credential precondition:** before Phase B is enabled, the user must provision write
access for ProjectV2 mutations — either by granting the pr-dashy App `Projects: read/write`
permission (cleanest; no new secret) or by adding a `project`-scoped PAT as `CCSWITCH_GH_PAT`.
`CCSWITCH_GH_RECONCILE_ENABLED` (default off) remains the kill switch; its enable checklist should
include "re-probe `createProjectV2` to confirm credential works."

---

## Cleanup note

Scratch repo `steiner385/gh-projects-spike-003` is archived (private, no cost) but could not be
deleted during the spike — ambient credential lacked `delete_repo` scope; App token returned 403
"not accessible by integration." The rename fallback was also blocked by GitHub's archived-repo
read-only restriction. **Manual deletion required:**

```bash
gh auth refresh -s delete_repo
gh repo delete steiner385/gh-projects-spike-003 --yes
```
