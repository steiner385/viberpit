// The product overview's hygiene notice (spec Sustain: health readout — server
// contract `GET /pm/projects/{slug}/health`, merged in #1597): capabilities and
// initiatives the map nesting doesn't reach, capabilities still `proposed` after
// their map-changes PR merged, and inbox items no one triaged. Renders nothing
// when there is nothing to say — this is a notice, not a permanent fixture.
import { Fragment } from 'react'
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

export function RoadmapHealthCard({ productSlug }: { productSlug: string }) {
  const { data: health } = useProjectHealth(productSlug)
  if (!health) return null

  const { unparentedCapabilities, unlinkedInitiatives, staleProposed, staleInbox, unindexedDocs, governance } = health
  const nothingToSay =
    unparentedCapabilities.length === 0 && unlinkedInitiatives.length === 0 &&
    staleProposed.length === 0 && staleInbox === 0 && unindexedDocs.length === 0 && !governance
  if (nothingToSay) return null

  const capPath = (slug: string) => capabilityPath(productSlug, slug)

  // No explicit `role`: `Banner`'s `warning` tone already defaults to `status`
  // (its own docs say pass `'none'` only for a changing list, which this is
  // not) — leave the default rather than repeating it.
  return (
    <Banner tone="warning" title="Roadmap hygiene">
      <ul className="flex flex-col gap-0.5">
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
      </ul>
    </Banner>
  )
}
