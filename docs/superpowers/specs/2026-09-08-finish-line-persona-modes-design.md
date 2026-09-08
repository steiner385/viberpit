# ViberPit finish line: persona modes, a four-section nav, and an explicit parking lot — design

**Date:** 2026-09-08 · **Status:** approved design (operator: "proceed with your recommendation"),
pre-implementation · **Owns:** the closure of the UI target state
(`2026-09-02-ui-target-state-design.md`) and the roadmap drill-down
(`2026-09-07-roadmap-drilldown-design.md`) as one deliverable, plus the board hygiene that
keeps agents pointed at it.

**Supersedes where they conflict:** roadmap drill-down §4 (two nav entries → one; Triage
becomes a list scope, not a section) and D10; page-migration PR 6 (Automations) and PR 8
(Insights), which are replaced by the Delivery section; target-state §4.3's variant rows for
Automations, Initiatives, Products and Insights.

---

## 1. Problem

An operator-side review on 2026-09-08 found that the product feels stalled even though master
took ~400 commits in the previous fourteen days. The causes, with evidence:

- **Every spec was executed foundation-first and stopped before its visible layer.** Target-state
  sub-specs A (tokens) and B (shell) landed whole; sub-spec C stopped after Settings, Products,
  Initiatives and Triage. Pipeline, Automations, Insights and the archived Reader are still legacy
  components hosted verbatim by `routes.tsx`, and the palette sweep never ran (92 `.tsx` files still
  carry `dark:`). Roadmap drill-down landed Phases 1 and 2 (shell generalization, tree backend) and
  stopped before Phase 3, the master/detail page that is the whole point.
- **Three generations of one PM idea share the nav.** Initiatives, Triage and Products are three
  pages from three specs with two incompatible hierarchies. The roadmap spec already diagnosed
  this; nobody executed the collapse.
- **The pages show stale or raw data.** Initiatives lists shipped AI-assistant phases as
  "Active · Build" with an unpressed Advance button; Products is a wall of "blocked by" text with
  no links; Insights is two raw `<select>`s and four tables; Triage is 48 proposals for one repo with
  no grouping.
- **Automations compete with the operator for attention.** Six of the fourteen registered
  automations are enabled in the live edge, the Backlog Runner and PR Doctor in `auto` mode. Until #1465 their sessions were distinguishable from human ones only by a 12px
  glyph; a feature-doc parse bug (#1462) produced ~40 duplicate "propose capability" sessions.
  Both PRs are now merged or armed. The Backlog Runner (mode `auto`) reports "6 implementing,
  62 eligible": it pulls from the whole open backlog, which is why effort is diffuse.
- **There is no finish line.** 77 open issues, no milestones, no repo project; the user-level
  ViberPit board (#26) marks most open items Blocked; the 20 UI-audit issues (#1399–#1418) were
  filed onto the KinDash board (#27) by mistake. Nothing says what "done" is.

## 2. Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | **Personas are modes, not users.** One operator wears three hats; the UI serves each hat with one primary surface answering one question. | Multi-user is a v1 non-goal in every feature doc. Splitting by persona is an information-architecture device, not an auth model. |
| D2 | **Five nav entries:** Sessions · Pipeline · Roadmap · Delivery · Settings. Initiatives, Triage, Products and Insights leave the rail. | Seven sections, three of them the same idea, is the confusion the operator named. Each remaining entry maps to a persona question (§3). |
| D3 | **Roadmap** is the roadmap drill-down spec's Products master/detail (its Phase 3) at `/roadmap`, with **Inbox** (today's Triage) as a fourth list scope beside Map · Initiatives · Queue. The pending-proposal badge moves to the Roadmap entry. | The triage keystrokes and the proposal detail are a work mode inside the roadmap, not a place. Same chassis, one fewer section. |
| D4 | **Delivery** is a new master/detail section at `/delivery` on the Sessions chassis: list scopes **Automations · CI health**; detail = one automation (status, schedule, events, spawned sessions, config) or one repo's check health (today's Insights tables). It replaces the Automations and Insights pages. | The project-manager question ("is delivery healthy, what is running, what is blocked?") is spread over two thin pages today. The roadmap spec §15 already names Automations as the next master/detail candidate. |
| D5 | **Finish the page migration**: Pipeline → Workspace (page-migration PR 10), archived review → Reader (PR 7 remainder), then the repo-wide sweep (PR 11). Automations and Insights migrate by *becoming* Delivery (D4), so PR 6 and PR 8 are not executed as written. | These are the pages the operator looks at every day; leaving them on the legacy palette is the visible half of "no UI improvement". |
| D6 | **Stale stage data is an attention problem, not a sync problem.** An initiative whose build tasks are all closed but which has not been advanced rolls up to tier `needs-you` ("Advance"), so it surfaces in Roadmap's Needs-you tab instead of reading as calm "Active". No auto-advance. | SDLC governance keeps human advance as the gate; the fix is to make the wait visible, which the roadmap spec's tier rollup (D9, §11) already provides. |
| D7 | **Take ViberPit out of the Backlog Runner for the duration** (remove `steiner385/viberpit` from its `enabled_repos`; today `eligible_label` is blank, which means "any open issue"). Finish-line rows are executed as sequenced plans by dispatched sessions, not pulled from the backlog. Finish-line issues get a `finish-line` label; parked issues get `parked` and a one-line reason. | The runner's `eligible_label` gate is global across its twelve repos, so gating on `finish-line` would starve KinDash. Removing one repo is the smallest reversible change; the label still tells a human or a dispatcher what is in scope. |
| D8 | **Park, do not close.** Assistant v2 Phases 7–10, rebrand Phases 2b–7, continuity 002 Phases 2–4, projects 003 Phase C, UI-audit epics E3–E5, and every `needs-human` infrastructure issue stay open with `parked` and a reason. The UI-audit issues move from board #27 to board #26. | A parked decision is visible; a silently growing backlog is not. Closing would lose the plans. |
| D9 | **Vocabulary.** Nav labels are `Sessions`, `Pipeline`, `Roadmap`, `Delivery`, `Settings`. Inside Roadmap the roadmap spec's D11 words hold (Product, Capability, Initiative, Epic, Issue) plus `Inbox` for the proposal queue. Code ids stay `projects`, `pm`, `automations`, `insights` where renaming would cascade through the API. | Labels come from `navDefs` and `features/products/vocab.ts`; copy cannot drift. |
| D10 | **Nothing new below the line.** No new server capability, no new automation, no new assistant pack, no schema beyond what roadmap Phase 3 already needs. | The point is to finish, not to widen. |

## 3. Personas as modes

| Mode | Question it answers | Primary surface | State today | Work in this spec |
|------|--------------------|-----------------|-------------|-------------------|
| Developer / vibe coder | What needs me right now? What are my sessions and PRs doing? | Sessions (composer, permission cards, Background scope) · Pipeline | Built; Pipeline on legacy palette | D5: Pipeline → Workspace |
| Product manager | What are we building, how does it fit together, in what order? | Roadmap: portfolio → product → capability → initiative → issues; Inbox for proposals | Three disconnected pages; tree backend landed, no page | D3: roadmap Phase 3 + Inbox scope |
| Project manager | What is in flight, blocked or waiting on a human? Is delivery healthy? | Roadmap Needs-you tabs · Delivery (automations, CI health) | Automations + Insights are thin legacy pages; needs-human is scattered | D4: Delivery; D6: tier rollup |
| Operator / admin | Accounts, hosts, deploy, config | Settings (16 sections, split variant) | Migrated | none |

## 4. Information architecture

### 4.1 Nav (`web/src/shell/navDefs.ts`)

| id | label | path | badge | list | kiosk |
|----|-------|------|-------|------|-------|
| `sessions` | Sessions | `/sessions` | sessions | yes | no |
| `pipeline` | Pipeline | `/pipeline` | pipeline | no | yes |
| `roadmap` | Roadmap | `/roadmap` | roadmap (pending proposals) | yes | no |
| `delivery` | Delivery | `/delivery` | automations (actionable items) | yes | CI-health scope only, once verified read-only |
| `settings` | Settings | `/settings` | settings | no | no |

Rows removed: `automations`, `initiatives`, `triage`, `projects`, `insights`. `NavId` is derived
from the array, so every consumer that mentions a removed id fails to compile; that is the
purge list.

### 4.2 Routes and redirects

| Path | Shows |
|------|-------|
| `/roadmap` | Portfolio: Needs you · Ranked · All initiatives (roadmap spec §7) |
| `/roadmap/:slug` | Product detail |
| `/roadmap/:slug/capabilities/:capSlug` | Capability detail |
| `/initiatives/:slug` | Initiative detail (kept, per roadmap spec §4) |
| `/roadmap?scope=inbox` (+ `&proposal=<id>`) | Inbox list + proposal detail with the triage key map |
| `/delivery` | Automations scope, welcome pane |
| `/delivery/automations/:id` | One automation |
| `/delivery/ci/:owner/:repo` | One repo's check health |

Redirects (`replace`): `/projects` and `/projects/*` → `/roadmap` and the matching product path;
`/initiatives` → `/roadmap?scope=initiatives`; `/triage` → `/roadmap?scope=inbox`;
`/automations` → `/delivery`; `/insights` → `/delivery?scope=ci`. Legacy redirects already in
`routes.tsx` stay.

### 4.3 Roadmap list column

Scopes: **Map · Initiatives · Inbox · Queue**. Map, Initiatives and Queue are exactly the
roadmap spec §6 (Queue stays hidden until spec 003 Phase C1). Inbox rows are triage proposals
(`TriageItem`), grouped by product then capability, with the existing `j/k a r e x A`
key map; the detail pane is the proposal editor the roadmap spec §12 describes. Undo per
roadmap spec §12.

### 4.4 Delivery

- **List, Automations scope:** one row per registered automation: name, enabled/mode pill,
  one status line (the same string the card shows today), work class, and an attention tier
  (needs-you when it has actionable items, blocked when its schedule window is closed, in-flight
  when it spawned a live session, idle otherwise). Sorted by tier then name.
- **Detail, automation:** header (name, mode selector, enable switch), tabs **Status · Schedule ·
  Events · Sessions · Config**. Status = today's card body plus actionable items with their
  actions. Schedule = `ScheduleEditor`. Events = recent events. Sessions = `useAutomationSessions`
  rows linking to `/sessions/background/:id` or `/sessions/:id` by work class. Config = the
  automation's `ConfigField`s (this is where `eligible_label` becomes visible).
- **List, CI health scope:** one row per repo the pipeline tracks, with the 7-day flaky count
  and slowest-check delta as the meta line.
- **Detail, repo:** today's four `CheckTable`s on `PageContent`, window selector in
  `PageHeader.actions`.
- Deleted: `automations/AutomationsTab.tsx` (+ test), `components/InsightsView.tsx`, both nav
  rows, `NavBadgeKey 'automations'` renamed `delivery`.

### 4.5 Attention tier for initiatives (D6)

In `server/pm/tree.py`, an initiative node whose stage is `build` and whose epic progress is
`closed == total > 0` gets tier `needs-you` with reason `advance`. The Roadmap Needs-you tab
renders the reason as the row's meta ("Ready to advance → Deploy"). Sessions-style tone mapping
is unchanged.

## 5. Sequence

Each row is one PR series with its own implementation plan; each leaves master shippable.

| # | Work | Spec of record | Deletes | Closes |
|---|------|----------------|---------|--------|
| 0 | Noise: merge #1462, arm #1465 | — | — | done 2026-09-08 |
| 1 | Board hygiene: `finish-line` and `parked` labels, ViberPit removed from the runner's repos (D7), audit issues moved to board #26, one tracking issue per row below | this spec §6 | — | — |
| 2 | Roadmap Phase 3 at `/roadmap`: Products master/detail, Map + Initiatives scopes, three detail levels, D6 tier, redirects | roadmap spec §6–§7, §11, Phase 3 | `projects/`, `initiatives/InitiativesTab.tsx`, `initiatives.css`, `projects.css`, `IncidentQueue` moves to Needs-you | #993 |
| 3 | Inbox scope: Triage onto the Roadmap chassis with key map and undo; delete the Triage section | roadmap spec §12 | `triage/TriageInbox.tsx`, `triage.css`, nav row | — |
| 4 | Delivery section: Automations + CI health master/detail; delete Automations and Insights sections | this spec §4.4 | `AutomationsTab`, `InsightsView`, two nav rows | page-migration PR 6, PR 8 |
| 5 | Pipeline → Workspace variant on the chassis | page-migration PR 10 | `.prdash-root` remap, `pipeline/kiosk.ts`, `useKioskCycle` | — |
| 6 | Archived review → Reader; sub-agent view → Reader | page-migration PR 7 (remainder) | inline dialogs in review | — |
| 7 | Sweep: `dark:`, raw palette, hex, `z-*` literals, ESLint rule to repo scope, `components/` bucket emptied, `docs/ACCESSIBILITY.md` and `web/README.md` rewritten | page-migration PR 11 | `components/`, `lib/useMediaQuery.ts` | target-state §9 |
| 8 | Roadmap Phases 4–5 (issue leaves, inline edits, map-changes PRs) | roadmap spec Phases 4–5 | — | — |

Rows 2–4 are the visible payoff and go first. Rows 5–7 close the target-state spec. Row 8 is
inside the line but last; if capacity runs out it is the only row that may slip without
breaking the nav.

## 6. The parking lot

Labelled `parked`, each with a one-line reason comment, and `finish-line` removed if present:

- AI assistant v2 Phases 7–10 (#1224, #1231, #1237, #1245 and their sub-issues, #1246–#1250):
  *assistant works; packs add breadth, not clarity.*
- Rebrand Phases 2b–7 (#1368–#1378) and epic #1365: *display surfaces are already ViberPit;
  the rest is internal identifiers and a host window.*
- Continuity 002 Phases 2–4 (#947–#949, epic #945): *Phase 1 liveness shipped; the rest needs
  a human drill window.*
- Projects 003 Phase B/C/ops (#988, #990, #992, #996) and Phase D hygiene (#994, #995): Phase D
  portfolio UI (#993) is row 2 above and is *not* parked.
- UI-audit epics E3–E5 (#1396–#1398) and their leaves (#1408–#1418): *evidence and kiosk-trust
  work after the surfaces they measure are settled.* E1–E2 leaves (#1399–#1407) get
  `finish-line` only where a row in §5 closes them; the rest are parked with a pointer.
- Infrastructure `needs-human` issues (#816, #821, #831, #832, #838, #840, #851, #1010–#1014,
  #1023, #1035, #1273, #1383, #1432–#1434): *unchanged; they are not UI work.*

`finish-line` goes on: #993, the tracking issue filed for each of rows 3–8, and the E1/E2
leaves a row closes (#1400 narrow-title, #1404 draft preservation, #1405 query-failure states,
#1406 pipeline anchors are candidates; the plan for each row decides).

## 7. Testing and gates

- Every page in §4.2 is a row in `pageContract.test.tsx` (one `h1`, `document.title`,
  `data-page-variant`, header while loading, no horizontal overflow) at 390, 820 and 1440.
- `navDefs.test.ts` pins the five ids and the kiosk-safe set (`pipeline`, and `delivery` only
  after the CI-health scope is verified read-only per the `showInKiosk` doc comment).
- Redirect table test: every path in §4.2's redirect list resolves to its target.
- Full `npm run typecheck`, `lint`, `test`, `build` on every row (shell changes touch mocked
  hooks in ~47 unrelated test files). Four-step server gate on rows that touch `server/`.
- Playwright: portfolio → capability → initiative → issue and back; Inbox accept with undo;
  Delivery automation toggle; phone list/detail hand-off on Roadmap and Delivery.
- Success criteria carried from target-state §9: `grep -c 'dark:' web/src` → 0 outside
  `index.css`; raw palette → 0 outside the chart allowlist; `components/` empty;
  `web/dist` CSS contains every token utility.

## 8. Non-goals

Anything in §6. Also: dates or milestones on the roadmap (roadmap spec D5), a public design
system, new assistant tools, any change to session engines, and any server API beyond what
roadmap Phase 3 and D6 need.
