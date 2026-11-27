/**
 * The display (kiosk) request gate (#3544) — the ONE place that knows a display session
 * may only read a closed list of routes.
 *
 * A display-scoped session is a wall: the server 403s every route outside
 * `DISPLAY_ROUTE_ALLOW` (`server/assistant/policy.py`). The shell still mounts its own
 * list/badge/workspace queries, and each one polled a dead endpoint on every tick for as
 * long as the wall was up. Rather than an `enabled:` copy in every hook, `api.get` asks
 * this gate before it touches the network, so a hook added tomorrow is covered unseen.
 *
 * The list is the server's own: `GET /api/auth/me` publishes it as `allowedReads` for a
 * display session, and `useAuthMe` hands it here on every poll — so there is no second
 * copy to drift, and a session that stops being a display lifts the gate.
 */
let allowedReads: readonly RegExp[] | null = null

/** Adopt the server-published allowlist; `null`/absent lifts the gate (a full session). */
export function setDisplayReads(patterns: readonly string[] | null | undefined): void {
  allowedReads = patterns ? patterns.map((p) => new RegExp(p)) : null
}

/** True when a display session may NOT `GET` this `/api`-relative path — never requested. */
export function displayGateBlocks(path: string): boolean {
  if (allowedReads === null) return false
  const route = `/api${path.split('?', 1)[0]}`
  return !allowedReads.some((rx) => rx.test(route))
}
