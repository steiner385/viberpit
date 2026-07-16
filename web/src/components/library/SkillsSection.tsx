import { useState } from 'react'
import { useLibrarySkills } from '@/api/hooks'
import { api } from '@/api/client'
import type { LibrarySkill } from '@/api/types'
import { ItemEditorDialog } from './ItemEditorDialog'

export function SkillsSection() {
  const { data, isLoading } = useLibrarySkills()
  const [dialog, setDialog] = useState<{ id: string | null; plugin?: string; readonly?: boolean; initialContent?: string } | null>(null)
  const [aiOpen, setAiOpen] = useState(false)
  const [aiDesc, setAiDesc] = useState('')
  const [aiLoading, setAiLoading] = useState(false)
  const [aiError, setAiError] = useState<string | null>(null)

  async function handleAiGenerate() {
    if (!aiDesc.trim()) return
    setAiLoading(true)
    setAiError(null)
    try {
      const result = await api.post<{ content: string }>('/library/skills/draft', { description: aiDesc })
      setAiOpen(false)
      setAiDesc('')
      setDialog({ id: null, initialContent: result.content })
    } catch (err) {
      setAiError(err instanceof Error ? err.message : 'Draft generation failed')
    } finally {
      setAiLoading(false)
    }
  }

  if (isLoading) return <div className="text-sm opacity-70 p-4">Loading…</div>

  const skills = data?.skills ?? []
  const userSkills = skills.filter((s) => s.source === 'user')
  // Group plugin skills by plugin id
  const pluginSkills = skills.filter((s) => s.source !== 'user')

  // Collect unique plugin ids in order
  const pluginIds = [...new Set(pluginSkills.map((s) => s.source))]

  function openSkill(s: LibrarySkill) {
    setDialog({ id: s.name, plugin: s.source !== 'user' ? s.source : undefined, readonly: s.readonly })
  }

  return (
    <div>
      {/* User skills */}
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300">Your skills</h3>
        <div className="flex gap-1">
          <button
            type="button"
            onClick={() => { setAiOpen(true); setAiError(null) }}
            className="text-xs px-2 py-1 rounded border border-[var(--border)] hover:bg-gray-50 dark:hover:bg-gray-700"
          >
            Create with AI
          </button>
          <button
            type="button"
            onClick={() => setDialog({ id: null })}
            className="text-xs px-2 py-1 rounded border border-[var(--border)] hover:bg-gray-50 dark:hover:bg-gray-700"
          >
            New skill
          </button>
        </div>
      </div>

      {aiOpen && (
        <div className="border border-[var(--border)] rounded-md p-3 space-y-2 text-sm mb-4">
          <div className="font-medium">Generate skill with AI</div>
          <label className="flex flex-col gap-0.5">
            <span className="text-xs opacity-70">Describe what the skill should do</span>
            <textarea
              className="border border-[var(--border)] rounded px-2 py-1 text-sm bg-transparent resize-none"
              rows={3}
              placeholder="e.g. A skill that reviews PRs for security vulnerabilities and suggests fixes"
              value={aiDesc}
              onChange={e => setAiDesc(e.target.value)}
              autoFocus
            />
          </label>
          {aiError && <p className="text-xs text-red-500">{aiError}</p>}
          <div className="flex gap-2">
            <button
              type="button"
              className="text-xs px-3 py-1 rounded bg-[var(--accent)] text-[var(--accent-foreground)] disabled:opacity-50"
              disabled={!aiDesc.trim() || aiLoading}
              onClick={handleAiGenerate}
            >
              {aiLoading ? 'Generating…' : 'Generate draft'}
            </button>
            <button
              type="button"
              className="text-xs px-3 py-1 rounded border border-[var(--border)] hover:bg-[var(--muted)]"
              onClick={() => { setAiOpen(false); setAiDesc(''); setAiError(null) }}
            >
              Cancel
            </button>
          </div>
        </div>
      )}
      <ul className="space-y-2 mb-6">
        {userSkills.map((s) => (
          <SkillRow key={s.name} skill={s} onClick={() => openSkill(s)} />
        ))}
        {userSkills.length === 0 && (
          <li className="text-sm opacity-60 p-3">No user skills yet.</li>
        )}
      </ul>

      {/* Plugin skills */}
      {pluginSkills.length > 0 && (
        <>
          <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-2">From plugins</h3>
          <ul className="space-y-2">
            {pluginIds.map((pluginId) =>
              pluginSkills
                .filter((s) => s.source === pluginId)
                .map((s) => (
                  <SkillRow key={`${pluginId}/${s.name}`} skill={s} pluginBadge={pluginId} onClick={() => openSkill(s)} />
                )),
            )}
          </ul>
        </>
      )}

      {/* Editor dialog */}
      {dialog !== null && (
        <ItemEditorDialog
          kind="skills"
          id={dialog.id}
          plugin={dialog.plugin}
          readonly={dialog.readonly}
          initialContent={dialog.initialContent}
          onClose={() => setDialog(null)}
        />
      )}
    </div>
  )
}

function SkillRow({
  skill,
  pluginBadge,
  onClick,
}: {
  skill: LibrarySkill
  pluginBadge?: string
  onClick: () => void
}) {
  return (
    <li className="border border-[var(--border)] rounded-lg p-3">
      <button
        type="button"
        className="w-full text-left"
        aria-label={skill.name}
        onClick={onClick}
      >
        <div className="flex items-baseline gap-2 min-w-0">
          <span className="font-medium">{skill.name}</span>
          {pluginBadge && (
            <span className="text-xs px-1.5 py-0.5 rounded-full border border-[var(--border)] opacity-70 shrink-0">
              {pluginBadge}
            </span>
          )}
          {skill.parseError && (
            <span className="text-xs px-1.5 py-0.5 rounded-full bg-red-100 dark:bg-red-900/30 text-red-600 dark:text-red-400 shrink-0">
              parse error
            </span>
          )}
        </div>
        {skill.description && (
          <p className="text-sm opacity-70 truncate">{skill.description}</p>
        )}
      </button>
    </li>
  )
}
