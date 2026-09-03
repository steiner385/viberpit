/**
 * The single owner of `document.title`.
 *
 * Two things want to write the title and neither may write it directly:
 *   - `ui/PageHeader` supplies the BASE title (the page's name).
 *   - `hooks/useNeedsYouTitle` supplies the needs-you COUNT.
 *
 * Before this module they both assigned `document.title` and clobbered each
 * other depending on effect order — the collision documented in PageHeader's
 * own source. Here they are two inputs to one composer, so the outcome does not
 * depend on which effect happens to run last.
 *
 * The base title is a mount-ordered stack, not a single slot, because two
 * `PageHeader`s can briefly coexist during a route transition (an outgoing
 * page mid-exit-animation while the incoming page has already mounted) and
 * either may unmount first. A caller claims a slot with a stable per-instance
 * token; the top of the stack is always the visible page's title, so an
 * unmount can neither clobber a newer claim (it isn't the top) nor blank the
 * title when an older claim is still underneath (releasing just exposes it).
 */
export const APP_NAME = 'ccswitch'

interface BaseEntry {
  token: symbol
  title: string
}

let baseStack: BaseEntry[] = []
let needsYou = 0

function currentBase(): string | null {
  return baseStack.length > 0 ? baseStack[baseStack.length - 1].title : null
}

function compose(): string {
  const base = currentBase()
  if (base === null) {
    // No page has claimed a title. Preserve the exact legacy string so nothing
    // regresses before pages adopt PageHeader in sub-spec C.
    return needsYou > 0 ? `(${needsYou}) ${APP_NAME} — needs you` : APP_NAME
  }
  const withApp = `${base} — ${APP_NAME}`
  return needsYou > 0 ? `(${needsYou}) ${withApp}` : withApp
}

function apply(): void {
  if (typeof document !== 'undefined') document.title = compose()
}

/**
 * Claim or update `token`'s base title. Called by `PageHeader` — once to
 * claim its slot on mount, and again on every subsequent render whose title
 * prop changed. A fresh token is pushed onto the stack (becoming the new
 * top); an existing token's entry is updated in place, so a title change on
 * an already-visible page doesn't reorder who is "on top" of a header
 * mounted after it.
 */
export function setBaseTitle(token: symbol, title: string): void {
  const existing = baseStack.find((e) => e.token === token)
  if (existing) existing.title = title
  else baseStack.push({ token, title })
  apply()
}

/**
 * Release `token`'s claim on the base title. Harmless if `token` is not the
 * current top of the stack — the remaining top wins, so a stale or
 * out-of-order unmount can neither clobber a newer claim nor blank the title
 * down to the app name while an older claim is still mounted underneath.
 */
export function releaseBaseTitle(token: symbol): void {
  baseStack = baseStack.filter((e) => e.token !== token)
  apply()
}

/** Called by `useNeedsYouTitle`. */
export function setNeedsYouCount(n: number): void {
  needsYou = Number.isFinite(n) && n > 0 ? Math.floor(n) : 0
  apply()
}

/** Test-only: clear both inputs and reset the title. */
export function resetDocumentTitle(): void {
  baseStack = []
  needsYou = 0
  apply()
}
