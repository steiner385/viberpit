// Placeholder — replaced in Task 7
import { useLibraryPlugins } from '@/api/hooks'

export function PluginsSection() {
  const { data, isLoading } = useLibraryPlugins()
  if (isLoading) return <div className="text-sm opacity-70 p-4">Loading…</div>
  return (
    <ul className="space-y-2">
      {(data?.plugins ?? []).map((p) => (
        <li key={p.id} className="border border-[var(--border)] rounded-lg p-3">
          <span className="font-medium">{p.name}</span>
          {p.description && <p className="text-sm opacity-70">{p.description}</p>}
        </li>
      ))}
    </ul>
  )
}
