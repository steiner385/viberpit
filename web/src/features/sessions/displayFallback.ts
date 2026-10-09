/** Why a session the operator expected on their desktop opened on a virtual display.
 *  Reasons are the server's `display_fallback_reason` (NativeDisplay.unavailable_reason()
 *  plus "native_disabled"); the one map from reason to human copy. */
const REASON_COPY: Record<string, string> = {
  no_graphical_session: 'no one is logged in to the desktop',
  cookie_missing: 'the desktop refused the connection (locked or session changed)',
  connect_refused: 'the desktop refused the connection (locked or session changed)',
  display_in_xvfb_pool: 'native display is not configured',
  invalid_display: 'native display is not configured',
  native_disabled: 'native display is not configured',
}

export function displayFallbackTitle(reason: string | null | undefined): string | null {
  if (!reason) return null
  const why = REASON_COPY[reason]
  return `Desktop unavailable — opened on a virtual display${why ? `: ${why}` : ''}`
}
