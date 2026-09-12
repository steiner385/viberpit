# KinDash roadmap hierarchy: 5S plan (proposal, 2026-09-12)

Status: proposal awaiting owner decisions (see "Decisions" at the end). Nothing here is applied.

## What the hierarchy is today

Source of truth as the Roadmap section renders it (`GET /api/pm/map`, `GET /api/sdlc/initiatives`,
`GET /api/pm/triage?product=kindash`, all read 2026-09-12):

| Level | Count | State |
|---|---|---|
| Product | 1 (`kindash`, repo `cairnea/KinDash`) | onboarding state still `registered` despite 18 indexed features |
| Capabilities | 18, all top-level (zero `parent:`), all `state: proposed` | rendered alphabetically by slug (`server/pm/tree.py:245`) |
| Initiatives | 16, all `origin: epic-adoption`, **0 linked to any capability** | names are slugified epic titles (`epic-ui-audit-04-repair-...`) |
| Issues | 240 open; ~50 classified onto capabilities; 20 awaiting triage; 3 "needs a home" | the classified ones are mostly bot-filed (`flaky` 37, `forced-deploy-audit` 25, `ci-failure` 12) |

### Root causes of "random / overlapping / confusing"

1. **Four taxonomies, none the master.**
   - `parent:` frontmatter is the only grouping field ccswitch reads (`server/pm/featuredoc.py:92`). No KinDash doc sets it, so the tree is flat.
   - Every doc carries `area:` (11 distinct values across 18 docs: `ci-infrastructure` x7, `tasks` x2, `web-frontend` x2, `quality-engineering`, `ai`, `mobile-android`, `calendar`, `family`, `auth`, `frontend-platform`). The indexer ignores it. It was invented per doc by the agents that wrote them.
   - KinDash's own in-code taxonomy, `FEATURE_CATEGORIES` in `server/config/feature-gates.ts` (core, ai, financial, tasks, family, content, goals, integrations, gamification, notifications, calendar, communication), drives the Help Center, pricing and Discover panel. The feature map never references it.
   - GitHub labels: 266 of them. A `domain-*` family (17 labels, 0 open issues), bare topic labels (`tasks`, `calendar`, `family`, `ai`, `accessibility`, `ci-infrastructure`), `foundation:l0..l3`, `business-value:1..10`, `effort:N`, `phase:N`, `ui-audit:2026-09`, `ui-benchmark-2026`, `type:*`, `status:*`, and `depends-on:NNNN` labels next to GitHub-native blocked-by.
   - `docs/KinDash-Overview.md` lists ten "Core Features" (Dashboard, Task Management, Family Calendar, Financial Management, Shopping Lists, Meal Planning, AI Chat Assistant, Notifications & Communication, Gamification, Family Administration) that match none of the above by name.

2. **The 18 "capabilities" are three different kinds of thing at one level.**
   - Product capabilities a family member experiences (6): task-views, task-recurrence-lifecycle, family-calendar-administration, member-visual-identity, ai-assistant-chat, session-identity-integrity.
   - Delivery-system processes (7, all `area: ci-infrastructure`): ci-test-reliability, flaky-test-triage, ci-storage-and-cost-guardrails, dependency-maintenance, deploy-health-monitoring, forced-deploy-audit, merge-automation-controls. They exist because the pm-triage drift branch clusters whatever is noisiest, and the noisiest issues are bot-filed.
   - Time-boxed programs wearing capability clothes (5): ui-benchmark-2026, ui-framework-overhaul, web-accessibility-remediation, android-accessibility-remediation, accessibility-verification-coverage. Each already exists as an epic-adopted initiative (#15549 program, #15137-9, #15474 program), so the same work shows twice, unlinked.

3. **Naming has no rule.** Slug/title mismatch (`ui-benchmark-2026` is titled "2026 web experience benchmark"); a year in a slug; five naming shapes side by side (noun capability "Task views", remediation "Web accessibility remediation", control "Merge automation controls", audit "Forced-deploy audit", process "Flaky test triage"); hyphenation drift ("Forced-deploy audit" vs "Deploy health monitoring").

4. **Initiatives are flattened and prefixed.** GitHub holds a two-level epic tree (program #15474 with five phase epics; program #15549 with four). Adoption creates an initiative for every open board epic, so parent and children appear as siblings. Titles keep the conventional-commit prefix (`epic(ui-audit): 04 — ...`), and the slug is that prefix slugified.

5. **Lifecycle state carries no information.** All 18 are `proposed`, including capabilities that demonstrably shipped (member-visual-identity per ADR-019/ADR-021; merge-automation-controls; the forced-deploy audit ledger). The tier column therefore cannot rank anything.

6. **Docs are ledgers, not contracts.** 188 to 1096 lines each (mean ~535). `forced-deploy-audit` carries an "eighth re-verification" stamp. The roadmap's one-line summary is the first body paragraph, which for most docs opens with implementation trivia (`deploy-production.yml accepts force=yes ...`, `Renovate runs three times each weekday ...`) rather than an outcome.

## 1. Sort (Seiri): decide what belongs in the product map

One rule: **a capability is a standing ability a family member (or an operator of the product) experiences; an initiative is a bounded effort that changes one or more capabilities; a program's phases are sub-issues on GitHub, not capabilities.**

Apply it:

| Doc today | Verdict |
|---|---|
| task-views, task-recurrence-lifecycle, family-calendar-administration, member-visual-identity, ai-assistant-chat, session-identity-integrity | Keep as leaf capabilities; re-home under a parent. |
| ui-benchmark-2026 | Program, not capability. Becomes the standing capability **Experience standards**; the 2026 benchmark stays initiative #15549. |
| ui-framework-overhaul | Program name for a standing capability. Rename to **Design system & app shell**; #15137/#15138/#15139 become its initiatives. |
| accessibility-verification-coverage, web-accessibility-remediation, android-accessibility-remediation | One capability **Accessibility** (parent, from the verification doc) with two children **Web accessibility** and **Android accessibility**; the #15474 audit epics are its initiatives. |
| the 7 `ci-infrastructure` docs | Not family-facing, but one repo maps to one product (`server/pm/repo_project.py`), so they cannot move to a second product. Demote them to leaves under one parent, **Delivery pipeline**. |
| Evidence appendices and re-verification stamps | Red-tag: move to `docs/audits/`, keep the doc as the outcome contract. |
| Labels: `domain-*` (17, 0 open), `depends-on:*` (10), `Week N` and `v1.x` milestones (0 open) | Retire (ccswitch has `server/pm/label_retirement.py` for exactly this). |

## 2. Set in order (Seiton): one tree, one naming rule

Top level is anchored on `FEATURE_CATEGORIES` ids where a category exists, so the map, Help Center groups and topic labels share names. Two cross-cutting parents are added because they have no category.

```
kindash
├── tasks              Tasks & chores
│   ├── task-views
│   └── task-recurrence-lifecycle
├── calendar           Calendar
│   └── family-calendar-administration
├── family             Family administration
│   └── member-visual-identity
├── ai                 AI chat assistant
│   └── ai-assistant-chat
├── security           Security & privacy           (standing bar; matches labels coppa / domain-auth / domain-perm, ADR-014/016/022)
│   └── session-identity-integrity        (future leaves as their docs arrive: coppa-parental-consent, family-isolation, step-up-auth)
├── experience         Experience platform          (standing bar; matches labels design-system / UI/UX / accessibility)
│   ├── design-system            (was ui-framework-overhaul)
│   ├── accessibility            (was accessibility-verification-coverage)
│   │   ├── web-accessibility        (was web-accessibility-remediation)
│   │   └── android-accessibility    (was android-accessibility-remediation)
│   └── experience-standards     (was ui-benchmark-2026)
└── delivery           Delivery pipeline            (engineering; matches label ci-infrastructure)
    ├── ci-test-reliability
    ├── flaky-test-triage
    ├── deploy-health-monitoring
    ├── forced-deploy-audit      (slug kept: a workflow and a 25-issue label reference it)
    ├── merge-automation-controls
    ├── ci-storage-and-cost-guardrails
    └── dependency-maintenance
```

18 flat nodes become 7 parents and 17 leaves.

### Three kinds of parent, told apart by who the user is and what "done" means

| Kind | Parents | Whose experience | What `live` means | What an initiative under it looks like |
|---|---|---|---|---|
| Product feature | tasks, calendar, family, ai (and the other category ids as they arrive) | a family member | the outcome exists in the product | build a thing |
| Standing bar (quality attribute) | security, experience | a family member, but nobody buys it | the bar is enforced by a blocking gate; `delivering` = a remediation is in flight; `discovery` = measured, no gate yet | an audit, a remediation, a gate |
| Delivery system | delivery | the maintainer and the autonomous SDLC loop | the operator signal is trustworthy | a reliability or cost fix |

A standing-bar capability is never finished the way a feature is; its doc states the bar (WCAG 2.2 AA, COPPA, one identity per session) and the current measurement. Bot-filed and audit-filed issues route to the delivery and standing-bar leaves by label (`flaky`, `forced-deploy-audit`, `ci-failure`, `accessibility`, `coppa`), which is the pre-classification rule in Standardize. The other eight categories (financial, content, goals, integrations, gamification, notifications, communication, core) get a parent doc only when their first leaf arrives.

Naming rules (to be written into `docs/product/README.md` in KinDash):

- **Capability**: a noun phrase for the standing ability. No remediation verbs, no dates, no tool names. Title in sentence case; slug is the title kebab-cased, so the two never disagree.
- **Parent slug = category id** (`tasks`, `calendar`, ...). Child slugs do not repeat the parent word unless the word is the subject (`task-views` is fine).
- **Initiative**: `<Program> · <NN> <phase name>` on the GitHub epic title, no `epic(scope):` prefix. ccswitch slugifies the epic title, so fix it at the source.
- **First paragraph after the H1 is one outcome sentence.** That is what the roadmap shows as the summary; `## Problem` comes after it.
- **Frontmatter is `slug`, `title`, `state`, `parent`** (the four keys `featuredoc.py` reads or writes). Drop `area`; keep `owner`/`added` only if something reads them (nothing in ccswitch does).
- **Size cap ~150 lines.** Evidence beyond that goes to `docs/audits/<slug>-<date>.md` and is linked.

Mechanics: one map-changes PR on `cairnea/KinDash` (`POST /api/pm/projects/kindash/map-changes` with `create` / `rename` / `reparent`, landed in #1589; the UI for it is row 8c, so drive it by API or hand-write the branch). `parent:` is read at index time; an unknown parent indexes as a root, cycles are rejected.

## 3. Shine (Seiso): make the data true

- **States**: set each leaf honestly from evidence. Candidates for `live`: member-visual-identity, merge-automation-controls, forced-deploy-audit, deploy-health-monitoring, dependency-maintenance. `delivering`: task-views (#15727 in build), design-system (#15137 in deploy), ai-assistant-chat. `discovery`: experience-standards, accessibility children. Parents inherit the max of their children.
- **Link the 16 initiatives** (`PATCH /api/sdlc/initiatives/{slug}` with `capabilityIds`): #15137-9 to design-system; #15474-9 to accessibility (02 also to design-system); #15549-53 to experience-standards; #15727 to task-views.
- **Drain the inbox**: 20 pending proposals (mostly flaky and forced-deploy items whose capability is obvious); the 3 "needs a home" items are a `marketing-site-content` cluster that belongs to the separate `web` product (`cairnea/web`, currently `map_proposed`), not to KinDash.
- **Advance onboarding** from `registered` to `live` (`advance-map`, `advance-live`). The wizard was bypassed when the docs landed by hand, so the product reports as un-onboarded while carrying 18 features.
- **Trim the docs** to the size cap; move ledgers to `docs/audits/`. The ~40 duplicate re-verification PRs came from the frontmatter bug fixed in #15825; their residue is still in the docs.
- **Retire dead labels** with the label-retirement tooling.

## 4. Standardize (Seiketsu): write the rule where it gets read

- `docs/product/README.md` in KinDash: the tree above, the naming rules, the doc template, the size cap, where evidence goes. Cite `FEATURE_CATEGORIES` as the parent list.
- **ADR-023 "Product capability taxonomy"** so the tree is governed the way ADR-019/020/021 govern colour, locale and identity signals.
- **A feature-doc lint in KinDash CI** (next to the existing `feature-registry-validator`): frontmatter keys, parent exists, slug equals kebab(title), line cap, first paragraph before `## Problem`.
- **ccswitch changes** so the next drift proposal does not recreate the mess:
  1. The map-synthesis / feature-doc prompt writes `parent:` and the leading outcome sentence (it currently describes body sections only, which is why agents invented `area:`).
  2. pm-triage treats automation-authored issues (`flaky`, `forced-deploy-audit`, `ci-failure`, `ci-doctor`) as pre-classified onto the delivery parent instead of letting them drive new product capabilities.
  3. **Mirror epic nesting onto initiatives** (decision 4, resolved 2026-09-12: no filter, model it). A nullable
     `Initiative.parent_initiative_id` set by `adopt_epics` from the mirrored `WorkItem.parent_work_item_id`;
     program progress rolls up through descendants to leaf issues inside `_epic_progress_batch`; a program's stage
     and tier derive from its phases and are never edited (advance nudge and GitHub Stage back-sync apply to leaf
     initiatives only); API carries `parentInitiativeSlug` + `children`; the capability page nests program over
     phases and the Initiatives scope shows a program chip on each phase row. Same one-parent-link shape the
     capability tree uses (drill-down spec D2). GitHub nests sub-issues to eight levels, so render recursively.
  4. Initiative title normalisation strips a leading `epic(scope):` prefix; a phase's title is the phase name,
     since structure now shows the program.

## 5. Sustain (Shitsuke): make drift visible

- A roadmap-health readout (ccswitch automation or a Needs-you row): capabilities with no parent, initiatives with no capability, leaves `proposed` for more than 30 days, docs over the cap, inbox items older than 7 days.
- Quarterly tree review as a recurring issue on KinDash.
- Remove the `area:` key entirely once parents are in, so there is no second taxonomy to drift back into.

## Sequence and effort

| Phase | Work | Size |
|---|---|---|
| A | KinDash PR: 6 parent docs, 18 frontmatter edits, 5 renames, docs trimmed | one PR, an afternoon |
| B | API: states, initiative links, inbox drain, onboarding advance | an hour of calls |
| C | ccswitch: prompt writes `parent`, skip child epics, prefix strip, bot-issue pre-classification | two small PRs |
| D | KinDash: README + ADR-023 + doc lint; label retirement sweep | one PR plus a sweep |
| E | Health readout automation | one PR |

## Decisions (resolved 2026-09-12)

1. The seven delivery-pipeline docs stay in the map under one **Delivery pipeline** parent.
2. Slugs are the category ids from `feature-gates.ts`; display titles are the marketing names from `KinDash-Overview.md`
   (tasks = Task management, calendar = Family calendar, family = Family administration, ai = AI chat assistant).
3. The five renames are approved.
4. Sub-epics are neither filtered nor flattened: ccswitch mirrors GitHub's epic nesting onto initiatives (Standardize, item 3).
5. Applied 2026-09-12 (KinDash branch `docs/roadmap-taxonomy`): five leaf states were set from each doc's own
   Evidence rather than the table above — member-visual-identity and merge-automation-controls are `delivering`,
   deploy-health-monitoring, dependency-maintenance and forced-deploy-audit are `discovery`/`delivering`
   (forced-deploy-audit `delivering`: its one automated closer does not yet enforce the outcome). The rule
   "evidence beats the table" is the standing one; the table is the review's first cut.
