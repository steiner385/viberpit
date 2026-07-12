// Placeholder — replaced in Task 9
import { useLibraryMcp } from '@/api/hooks'

export function McpSection() {
  const { data, isLoading } = useLibraryMcp()
  if (isLoading) return <div className="text-sm opacity-70 p-4">Loading…</div>
  return (
    <ul className="space-y-2">
      {(data?.servers ?? []).map((s) => (
        <li key={`${s.source}/${s.name}`} className="border border-[var(--border)] rounded-lg p-3">
          <span className="font-medium">{s.name}</span>
        </li>
      ))}
    </ul>
  )
}
