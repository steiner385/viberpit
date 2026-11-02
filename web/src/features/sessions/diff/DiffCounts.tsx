/** The `+N −M` pair — one rendering for the header chip, the Changes tab's file
 *  rows and the rail's Changes card. */
export function DiffCounts({ additions, deletions, className }: { additions: number; deletions: number; className?: string }) {
  return (
    <span className={`font-mono tabular-nums ${className ?? ''}`}>
      <span className="text-success">+{additions}</span>{' '}
      <span className="text-danger">−{deletions}</span>
    </span>
  )
}
