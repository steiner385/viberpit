/** Browser-local clock for an ISO event timestamp, for a feed the reader scans
 *  down: the time with seconds when the entry is from today, the date and time
 *  once it is not, plus the full date-time for a title. An unparseable string
 *  is shown as-is rather than as "Invalid Date". */
export function formatEventTime(iso: string, now: Date = new Date()): { short: string; full: string } {
  const d = new Date(iso)
  if (!Number.isFinite(d.getTime())) return { short: iso, full: iso }
  const today = d.toDateString() === now.toDateString()
  const short = today
    ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
    : d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
  return { short, full: d.toLocaleString([], { dateStyle: 'medium', timeStyle: 'medium' }) }
}

/** Browser-local short (HH:MM) and full (HH:MM:SS + date) forms for a block's epoch-ms ts. */
export function formatBlockTime(ts?: number): { short: string; full: string } {
  if (typeof ts !== 'number' || !Number.isFinite(ts)) return { short: '', full: '' }
  const d = new Date(ts)
  const short = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  const full = d.toLocaleString([], { dateStyle: 'medium', timeStyle: 'medium' })
  return { short, full }
}
