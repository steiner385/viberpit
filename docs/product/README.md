# ViberPit product hierarchy

ViberPit helps operators run and steer coding sessions across accounts and hosts,
with usage-aware rotation, isolated workspaces, and governed delivery.

The planning spine is **Product → Capability → Initiative → Issue**. Capabilities
may contain sub-capabilities. An initiative and its GitHub epic are two
representations of one effort, not separate levels.

| Concept | Meaning and source of truth |
|---|---|
| Product | An independently managed product, registered with its repositories; a GitHub Project v2 provides its board. |
| Capability | A standing ability or quality standard, recorded in versioned Markdown under `features/`. |
| Initiative | A bounded outcome with acceptance criteria; one GitHub epic and its lifecycle overlay. A pre-build initiative may await epic materialization. |
| Program (optional) | A coordinated group of initiatives, represented by a parent GitHub epic and its overlay. Phases are child initiatives where independently scoped outcomes require them. |
| Issue | A bounded change, bug, or investigation, with native GitHub parent and dependency relationships. |
| PR / session | Implementation and execution evidence linked to work; neither adds a planning level. |

A maintenance issue can attach directly to a capability. An initiative has one
primary capability and may touch others. Programs and phases preserve actual
GitHub nesting; they do not introduce a mandatory level. Dependencies remain
separate from parents and ordering. Count unique descendant leaf issues for
program progress, and derive program stage from its phases rather than advancing
the container independently. Runtime support for that rollup belongs to the
[initiative nesting work](https://github.com/steiner385/viberpit/pull/1597).

## Capability map

The six parents below organize the existing capabilities. The `parent:` field in
each capability document is the canonical membership record; this index links to
the parents rather than maintaining a second membership registry.

- [Session workspaces](features/session-workspaces.md) — Run concurrent coding sessions, preserve their context, and return to the same work from any device.
- [Accounts and engines](features/accounts-and-engines.md) — Choose an agent engine and keep work moving across healthy accounts with available usage.
- [Operator experience](features/operator-experience.md) — Understand and steer coding work through accessible interfaces, timely notifications, and useful guidance.
- [Planning and automation](features/planning-and-automation.md) — Turn product outcomes into prioritized, governed work that agents can execute with human oversight.
- [Security and isolation](features/security-and-isolation.md) — Keep access authenticated, credentials protected, accounts isolated, and session actions within approved permissions.
- [Fleet and delivery operations](features/fleet-and-delivery-operations.md) — Keep the session fleet healthy and make the path from tested changes to running software observable and recoverable.

[Child sessions](guides/child-sessions.md) is supporting documentation linked from
[Session tools](features/session-tools.md), not a separate indexed capability.

The ViberPit rebrand is a finite [initiative (#1365)](https://github.com/steiner385/viberpit/issues/1365)
under Operator experience. Its [migration guide](guides/viberpit-rebrand.md) preserves
design and delivery evidence outside the indexed capability directory. Remaining
engineering issues stay with the initiative and their durable capability homes.

## Personas

A capability whose bold **Target users** paragraph describes a persona may
also name that persona's slug in its `personas:` frontmatter list. Each slug indexes
to a `docs/product/personas/<slug>.md` document (issue #2164), parsed the same
way capability docs are: YAML frontmatter (`slug`, `title`, `role`, `age_band`,
`tier`) plus a free-form body (goals, constraints, protections). Only `slug` is
always required, and it falls back to the filename; a doc with a frontmatter
fence also needs a `title` (or a `# heading` to fall back to), while a fenceless
doc may have none. The rest are optional. pm-sync indexes personas into a
`Persona` table per product, default-branch-only — there is no map-changes-branch proposal flow
for personas, unlike capabilities. A capability's declared `personas:` slug
that doesn't resolve to a live Persona row surfaces on `GET
/pm/projects/{slug}/health` as `unlinkedPersonas`, rendered on the roadmap
hygiene banner. `GET /pm/projects/{slug}/personas` lists a product's indexed
(non-orphaned) personas.

ViberPit does not run persona reviews itself — this just gives the persona an
indexed identity a capability doc, a spec's acceptance criteria, or a test can
cite.

## Maintaining the hierarchy

- Search the existing map before adding a capability. Use noun-phrase titles for
  standing abilities, outcome-oriented initiative titles, and one bounded result
  per issue. Keep stable capability and initiative slugs when improving display text.
- Start capability bodies with the user or operator outcome. Use the existing
  parser’s `slug`, `title`, `state`, `parent`, and supported `links` frontmatter;
  do not create a second area registry. Evidence sections can retain implementation
  detail without making it the summary.
- Propose taxonomy changes through the existing map-change PR workflow and product-map
  curation gate. Preserve reference integrity when moving or retiring a capability.
- Keep capability maturity, initiative lifecycle, and issue workflow distinct.
  Closed issues or merged PRs alone do not prove deployment or evaluation acceptance.
  Review open parents with completed descendants and closed parents with open
  descendants against their acceptance evidence before changing status.
- Keep parked work in backlog. Archive, cancel, or close only with the corresponding
  evidence; similar error titles alone do not prove duplication. Route operational
  reports to their owning capability using scope evidence, not broad `auto-error`
  labels. Reuse the existing label-routing mechanism when it is available.
- Audit for initiative/product ownership mismatches, active leaf initiatives without
  primary capabilities, missing or cyclic capability parents, and prose-only epic
  memberships. Six intentional roots are valid. Track inbox age and unclassified
  work separately from delivery progress.
- Extend the existing roadmap health readout for uncovered drift checks. Do not add
  a parallel hierarchy or progress implementation. These are maintenance rules;
  they do not create recurring automation.

The terminology is shared with [GitHub Projects integration](../../specs/003-gh-projects-integration/spec.md)
and the [roadmap design](../superpowers/specs/2026-09-07-roadmap-drilldown-design.md).
