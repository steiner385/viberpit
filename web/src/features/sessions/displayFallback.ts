/** Why a session the operator expected on their desktop opened on a virtual display.
 *  Reasons are the server's `display_fallback_reason` (NativeDisplay.unavailable_reason()
 *  plus "native_disabled"); the one map from reason to human copy. */
const REASON_COPY: Record<string, string> = {
  no_graphical_session: 'no one is logged in to the desktop',
  cookie_missing: 'the desktop session changed or is not responding',
  connect_refused: 'the desktop session changed or is not responding',
  display_in_xvfb_pool: 'native display is not configured',
  invalid_display: 'native display is not configured',
  native_disabled: 'native display is not configured',
}

export function displayFallbackTitle(reason: string | null | undefined): string | null {
  if (!reason) return null
  const why = REASON_COPY[reason]
  return `Desktop unavailable — opened on a virtual display${why ? `: ${why}` : ''}`
}

/** What a session's "display" readout shows: `desktop` when its GUI renders on the
 *  host's desktop, `virtual` (with the fallback copy) when a desktop was expected
 *  but unavailable, nothing otherwise. One rule for every surface that shows it. */
export interface DisplayReadout {
  value: 'desktop' | 'virtual'
  /** Tooltip copy; for `virtual` it is the fallback reason, worth showing inline. */
  title: string
}

export function displayReadout(
  mode: 'native' | 'virtual' | null | undefined,
  reason: string | null | undefined,
): DisplayReadout | null {
  if (mode === 'native') return { value: 'desktop', title: "Windows open on the host's desktop" }
  const why = displayFallbackTitle(reason)
  return why ? { value: 'virtual', title: why } : null
}
