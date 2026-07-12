// Placeholder — replaced in Task 8
import { useLibraryCommands } from '@/api/hooks'

export function CommandsSection() {
  const { data, isLoading } = useLibraryCommands()
  if (isLoading) return <div className="text-sm opacity-70 p-4">Loading…</div>
  return (
    <ul className="space-y-2">
      {(data?.commands ?? []).map((c) => (
        <li key={`${c.source}/${c.path}`} className="border border-[var(--border)] rounded-lg p-3">
          <span className="font-medium">/{c.path.replace(/\//g, ':')}</span>
          {c.description && <p className="text-sm opacity-70">{c.description}</p>}
        </li>
      ))}
    </ul>
  )
}
