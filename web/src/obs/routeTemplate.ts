// web/src/obs/routeTemplate.ts
/**
 * The route SHAPE of a URL — `/sessions/:id` — with every identifier removed.
 *
 * ## Why this is not `location.href`
 *
 * `clientLogger` ships the full URL because an error report is useless without
 * knowing exactly where it happened, and the tailnet identity that sent it is
 * already in the record. Field performance is the opposite trade: the samples
 * are aggregated across everybody and kept indefinitely, and issue #1411 is
 * explicit that they may carry no prompts, tokens or account names. A session
 * URL is `/sessions/<uuid>`, a product URL is `/roadmap/product/<slug>` — every
 * one of those tails is an identifier, and none of them adds anything to a p75.
 * So the whole point of the segmentation key is the shape, not the instance.
 *
 * ## Why the param map rather than a pattern list
 *
 * The obvious implementation is a list of path patterns to match against. That
 * is a second copy of `shell/routes.tsx`, and the day a route is added it goes
 * stale silently — the new route's samples land under a catch-all and nobody
 * notices. Instead the caller hands us react-router's own `useParams()`: the
 * params ARE the identifiers the route table declared, so a new `:whatever`
 * route is redacted correctly the moment it exists, with nothing to update here.
 *
 * `scrubSegment` is defence in depth behind that, not the primary mechanism —
 * it catches an id that reached the path without being a declared param.
 */

/** Longest route in the app is 4 segments (`/sessions/:id/agents/:agentId`); 6
 *  leaves room and bounds the cardinality of the `route` dimension in storage. */
const MAX_SEGMENTS = 6

/**
 * Does this segment look like an instance rather than a place?
 *
 * Every static segment in the app's route table is word-shaped — lowercase
 * letters and hyphens (`sessions`, `permission-profiles`, `background`). Every
 * identifier that reaches a URL carries digits: a uuid, an issue number, a
 * generated slug. So "contains a digit" is the whole test, plus a length guard
 * so a future `/v2/…` style segment is not mistaken for an id.
 */
function looksLikeId(segment: string): boolean {
  if (segment.length <= 2) return false
  return /\d/.test(segment)
}

export function routeTemplate(
  pathname: string,
  params: Readonly<Record<string, string | undefined>>,
): string {
  // The query string is where a prompt or a token would be; it never survives.
  let path = pathname.split(/[?#]/, 1)[0]
  if (!path.startsWith('/')) path = `/${path}`

  // Splat params span several segments, so they have to be substituted on the
  // whole string before it is split. `*` is not a legal placeholder name in the
  // output (it reads as a wildcard), hence `:splat`.
  const splat = params['*']
  if (splat) {
    const suffix = splat.startsWith('/') ? splat : `/${splat}`
    if (path.endsWith(suffix)) path = `${path.slice(0, -suffix.length)}/:splat`
  }

  const byValue = new Map<string, string>()
  for (const [name, value] of Object.entries(params)) {
    if (name === '*' || !value) continue
    byValue.set(value, `:${name}`)
  }

  const segments = path.split('/').filter(Boolean)
  const truncated = segments.length > MAX_SEGMENTS
  const kept = truncated ? segments.slice(0, MAX_SEGMENTS - 1) : segments

  const out = kept.map((seg) => {
    const named = byValue.get(seg)
    if (named) return named
    if (seg.startsWith(':')) return seg
    return looksLikeId(seg) ? ':id' : seg
  })
  if (truncated) out.push('…')

  return `/${out.join('/')}`
}
