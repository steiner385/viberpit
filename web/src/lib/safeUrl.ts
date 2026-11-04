/**
 * Whether an untrusted url may become an `href`: only an absolute http(s) url does.
 * Anything else — `javascript:`, `data:`, a protocol-relative `//host`, leading
 * whitespace — renders as text, never as a link. The one gate for values the
 * server passes through unchecked (an artifact's url, a review comment's url).
 */
export function isSafeHttpUrl(url: string | null | undefined): url is string {
  return typeof url === 'string' && /^https?:\/\//i.test(url)
}
