/**
 * Format a usage-window reset timestamp for display.
 *
 * Single source of truth for "when does this limit reset" copy — shared by the
 * UsageBadge tooltips and the AllLimitedBanner so the two surfaces never drift.
 * Renders in the viewer's local timezone via Intl (no hard-coded locale).
 *
 * @param iso ISO-8601 timestamp string, or null when no window is active.
 * @returns A localized "Fri Jun 19, 2:30 PM"-style string, or null when `iso`
 *          is null/empty/unparseable.
 */
export function formatResetTime(iso: string | null | undefined): string | null {
  if (!iso) return null
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return null
  return new Intl.DateTimeFormat(undefined, {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  }).format(date)
}
