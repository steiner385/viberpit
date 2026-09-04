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
| Sub-issue reorder | `reprioritizeSubIssue` | **Yes** | **Requires `afterId` or `beforeId`** — passing neither returns VALIDATION error (inferred from first run; only the successful second run is preserved in the saved log). Input: `issueId` (NON_NULL), `subIssueId` (NON_NULL), `afterId`/`beforeId` (optional but platform-enforced: at least one required). Read-back confirmed order: `[{number:2},{number:3}]`. Ambient credential. (`spike-003-mutations.log` lines 154–172) |
| Aliased batched field updates | `updateProjectV2ItemFieldValue` ×N | **Unverified** | SKIP — `createProjectV2` failed under both credentials, so no project existed. Must re-probe at Phase B start. |
| Field creation via API | `createProjectV2Field` | **Unverified** | SKIP — same reason. Must re-probe at Phase B start. |
| Project creation | `createProjectV2` | **No** (both creds) | App token: FORBIDDEN — "pr-dashy[bot] does not have permission to create projects on ownerId …" (`spike-003-mutations.log` lines 99–133). Ambient: INSUFFICIENT_SCOPES — needs `project` scope (separate manual probe, recorded in Task 2 report; consistent with the read-scope failure in `spike-003-auth.log` — not re-run in the committed spike script). Phase B precondition. |

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
| Sub-issues per parent | 100 (per controller ruling; GitHub docs unreachable during spike — verify against docs at Phase B start) | No — largest epic today ≈ 10 sub-issues |
| Blocked-by relations per issue | 50 (per controller ruling; GitHub docs unreachable during spike — verify against docs at Phase B start) | No — no issue approaches this; <100 open issues total across all repos |
| Items per Project | 1,200 (per controller ruling; GitHub docs unreachable during spike — verify against docs at Phase B start) | No — largest product has <100 open issues |

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
"not accessible by integration." The mutations log's final NOTE claimed the repo was renamed to
`DELETE-ME-gh-projects-spike-003`, but a post-spike read-only check (`gh repo view
steiner385/gh-projects-spike-003 --json name,isArchived` → `{"isArchived":true,"name":"gh-projects-spike-003"}`)
confirms the rename PATCH did not apply — the repo retains its original name. **Manual deletion
required using the verified name:**

```bash
gh auth refresh -s delete_repo
gh repo delete steiner385/gh-projects-spike-003 --yes
```

---

## 2026-09-03 addendum: credential re-probe after App grant + PAT provisioning

### (a) Org-level App grant — mutations verified

`organization_projects: write` permission granted to the pr-dashy App installation for
`steiner385`. The full Projects v2 **org-board** mutation set was verified under the App token:
`createProjectV2`, `createProjectV2Field`, `addProjectV2ItemById` (private-repo issues),
aliased `updateProjectV2ItemFieldValue` batch, and `deleteProjectV2` — all returned SUCCESS.
The App token is therefore the right credential for org-owned boards.

### (b) User-owned boards: GitHub platform gap — PAT required

The App installation token **cannot** see user-owned Projects v2 at all. A probe with 24+
boards present (`users/steiner385/projects`) returned `{"totalCount":0,"nodes":[]}` — no
errors, just an empty list. This is confirmed as a GitHub platform limitation: no App
permission exists for user-level Projects (the `organization_projects` permission only covers
org-owned boards). This finding has no workaround within the App token model.

A classic PAT with `project` + `repo` scopes was verified against the full user-owned
mutation set: `createProjectV2`, `createProjectV2Field`, `addProjectV2ItemById` (private-repo
issues), aliased `updateProjectV2ItemFieldValue` batch, and `deleteProjectV2` — all SUCCESS.
The PAT is provisioned as `CCSWITCH_GH_PROJECT_TOKEN` in the daemon env
(`dobby:/etc/ccswitch/daemon.env`).

### (c) CORRECTION to Q1: App token "reads user projectsV2 OK" was a false positive

The Phase 0 table entry "GitHub App installation token reads user Projects v2 — **Yes** —
`totalCount:0, nodes:[]`" was a false positive. `totalCount:0` with no GraphQL errors means
the query executed successfully but returned nothing — the App is blind to user boards, not
forbidden. The probe ran with zero boards present; a subsequent probe with 24+ boards confirmed
the App always returns empty for `user(login:).projectsV2`. **Phase A discovery must use the
PAT, not the App token.** This PR (`fix/003-project-fetch-pat`) wires the PAT preference into
`_pm_sync_fetch_projects`.

### (d) Cross-installation limitation: org-hosted boards cannot resolve user-repo issue nodes

The org installation token (`pr-dashy[bot]`) cannot resolve node IDs for issues in
user-owned repos (`steiner385/cc-account-switcher`, `steiner385/KinDash`). Consequently,
`addProjectV2ItemById` from an org-hosted board targeting user-repo issues fails under the App
token. Org-hosted boards for user repos are not viable as an App-only solution; the PAT (which
has `repo` scope and can see both repos) is required for item-level sync.

### (e) Decision: hybrid credential model

| Operation | Credential |
|---|---|
| Projects v2 reads and writes (all owners) | `CCSWITCH_GH_PROJECT_TOKEN` (classic PAT, `project`+`repo`) |
| Everything else (PRs, issues, webhooks, branch protection, etc.) | GitHub App installation token (`token_for_owner`) |

Boards created: `users/steiner385/projects/26` ("ccswitch") and `#27` ("KinDash"), titles
matched to product slugs. Remaining ops item: delete the archived spike repo
`steiner385/gh-projects-spike-003` (see cleanup note above).

---

## Phase B probe — 2026-09-03

Ran `python scripts/spike_gh_projects_v2.py phaseb` under `CCSWITCH_GH_PROJECT_TOKEN` (PAT).
Raw log: `.superpowers/sdd/plan-phase-b/spike-logs/spike-003-phaseb.log` (gitignored). Scratch
board `spike-003-phaseb (safe to delete)` was created under `steiner385`, populated, and deleted
at the end; a post-run listing of `user(login:"steiner385").projectsV2` confirms no board with
"phaseb" in its title remains.

| Probe | Result | Input shape confirmed |
|---|---|---|
| createProjectV2Field SINGLE_SELECT + options | y | `singleSelectOptions: [{name,color,description}]` — created field "Stage" with 7 options (intake, design, plan, build, deploy, maintain, done), each returned an `id`/`name`. (log § "single-select field with options (Stage)", lines 1–30) |
| updateProjectV2Field replaces built-in Status options | **y** | `fieldId` (the built-in Status field's id, read via `fields(first:20)` on the project node) + `singleSelectOptions: [{name,color,description}]`. The built-in Status field started with GitHub's default `{Todo, In Progress, Done}` and was successfully replaced with the spec vocabulary `{Backlog, Ready, In progress, Blocked, Needs human, Done}` — full option list with fresh ids came back in the response, no errors. (log § "fields incl. built-in Status" / "updateProjectV2Field", lines 31–84) |
| 10-alias add + 10-alias set batches | y | `rateLimit` is a **Query-root field only** — GitHub's schema rejects it inside a mutation operation (`Field 'rateLimit' doesn't exist on type 'Mutation'`, `undefinedField`, confirmed live on the first attempt). Fixed by bracketing each batch with a separate `query { rateLimit { cost remaining resetAt } }` call and diffing `remaining`. Add batch (10× `addProjectV2ItemById`) cost 2 points (remaining 4605→4603); set batch (10× `updateProjectV2ItemFieldValue` with `value: {singleSelectOptionId}}`) cost 2 points (remaining 4602→4600 — the 1-point gap between the add batch's "after" and the set batch's "before" is the intervening `field(name:"Status")` lookup used to resolve the "Ready" option id). No errors in either batch. (log § "20-alias batch", lines 85–87) |
| clearProjectV2ItemFieldValue | y | `projectId, itemId, fieldId` (no `value`) — cleared item a0's Status; response `errors: null`. (log § "clearProjectV2ItemFieldValue", line 89) |

**Implication for Task 9:** edit built-in Status. `updateProjectV2Field` successfully replaces the
built-in Status field's options in place — Task 9's `ensure_fields` should overwrite the default
`{Todo, In Progress, Done}` options with the spec vocabulary rather than creating a parallel
custom field.

**Correction vs. brief:** the brief's probe code appends `rateLimit { cost remaining resetAt }`
directly inside the batch `mutation { ... }` block. That shape is rejected by GitHub's live schema
(`rateLimit` only exists on `Query`). The committed script queries `rateLimit` separately
before/after each batch and reports the `remaining` delta as the batch's cost — this is the shape
Task 6's builders must copy, not the brief's literal mutation-embedded form.

---

## Option-id probe (#987 item 3) — 2026-09-03

`python scripts/spike_gh_projects_v2.py optionids`, PAT again. Scratch board
`spike-003-optionids (safe to delete)` created, probed and deleted in a `finally` block.

The Phase B probe above answered "can `updateProjectV2Field` edit the built-in Status options"
(yes) by replacing a **disjoint** default set. It never answered the question `_ensure_fields`
actually depends on: when the option list is the **union** — every existing name re-sent
unchanged, spec names appended — are the existing options' ids preserved?

| Question | Answer |
|---|---|
| Are the ids of re-sent, unchanged option names preserved? | **No — regenerated.** `Todo f75ad846 → bf7e76f6`, `In Progress 47fc9ee4 → b311984a`, `Done 98236657 → df4deeee`. Every id changed even though the names and their order were re-sent verbatim. |
| Do item values pointing at the old ids survive? | **No — silently blanked.** An item set to `Todo` before the update read back `fieldValueByName(name: "Status") == null` after it. No error, no warning; the value is simply gone. |
| Does the union preserve the human's columns? | Yes — the *columns* all survive by name. Only their contents are lost. |

**Implication:** the union is not "non-destructive", it is "column-preserving". Any Status a human
had already set by hand on a live board is erased by the one-time migration's Status-option update.
This is recoverable but not automatic: the reconciler re-derives and re-pushes Status for every
item **in the mirror**, so managed items refill on the next cycles, and only a value the reconciler
would not have set anyway (a human's `Icebox` column, say) stays blank. Runbook carries the caveat
as a pre-apply decision.

**Second finding — case-colliding near-duplicate columns.** The union dedupes on exact name, and
GitHub's default is `In Progress` while the spec vocabulary is `In progress`. The probe's union
came back with **both**, so a board bootstrapped from GitHub defaults ends up with two
near-identical in-progress columns (plus a dead `Todo`). Not a bug in the union — a consequence of
matching by exact name, and normalising it in code is the wrong lever (`_field_gap` and
`build_batch` both look options up by the exact spec name, so folding case would park every board
whose column is cased differently). Handled as a manual pre-apply step in the runbook: rename the
board's default options to the spec names first, and the union then has nothing to append.

### Follow-up: the ids are regenerated only because the input omitted them (PR #1006)

The probe above sent names only — exactly what `build_update_field_options` did through PR #986.
Re-run with the `optionids` mode extended to try the union **both ways** on two scratch boards:

| union update | preserved option ids | item assignment |
|---|---|---|
| names only | **regenerated** — `Todo` `f75ad846` → `ac49461c` | **lost** (`Status: null`) |
| echoing each existing `id` | **preserved** | **survived** |

`ProjectV2SingleSelectFieldOptionInput.id` is optional-but-accepted: an entry *with* an id is an
update of that option, an entry without one is a new option, name match or not. So the "column-
preserving, not non-destructive" verdict holds only for a names-only input. `build_update_field_options`
now takes `option_ids` and `_ensure_fields` passes the board's name → option-id map straight from
`field_cache`, so the reconciler's own union keeps ids and per-item values. The pre-apply caveat in
the runbook is kept for hand-rolled repairs (spike script, rollback section) that omit the ids.
