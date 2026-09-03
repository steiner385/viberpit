import { useProjectDeps } from '@/api/pm'

export function DependencyPanel({ slug }: { slug: string }) {
  const { data, isLoading } = useProjectDeps(slug)
  if (isLoading || !data) return null
  const active = data.edges.filter((e) => e.active)
  const bySrc = new Map<string, typeof active>()
  for (const edge of active) {
    bySrc.set(edge.src, [...(bySrc.get(edge.src) ?? []), edge])
  }
  const { drift } = data
  const badges = [
    drift.proseOnly.length && `${drift.proseOnly.length} prose-only`,
    drift.unresolvedTargets.length && `${drift.unresolvedTargets.length} unresolved`,
    drift.missingFromProject.length && `${drift.missingFromProject.length} off board`,
    drift.checklistEpics.length && `${drift.checklistEpics.length} checklist epics`,
  ].filter(Boolean) as string[]
  if (active.length === 0 && badges.length === 0) {
    return <p className="text-sm pm-muted">No mirrored dependencies.</p>
  }
  return (
    <div className="space-y-2">
      {badges.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {badges.map((b) => (
            <span key={b} className="rounded bg-amber-500/15 px-2 py-0.5 text-xs text-amber-600">
              {b}
            </span>
          ))}
        </div>
      )}
      {[...bySrc.entries()].map(([src, edges]) => (
        <div key={src} className="text-sm">
          <span className="font-medium">{src}</span>
          <span className="pm-muted"> blocked by </span>
          {edges.map((e, i) => (
            <span key={`${e.dst}:${e.source}`}>
              {i > 0 && ', '}
              <span className={e.dstState === 'closed' ? 'line-through opacity-60' : ''}>
                {e.dst}
              </span>
              <span className="text-xs pm-muted"> ({e.source})</span>
            </span>
          ))}
        </div>
      ))}
    </div>
  )
}
