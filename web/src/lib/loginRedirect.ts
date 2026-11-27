/**
 * The passkey login page (#1410 FR-G12.12) and the one rule for where it sends
 * a user afterwards. The API client builds the `/login?next=…` URL on a passkey
 * 401; the login page reads `next` back through `safeNextPath`.
 */
export const LOGIN_PATH = '/login'

const HOME = '/'

/**
 * `raw` if it is a same-origin path, else `/`. Only a value that starts with a
 * single `/` qualifies — `//host` and `/\host` are protocol-relative to a
 * browser — and it is re-parsed against this origin to be sure. The login page
 * itself is never a destination, so a stale `next` cannot loop.
 */
export function safeNextPath(raw: string | null | undefined): string {
  if (!raw || !raw.startsWith('/') || raw.startsWith('//') || raw.startsWith('/\\')) return HOME
  let url: URL
  try {
    url = new URL(raw, window.location.origin)
  } catch {
    return HOME
  }
  if (url.origin !== window.location.origin || url.pathname === LOGIN_PATH) return HOME
  return `${url.pathname}${url.search}${url.hash}`
}

/** `/login?next=<current>`, or bare `/login` when `next` would only point home. */
export function loginUrlFor(current: string): string {
  const next = safeNextPath(current)
  return next === HOME ? LOGIN_PATH : `${LOGIN_PATH}?${new URLSearchParams({ next })}`
}
