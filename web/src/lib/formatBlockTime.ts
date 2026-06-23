/** Browser-local short (HH:MM) and full (HH:MM:SS + date) forms for a block's epoch-ms ts. */
export function formatBlockTime(ts?: number): { short: string; full: string } {
  if (typeof ts !== 'number' || !Number.isFinite(ts)) return { short: '', full: '' }
  const d = new Date(ts)
  const short = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  const full = d.toLocaleString([], { dateStyle: 'medium', timeStyle: 'medium' })
  return { short, full }
}
