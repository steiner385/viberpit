// The product overview's hygiene notice (spec Sustain: health readout — server
// contract `GET /pm/projects/{slug}/health`, merged in #1597): capabilities and
// initiatives the map nesting doesn't reach, capabilities still `proposed` after
// their map-changes PR merged, inbox items no one triaged, and capability
// evidence gaps (#2162: dangling cited paths, untested live capabilities, code
// with no spec, a state older than its spec). Renders nothing
// when there is nothing to say — this is a notice, not a permanent fixture.
//
// Collapsed by default to ONE line of counts (2026-09-30 Roadmap critique, P1):
// expanded, it is up to 83 file-path links that sat above the product's Needs-you
// list — ~1,500px on a phone before the first actionable row — for what is one
// decision ("look at hygiene"), not 83. The title is the toggle, the same idiom
// `shell/DeployPanel` uses; the full list is one tap away and unchanged.
import { Fragment, useId, useState } from 'react'
import { Link } from 'react-router-dom'
import { useProjectHealth } from '@/api/hooks'
import { Banner } from '@/ui/Banner'
import { capabilityPath, initiativePath } from '../roadmapNavState'

function SlugLinks({ slugs, path }: { slugs: string[]; path: (slug: string) => string }) {
  return (
    <>
      {slugs.map((slug, i) => (
        <Fragment key={slug}>
          {i > 0 && ', '}
          <Link to={path(slug)} className="underline">{slug}</Link>
        </Fragment>
      ))}
    </>
  )
}

// pm_sync's classified constitution status for the product's primary repo
// (issue #2165) — see server/sdlc/constitution_status.classify_document.
const GOVERNANCE_MESSAGE: Record<string, string> = {
  missing: 'no ratified constitution — spec sessions have nothing to check governance against',
  template: 'the constitution is still the unfilled template — spec sessions have nothing real to check against',
  no_version_footer: "the constitution's Version / Ratified / Last Amended footer doesn't parse, so it can't be confirmed ratified",
}

/** How many summary segments the one-line title names before "+n more". */
const SUMMARY_SEGMENTS = 3

export function RoadmapHealthCard({ productSlug }: { productSlug: string }) {
  const { data: health } = useProjectHealth(productSlug)
  // Hooks before the early returns below (rules-of-hooks).
  const [open, setOpen] = useState(false)
  const listId = useId()
  if (!health) return null

  const {
    unparentedCapabilities, unlinkedInitiatives, staleProposed, staleInbox, unindexedDocs, governance,
    // Defaulted: an edge predating #2162/#2161/#2164 omits them.
    danglingEvidence = [], untestedCapabilities = [], codeWithoutSpecs = [], stateBehindSpec = [],
    artifactsEpicClosedNotDone = [], artifactsUnknownCapability = [], artifactsMissingFrontmatter = [],
    unlinkedPersonas = [],
  } = health
  const nothingToSay =
    unparentedCapabilities.length === 0 && unlinkedInitiatives.length === 0 &&
    staleProposed.length === 0 && staleInbox === 0 && unindexedDocs.length === 0 && !governance &&
    danglingEvidence.length === 0 && untestedCapabilities.length === 0 &&
    codeWithoutSpecs.length === 0 && stateBehindSpec.length === 0 &&
    artifactsEpicClosedNotDone.length === 0 && artifactsUnknownCapability.length === 0 &&
    artifactsMissingFrontmatter.length === 0 && unlinkedPersonas.length === 0
  if (nothingToSay) return null

  const capPath = (slug: string) => capabilityPath(productSlug, slug)

  // The one-line summary: each non-empty finding as "<count> <noun>", in the
  // list's own order, the first few named and the rest counted. The counts are
  // the same numbers the bullets below spell out — one source, two densities.
  const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`
  const segments = [
    governance ? 'governance' : null,
    unparentedCapabilities.length > 0 ? plural(unparentedCapabilities.length, 'capability outside the tree', 'capabilities outside the tree') : null,
    unlinkedInitiatives.length > 0 ? plural(unlinkedInitiatives.length, 'initiative with no capability', 'initiatives with no capability') : null,
    staleProposed.length > 0 ? plural(staleProposed.length, 'stale proposed capability', 'stale proposed capabilities') : null,
    staleInbox > 0 ? plural(staleInbox, 'stale inbox item', 'stale inbox items') : null,
    unindexedDocs.length > 0 ? plural(unindexedDocs.length, 'unparsed doc', 'unparsed docs') : null,
    danglingEvidence.length > 0 ? plural(danglingEvidence.length, 'stale evidence path', 'stale evidence paths') : null,
    untestedCapabilities.length > 0 ? `${untestedCapabilities.length} without tests` : null,
    codeWithoutSpecs.length > 0 ? `${codeWithoutSpecs.length} without spec` : null,
    stateBehindSpec.length > 0 ? plural(stateBehindSpec.length, 'state behind spec', 'states behind spec') : null,
    artifactsEpicClosedNotDone.length > 0 ? plural(artifactsEpicClosedNotDone.length, 'closed epic not done', 'closed epics not done') : null,
    artifactsUnknownCapability.length > 0 ? plural(artifactsUnknownCapability.length, 'unknown capability in frontmatter', 'unknown capabilities in frontmatter') : null,
    artifactsMissingFrontmatter.length > 0 ? plural(artifactsMissingFrontmatter.length, 'artifact without frontmatter', 'artifacts without frontmatter') : null,
    unlinkedPersonas.length > 0 ? plural(unlinkedPersonas.length, 'unresolved persona', 'unresolved personas') : null,
  ].filter((s): s is string => s !== null)
  const summary = segments.slice(0, SUMMARY_SEGMENTS).join(' · ')
    + (segments.length > SUMMARY_SEGMENTS ? ` · +${segments.length - SUMMARY_SEGMENTS} more` : '')

  // No explicit `role`: `Banner`'s `warning` tone already defaults to `status`
  // (its own docs say pass `'none'` only for a changing list, which this is
  // not) — leave the default rather than repeating it.
  return (
    <Banner
      tone="warning"
      title={
        <button
          type="button"
          aria-expanded={open}
          aria-controls={listId}
          onClick={() => setOpen((o) => !o)}
          className="text-left"
        >
          Roadmap hygiene — {summary}{' '}
          <span aria-hidden="true">{open ? '▾' : '▸'}</span>
        </button>
      }
    >
      {open && (
      <ul id={listId} className="flex flex-col gap-0.5">
        {governance && (
          <li>{GOVERNANCE_MESSAGE[governance] ?? `governance issue: ${governance}`}</li>
        )}
        {unparentedCapabilities.length > 0 && (
          <li>
            {unparentedCapabilities.length} {unparentedCapabilities.length === 1 ? 'capability' : 'capabilities'} outside the tree: {' '}
            <SlugLinks slugs={unparentedCapabilities} path={capPath} />
          </li>
        )}
        {unlinkedInitiatives.length > 0 && (
          <li>
            {unlinkedInitiatives.length} leaf {unlinkedInitiatives.length === 1 ? 'initiative' : 'initiatives'} with no capability: {' '}
            <SlugLinks slugs={unlinkedInitiatives} path={initiativePath} />
          </li>
        )}
        {staleProposed.length > 0 && (
          <li>
            {staleProposed.length} stale proposed {staleProposed.length === 1 ? 'capability' : 'capabilities'}: {' '}
            <SlugLinks slugs={staleProposed} path={capPath} />
          </li>
        )}
        {staleInbox > 0 && (
          <li>{staleInbox} inbox {staleInbox === 1 ? 'item' : 'items'} older than 7 days</li>
        )}
        {unindexedDocs.length > 0 && (
          <li>
            {unindexedDocs.length} feature {unindexedDocs.length === 1 ? 'doc' : 'docs'} could not be parsed and {unindexedDocs.length === 1 ? "isn't" : "aren't"} on the map: {' '}
            {unindexedDocs.map((d, i) => (
              <Fragment key={d.path}>
                {i > 0 && ', '}
                <span title={d.error}>{d.path}</span>
              </Fragment>
            ))}
          </li>
        )}
        {danglingEvidence.length > 0 && (
          <li>
            {danglingEvidence.length} cited evidence {danglingEvidence.length === 1 ? 'path' : 'paths'} no longer on the default branch: {' '}
            {danglingEvidence.map((d, i) => (
              <Fragment key={`${d.capability}:${d.kind}:${d.path}`}>
                {i > 0 && ', '}
                <Link to={capPath(d.capability)} className="underline" title={`${d.capability} · ${d.kind}`}>{d.path}</Link>
              </Fragment>
            ))}
          </li>
        )}
        {untestedCapabilities.length > 0 && (
          <li>
            {untestedCapabilities.length} live {untestedCapabilities.length === 1 ? 'capability cites' : 'capabilities cite'} no tests: {' '}
            <SlugLinks slugs={untestedCapabilities} path={capPath} />
          </li>
        )}
        {codeWithoutSpecs.length > 0 && (
          <li>
            {codeWithoutSpecs.length} {codeWithoutSpecs.length === 1 ? 'capability cites' : 'capabilities cite'} code but no spec: {' '}
            <SlugLinks slugs={codeWithoutSpecs} path={capPath} />
          </li>
        )}
        {stateBehindSpec.length > 0 && (
          <li>
            {stateBehindSpec.length} {stateBehindSpec.length === 1 ? 'capability has' : 'capabilities have'} a state older than {stateBehindSpec.length === 1 ? 'its' : 'their'} newest spec: {' '}
            <SlugLinks slugs={stateBehindSpec} path={capPath} />
          </li>
        )}
        {artifactsEpicClosedNotDone.length > 0 && (
          <li>
            {artifactsEpicClosedNotDone.length} {artifactsEpicClosedNotDone.length === 1 ? 'initiative' : 'initiatives'} {artifactsEpicClosedNotDone.length === 1 ? "has" : "have"} a closed epic but {artifactsEpicClosedNotDone.length === 1 ? "isn't" : "aren't"} marked done: {' '}
            <SlugLinks slugs={artifactsEpicClosedNotDone} path={initiativePath} />
          </li>
        )}
        {artifactsUnknownCapability.length > 0 && (
          <li>
            {artifactsUnknownCapability.length} artifact frontmatter {artifactsUnknownCapability.length === 1 ? 'capability names' : 'capabilities name'} no indexed feature: {' '}
            {artifactsUnknownCapability.map((a, i) => (
              <Fragment key={`${a.initiative}:${a.capability}`}>
                {i > 0 && ', '}
                <Link to={initiativePath(a.initiative)} className="underline" title={a.initiative}>{a.capability}</Link>
              </Fragment>
            ))}
          </li>
        )}
        {artifactsMissingFrontmatter.length > 0 && (
          <li>
            {artifactsMissingFrontmatter.length} {artifactsMissingFrontmatter.length === 1 ? 'initiative has' : 'initiatives have'} a committed artifact with no frontmatter: {' '}
            <SlugLinks slugs={artifactsMissingFrontmatter} path={initiativePath} />
          </li>
        )}
        {unlinkedPersonas.length > 0 && (
          <li>
            {unlinkedPersonas.length} unresolved persona {unlinkedPersonas.length === 1 ? 'reference' : 'references'}: {' '}
            {unlinkedPersonas.map((u, i) => (
              <Fragment key={`${u.capability}:${u.persona}`}>
                {i > 0 && ', '}
                <Link to={capPath(u.capability)} className="underline" title={u.capability}>{u.persona}</Link>
              </Fragment>
            ))}
          </li>
        )}
      </ul>
      )}
    </Banner>
  )
}
