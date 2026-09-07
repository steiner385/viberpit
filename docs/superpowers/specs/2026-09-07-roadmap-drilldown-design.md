# Roadmap drill-down: Products, Capabilities, Initiatives, Triage — design

**Date:** 2026-09-07
**Status:** approved design, pre-implementation
**Owns:** #993 (003 Phase D portfolio UI). Coordinates with #1414 (page/token adoption).
**Supersedes where they conflict:** spec 003 §7c (portfolio, initiative, queue views);
PM-layer design §8 (Products area: inbox, product board, roadmap); UI target-state
§4.3 and page-migration PR 9 insofar as they treat Products and Initiatives as two
separate Standard pages.

## 1. Problem

The Products, Initiatives and Triage pages exist but do not add up to a roadmap. An
audit on 2026-09-07 (appendix A) found:

- Three generations of design coexist with two incompatible hierarchies. The PM-layer
  spec says `Product → Feature → Initiative → Issues`; spec 003 says `Initiative =
  epic issue` and never mentions Feature. The payoff surfaces of the first (product
  board, priority roadmap) were declared superseded by the second, which never
  specified replacements. Phase D was never planned.
- The links that would join the pages exist in the database and stop at the API.
  `Initiative.project_id` is never serialized; there is no features endpoint; the
  `feature_links`, `initiative_features` and `initiative_repos` tables have no readers
  or writers.
- Hierarchy depth is one level, owned by GitHub (epic → sub-issue). No capability
  grouping, no initiative-to-capability relation in practice.
- Vocabulary drifts six ways (Products/project, capability/feature, initiative/epic,
  stage ids vs labels, five status vocabularies, five meanings of "triage": the PM
  proposal inbox, the session attention tier, the control-band incident queue that
  landed with #804, feedback triage, and the nav label).
- Every page dead-ends: the onboarding wizard asks the user to "advance" with no
  control, counts are not links, sync is silent, there is no detail view, no edit, no
  delete, no search, no filter, no sort.
- The assistant's tool surface can read initiative detail, product detail, product
  dependencies and the priority queue. The pages cannot.

The operator's stated intent is a top-down view of the product roadmap: how
capabilities relate, group and depend on each other; drill down and trace back up;
add detail at any level; see status.

## 2. Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | Spine is **Product → Capability → Initiative → Epic → Issue/PR**. | Capabilities are the durable map of what a product is; initiatives are efforts that change them. Matches the operator's mental model and the PM-layer data. |
| D2 | Capabilities form a **nested tree** of arbitrary depth via one `parent` link. | The operator wants groups; two fixed levels cannot express a sub-capability. |
| D3 | **Markdown stays truth** for capabilities. UI edits write to a branch and open a PR; the tree shows them as proposed until merge. | Keeps one source of truth and the existing review gate; the AI feature-map proposals already work this way. |
| D4 | **One open "map changes" PR per product**, appended until merged. | Edits feel immediate; one review per sitting; AI map-diff proposals join the same PR. |
| D5 | Roadmap is **priority-ordered only**. No dates, no milestones. | Matches how agents pull work; no new time columns. Every prior spec rejected timeline views. |
| D6 | Products becomes a **master/detail section on the Sessions chassis**: list column in `app.list`, detail in `app.main`, selection is the URL. | Reuses the generic list, row, filter, ordering and keyboard code that already exists; one paradigm across the app. |
| D7 | The list column has three scopes: **Map · Initiatives · Queue**. Initiatives also appear as leaves in Map under their primary capability; Issues appear as lazy-loaded leaves. | Map is the complete picture; Initiatives is the flat, stage-grouped view of the same rows; Queue is what agents do next. |
| D8 | **Editing is inline** at every level. | Adding detail at a level is the point; a separate form page breaks the drill-down. |
| D9 | The tree shows **one attention tier per row**, rolled up from children, computed like the sessions tier. | Five raw status vocabularies are the single largest source of confusion. |
| D10 | Triage keeps its nav entry and keystrokes, moves onto the same chassis, gains a capability chooser and undo. | It is a work mode, not a page; the list/detail split fixes its unlabelled editor. |
| D11 | User-facing vocabulary is fixed: **Product, Capability, Initiative, Epic, Issue**. Code and API identifiers keep `project`/`feature`. | Renaming tables and routes is a cascade across sync, reconcile and assistant tools; labels come from one module so copy cannot drift. |
| D12 | Control-band **incidents** (#804) are not "triage" in the UI. They surface in the Needs you tabs of the portfolio and of the product that owns the repo, and on the initiative that adopts one. The Triage nav entry is the PM proposal inbox only. | The two queues share a word and nothing else; the incident code already says "incident" everywhere. |

## 3. Use cases

Grouped by job. "New" marks model or API work.

**Orient**
1. See the portfolio: every product, one-line description, health rollup.
2. See one product's capability tree with lifecycle and doc link.
3. See what is in flight for a product and which capabilities each initiative touches. *New.*
4. See the ranked upcoming work with score inputs, per product and across products.

**Navigate**
5. Drill: product → capability → initiative → epic → issues → PRs and sessions.
6. Trace up from any issue, PR, session or initiative to its capability and product.
7. Follow dependencies at each level: product edges, capability links, issue blocked-by.
8. Group capabilities into sub-trees. *New.*

**Add detail**
9. Register and edit a product: repos, description, product-level dependencies.
10. Create or edit a capability, its parent, summary and links. *New (PR-backed).*
11. Create an initiative from a capability; set primary and touched capabilities. *New.*
12. Decompose an initiative plan into an epic with sub-issues (spec 003 Phase C, out of scope here; the UI leaves room for it).
13. Add or remove a dependency at any level.
14. Reprioritize by editing score inputs.

**Curate and gate**
15. Triage: accept, correct the capability, adjust scores, or reject the classifier's proposal.
16. Advance SDLC gates with the pending decision visible and a way back one stage.
17. Work the "needs you" pile: PRs awaiting merge, rejected proposals, no-fit clusters, blocked items.

**Monitor**
18. Rollup status at every level derived from below.
19. Drift and staleness: stale capability docs, prose-only deps, items off the board, no-fit clusters.
20. Agent activity: what agents will do next and what they are doing now, per initiative.

Out of scope: time dimension (21), manual rank override, multi-user roles, non-GitHub
projects, a drag-drop board (GitHub's is deep-linked).

## 4. Information architecture

**Nav.** Two entries replace three. `products` (label Products, icon Package,
`hasList: true`) is the roadmap home. `triage` (label Triage, icon Inbox,
`hasList: true`) keeps its entry and gains a `badge` with the pending count.
`initiatives` is removed; `/initiatives` redirects to `/products?scope=initiatives`,
`/initiatives/:slug` stays as the initiative detail route.

**Routes.** Every level is deep-linkable. Paths are built in one module modelled on
`session/sessionNavState.ts`, derived from `NAV_BY_ID`, never literals.

| Path | Detail pane shows |
|------|-------------------|
| `/products` | Portfolio: Needs you · Ranked queue · All initiatives |
| `/products/:slug` | Product: Overview · In flight · Needs you · Ranked · Dependencies |
| `/products/:slug/capabilities/:capSlug` | Capability: Overview · Sub-capabilities · In flight · Issues · Needs you |
| `/initiatives/:slug` | Initiative: Track · Epic · Sessions · Capabilities |
| `/triage` (+ `?scope=product:x` or `capability:y`) | Inbox list + proposal detail |

Query state: `scope` (list scope), `tab` (detail tab), list filters. Tabs are URL
state per the page-migration contract.

**Breadcrumb.** A new `ui/Breadcrumb` primitive renders the graph path for the open
node: `Products › ccswitch › Sessions › Dictation › Server-side STT › Whisper on the
edge › #1204`. An initiative reached from a global list shows its product and primary
capability, so trace-up works regardless of how the user arrived. Issues and PRs are
leaves; they link out to GitHub and appear on the initiative and capability panes.

**Help text.** Each nav entry's `help` says what the page is for. Keystrokes move to
the shortcut registry and the Help sheet's keys table.

## 5. Shell generalization

Today `hasList` is a boolean and `ContextList` always renders the sessions column;
`useOpenItem` only recognizes `/sessions/:id` and archived. Products (and later
Automations) need:

1. `NavDef.list?: () => JSX.Element` — the list column component for the section.
   `ContextList` renders `def.list` when present. Sessions moves onto this field.
2. `NavDef.openItem?: (params, pathname) => OpenItem | null` — how the section
   decides an item is open, so `REGION_VISIBILITY['app.list']` hands the phone band
   to the detail. `OpenItem.kind` gains `'product' | 'capability' | 'initiative' |
   'proposal'`.
3. A workspace hook per list section beside `useSessionsWorkspace`, supplying list
   props; `app.dock` stays gated on `inSessions`.
4. `ui/Breadcrumb`.
5. A `features/products/vocab.ts` exporting every user-facing label (`PRODUCT`,
   `CAPABILITY`, stage labels, tier labels). The Advance button, track nodes, chips
   and empty states all read from it.

These are the first phase and are independent of any data change.

## 6. The list column

Composed from existing generic parts: `ScopeBody`, `ScopeSegment`,
`SessionListControls` (generic over group/sort options), `useItemOrdering`,
`useListNavigation`, `GroupedList`, `CardFrame`. New: `ProductRow`, `CapabilityRow`,
`InitiativeRow`, `IssueRow` as `CardFrame` content with tree indent, expand toggle
and hover affordances; a `roadmapTier.ts` shaped like `session/statusTier.ts`.

**Scopes.**
- **Map**: products as group headers; under each, the capability tree; initiatives
  (◆) as rows under their primary capability and as a link chip under any other
  capability they touch; epics and issues as lazy leaves under initiatives and
  capabilities. Two pseudo-nodes per product: **Awaiting triage** (pending
  proposals) and **Needs a home** (rejected or no-fit). Both open the inbox scoped.
- **Initiatives**: every initiative, grouped by stage (default) or product, sorted by
  attention tier then updated time.
- **Queue**: the ranked cross-product list from the queue endpoint. Ships only when
  the backlog runner reads the same query (§12).

**Behaviour.**
- Issue rows collapse by default and load on expand via the leaves endpoint. The
  list is virtualized; a product with hundreds of open issues costs nothing until a
  capability is opened.
- Rows show: status rail and dot from the attention tier, title, and one muted line
  of context (counts for containers; capability and score for issues).
- Filters: text search, tier, lifecycle state, product. Persisted per scope like the
  sessions list.
- Keyboard, registered in `shell/shortcuts.ts` group `list-mode`: `j/k` move, `←/→`
  collapse/expand, `Enter` open, `a` add child, `e` edit, `d` add dependency, `x`
  select. Phone: the band shows list or detail, never both.
- Footer: `+ Product`, last sync time, and a sync button that toasts on completion
  (today it is silent).

## 7. The detail pane

Every level uses the page-migration Standard skeleton: `PageHeader` (breadcrumb as
`meta`, title, actions) → `Tabs` → `PageContent` with `Skeleton` → `EmptyState` →
content, errors as a `Banner` first child. Each level registers in the page-contract
table.

**Portfolio (nothing selected).** Tabs: Needs you (every pending human decision
across products, newest first, each a link), Ranked queue, All initiatives. Stat
tiles: products, in flight, blocked, need you. Needs you includes open, diagnosing
and scheduled incidents from the control-band queue, rendered by the existing
`IncidentQueue` rows with their triage-now, schedule and dismiss actions; it moves
here from the Initiatives tab when that tab is retired.

**Product.** Header actions: `+ Capability` (opens an inline row in the tree),
`+ Initiative`, Sync, overflow (edit, archive). Tabs: Overview (tiles, initiatives in
flight with `InitiativeTrack`, product dependencies), In flight, Needs you (gates,
proposals, blocked items, and incidents whose `repo` belongs to this product),
Ranked (this product's issues by score with inputs on hover), Dependencies
(product edges chip editor; the existing endpoints get their first UI).

**Capability.** Header: lifecycle `StatusPill`, doc link at SHA, proposed chip with
PR number when pending. Tabs: Overview (summary from the doc, links chip editor,
parent), Sub-capabilities, In flight, Issues (grouped by state, ordered by score,
with an "N awaiting triage" link), Needs you.

**Initiative.** Header: stage `StatusPill`, Advance with confirm dialog and a
Step-back action, overflow (edit title, capabilities). Tabs: Track
(`InitiativeTrack` on tokens plus artifact links; the next-action hint the backend
already computes is shown here; an initiative with `origin = control-band` shows
the incident it adopted, linked by `initiative_slug`), Epic (sub-issues with state, blockers, live session,
PR; deep link to the GitHub epic and board), Sessions (`buildSessions` from the detail
endpoint), Capabilities (primary and touched, editable).

**Wizard fixes (phase 1, on the existing page).** `await_map_pr` shows the PR link
and an Advance button wired to the existing advance-map endpoint; `await_triage`
links to the inbox scoped to the product and offers Advance; `register` offers an
add-repo form; the onboarding-state chip uses the wizard's friendly labels.

## 8. Inline editing

| Edit | Interaction | Write target |
|------|-------------|--------------|
| Add / rename capability, reparent, edit summary | Inline row input (`a`, `e`); row shows **proposed** + PR number until merge | Markdown on the product's map-changes branch (§9) |
| Capability links (`powered_by`, `integrates_with`) | Chip editor on Overview | Doc frontmatter, same PR |
| Create initiative; set primary and touched capabilities | `a` on a capability row; chips on the initiative header; drag ◆ row to another capability | ccswitch DB, immediate |
| Reassign an issue's capability | Click the capability chip on the row; type-ahead over the tree | Overlay PATCH, immediate |
| Edit scores | Click score chips; four labelled fields with computed score | Overlay PATCH; Score reaches the board via the reconciler |
| Add / remove issue dependency | `+ dep` on the row; pick a target from the tree or by number | GitHub native blocked-by through the reconciler journal (add exists; remove is new) |
| Product dependency | Chip editor on the product's Dependencies tab | ccswitch DB (endpoints exist) |
| Advance / step back a stage | Track button with confirm | ccswitch DB; back-synced to the board Stage field |

All writes are optimistic with rollback and toast, as the triage hooks do today.
Every mutation records an audit row with the acting identity.

## 9. Map-changes PRs

One branch and PR per product, `pm/map-changes-<slug>`, owned by ccswitch:

- The first UI edit creates the branch from the primary repo's default branch,
  writes the doc, and opens a non-draft PR titled "Capability map changes: <product>".
  Later edits push commits to it. The AI map-diff proposal (`ProposeFeatureMapDiff`)
  targets the same branch when one is open.
- A small table `feature_map_changes` records `project_id`, `branch`, `pr_number`,
  `opened_at`, `status ∈ open|merged|abandoned`. Branch content is truth; the row is
  a pointer.
- The feature index runs over the branch head as well as the default branch.
  Capabilities present only on the branch are indexed with `pr_number` and
  `commit_sha` set (these columns exist and are never written today) and render as
  proposed. Merge clears them on the next sync.
- The product header shows the open PR, its age, and offers Rebase when it is behind.
  Abandoning closes the PR and drops the pointer.
- Conflicts on push (someone edited the doc on master) surface as a `Banner` on the
  product with a link to the PR; the UI never force-pushes.

## 10. Data model and API

**Principle.** GitHub is truth for issues, epics and blocked-by. Markdown is truth
for capabilities. ccswitch owns the overlay only. New columns are nullable so
`_reconcile_columns` picks them up on both dialects; each schema change also gets a
migration revision chained off the newest.

**Model.**
- `features.parent_feature_id` (nullable self FK), read from a `parent:` frontmatter
  key by `featuredoc.py`. Cycles rejected at index time; an unknown parent indexes
  the doc at the root with a drift warning.
- `feature_links`: the parser already produces `FeatureLinkRef`; `feature_index`
  starts writing rows. Cross-product links resolve by `(project, feature)`.
- `initiative_features.is_primary` (nullable bool). The initiative's product is
  derived from its primary capability; the create and edit endpoints maintain it.
- `feature_map_changes` (§9).
- `Initiative` payload gains `projectSlug`, `primaryCapability`, `capabilities[]`.
- Cleanup in the same change: drop `initiative_repos` and the four unused score
  columns on `initiatives` (real migration, `_REQUIRED_EXTRAS` and reconcile lists
  updated).
- Untouched: `triage_items` and `GET /api/sdlc/triage` (#804) stay as they are; the
  UI reads them through the existing `useIncidents` hook.

**Read endpoints.**
- `GET /api/pm/map` → every product with its capability tree (id, slug, title,
  state, parent, links, doc SHA, proposed PR), initiatives (slug, stage, status,
  primary, touched, epic progress), and per-node rollup counts and attention tier.
  No issues. Invalidated on the existing `/ws` status frames and the sync loop.
- `GET /api/pm/leaves?capability=…|initiative=…` → issues and PRs under a node
  ordered by score, with epic children, active deps, triage state, live session.
- `GET /api/sdlc/initiatives/{slug}` exists (children, sessions, next action); it
  gains the capability fields and a frontend hook.

**Write endpoints.**
- `POST /api/pm/projects/{slug}/map-changes` with `{op: create|rename|reparent|
  summary|links, …}` → commits to the branch, opens or appends the PR, returns the
  pointer. `DELETE` abandons.
- `PATCH /api/sdlc/initiatives/{slug}` `{title?, primaryCapabilityId?,
  capabilityIds?}`; `POST /api/sdlc/initiatives/{slug}/step-back`.
- `POST/DELETE /api/pm/work-items/{id}/deps` → journal `add_blocked_by` /
  `remove_blocked_by` mutations; the reconciler applies them under its SHA fence.
- `POST /api/pm/triage/{id}/undo` → returns an accepted or rejected proposal to
  pending and the item to `proposed`, within a server-enforced window.
- Existing: overlay PATCH (gains UI for `featureId`), edges create/delete, advance-map.

## 11. Attention tier

`features/products/roadmapTier.ts`, pure, tested:

`needs-you` > `blocked` > `in-flight` > `proposed` > `live` > `idle`.

| Tier | Sources |
|------|---------|
| needs-you | initiative `awaiting_human`; artifact or map-changes PR open; proposal pending; item `needs_human`; board Status "Needs human"; incident `open` or `scheduled` for a repo of this product |
| blocked | active blocked-by edge with open blocker; initiative status `blocked`; board Status "Blocked" |
| in-flight | initiative stage between intake and deploy with status `active`; item with a live session; board "In progress"; incident `diagnosing` or `queued` |
| proposed | capability on an unmerged map-changes branch; initiative status `proposed` |
| live | capability state `live`; initiative `done`; item closed or merged |
| idle | everything else (Backlog, `unclassified` handled by the pseudo-nodes) |

A node's tier is the maximum of its own and its children's. Rollup counts (in
flight, blocked, needs you, awaiting triage) come from the same walk. The detail
pane shows the underlying values in words from the vocabulary module, never raw
snake_case.

## 12. Triage

Same chassis. List column: proposals grouped by product, scope chip when arrived
from a tree node, filters for confidence, `BulkActionBar` with **Accept selected**
(pointer equivalent of `A`). Detail pane: issue body excerpt, capability type-ahead,
four labelled score fields with computed score, rationale and confidence, who
proposed when; Accept, Save edits & accept, Reject. Keys unchanged. Accept and
reject fire immediately and show a six-second undo toast backed by the undo
endpoint. Bulk accept reports per-item failures. Rejected items land in **Needs a
home** with a Reclassify action that returns them to `unclassified` so the loop can
re-propose them.

## 13. Phasing

Each phase is its own implementation plan and PR series and leaves the app better
than it found it.

1. **Shell generalization and dead-end fixes.** §5, §7 wizard fixes, stage labels
   that match the track, sync toast, page-contract rows. No data changes.
2. **Backend for the tree.** §10 model changes, map and leaves endpoints, initiative
   payload, cleanup.
3. **Products master/detail.** Map scope without issue leaves, the three detail
   levels, Initiatives scope, retire the Initiatives nav with redirect. Deletes
   `projects.css`, `initiatives.css`, `InitiativesTab`, `ProjectsTab`; moves
   `IncidentQueue` to the portfolio and product Needs you tabs and onto tokens.
4. **Issue leaves and inline overlay edits.** Lazy virtualized leaves, score and
   capability edits, dependency add/remove via the reconciler, initiative edits and
   step-back.
5. **Map-changes PRs.** §9 and inline capability editing.
6. **Triage on the chassis; Queue scope.** §12. Queue ships behind spec 003 Phase C1
   (runner reads the queue query); until then the tab is hidden, not stubbed.

## 14. Testing

- Pure units first: `roadmapTier` and rollup, the tree reducer (expand, cursor,
  virtual window), the map-changes diff and frontmatter round-trip.
- API: map endpoint with nested capabilities, cross-product links, orphaned docs,
  cycles; leaves endpoint ordering and scoping; step-back guards; undo window.
- Map-changes against fake git and GitHub layers; conflict and abandon paths.
- Page-contract rows for portfolio, product, capability, initiative and triage.
- Full `npm run test` on every phase (shell changes touch mocked hooks that dozens
  of unrelated tests depend on), plus `typecheck`, `lint`, `build`.
- The four-step server gate (`ruff`, `mypy`, core pytest, server pytest) before every
  push; Postgres suite for phases that add a model.
- Playwright: drill from portfolio to an issue and back via breadcrumb; phone
  list/detail hand-off; triage accept with undo.

## 15. Dependencies and follow-ups

- Queue honesty depends on spec 003 Phase C1 (runner flips to the mirror query).
- Plan materialization (use case 12) is spec 003 Phase C2/C4; the initiative Epic
  tab is where it will surface.
- Automations is the next candidate for the same master/detail chassis once §5
  lands; not in this spec.
- Issue #1393's epic items are recorded on the KinDash Projects board (#27), not
  the ccswitch board (#26). Flagged for the owner; not addressed here.

## Appendix A: audit findings (condensed)

**Hierarchy and links.** `Initiative.project_id` not serialized
(`server/api/sdlc.py:29-44`); no features endpoint (`featureCount` only,
`server/api/pm.py:218`); `feature_links`, `initiative_features`, `initiative_repos`
dead; `Initiative` score columns dead; `Feature.commit_sha/pr_number/pr_url` never
written; `Project.onboarding_state ∈ {syncing, live}` and `Initiative.status ∈
{blocked, done}` never written; one-level hierarchy via
`WorkItem.parent_work_item_id`.

**Orphaned API and hooks.** `fetchInitiative` zero callers; `InitiativeEpic.children`
zero readers; `useTriage(projectSlug)` never scoped; `OverlayEdits.featureId` never
set; `GET /pm/projects/{slug}`, `POST/DELETE …/edges`, `POST …/advance-map`,
`GET /pm/queue`, `GET /pm/reconcile/journal` no frontend caller. (`GET /api/sdlc/triage`
was a stub at audit time; #804 landed the real incident queue the same day.)

**Dead ends.** Wizard `await_map_pr` / `await_triage` say "advance" with no control
and "below" for a queue on another page (`OnboardingWizard.tsx:52-54`); `register`
has no add-repo UI; `Sync now` no feedback; counts not links; Triage empty state has
no onward action; no detail routes, no edit/delete, no search/filter/sort anywhere.

**Vocabulary.** Nav "Products" vs everything else "project"; capability vs feature;
track labels Intent/Spec vs Advance button `intake`/`design`
(`InitiativeTrack.tsx:19-21`, `InitiativesTab.tsx:85`); onboarding state raw on the
card, friendly in the wizard; five status vocabularies (initiative status, triage
state, feature lifecycle, board Status, incident status); "triage" also means the
session attention tier (`session/triageTier.ts`).

**Interaction.** Triage `a`/`r`/`A` irreversible with no undo; `A` has no pointer
equivalent; the editor cannot change the proposed capability; four unlabelled
number inputs; bulk failures unreported. Advance has no confirm and no step-back.
Triage has no heading; Products and Initiatives have no `h1`; load-bearing text in
`title=` tooltips.

**Docs.** Two hierarchies (PM-layer §3 vs 003 D2); product board and roadmap planned
in phase 4 and never built; Phase D (#993) never planned; success metrics for the
area are "unknown"; no user stories anywhere in the corpus.
