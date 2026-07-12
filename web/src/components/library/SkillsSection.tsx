// Placeholder — replaced in Task 8
import { useLibrarySkills } from '@/api/hooks'

export function SkillsSection() {
  const { data, isLoading } = useLibrarySkills()
  if (isLoading) return <div className="text-sm opacity-70 p-4">Loading…</div>
  return (
    <ul className="space-y-2">
      {(data?.skills ?? []).map((s) => (
        <li key={`${s.source}/${s.name}`} className="border border-[var(--border)] rounded-lg p-3">
          <span className="font-medium">{s.name}</span>
          {s.description && <p className="text-sm opacity-70">{s.description}</p>}
        </li>
      ))}
    </ul>
  )
}
