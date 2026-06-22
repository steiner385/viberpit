/** Format elapsed milliseconds as a compact "12s" / "1m 2s" readout. */
export function formatElapsed(ms: number): string {
  const totalSec = Math.floor(ms / 1000)
  if (totalSec < 60) return `${totalSec}s`
  const min = Math.floor(totalSec / 60)
  return `${min}m ${totalSec % 60}s`
}
