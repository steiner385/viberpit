// The product overview's hygiene notice (spec Sustain: health readout — server
// contract `GET /pm/projects/{slug}/health`, merged in #1597): capabilities and
// initiatives the map nesting doesn't reach, capabilities still `proposed` after
// their map-changes PR merged, inbox items no one triaged, and capability
// evidence gaps (#2162: dangling cited paths, untested live capabilities, code
// with no spec, a state older than its spec). Renders nothing
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
    </Banner>
  )
}
