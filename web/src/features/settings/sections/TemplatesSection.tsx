/**
 * TemplatesSection — manage named prompt templates with variables (#598).
 */
import { useState } from 'react'
import { useTemplates, useCreateTemplate, useUpdateTemplate, useDeleteTemplate } from '@/api/hooks'
import type { PromptTemplate } from '@/api/types'

export function TemplatesSection() {
  const { data: templates = [], isLoading } = useTemplates()
  const createTemplate = useCreateTemplate()
  const updateTemplate = useUpdateTemplate()
  const deleteTemplate = useDeleteTemplate()

  const [editing, setEditing] = useState<PromptTemplate | null>(null)
  const [creating, setCreating] = useState(false)
  const [form, setForm] = useState({ name: '', content: '', tags: '' })

  const resetForm = () => setForm({ name: '', content: '', tags: '' })

  const parseTags = (s: string) => s.split(',').map(t => t.trim()).filter(Boolean)

  const handleSave = async () => {
    const body = {
      name: form.name,
      content: form.content,
      tags: parseTags(form.tags),
    }
    if (editing) {
      await updateTemplate.mutateAsync({ id: editing.id, ...body })
      setEditing(null)
    } else {
      await createTemplate.mutateAsync(body)
      setCreating(false)
    }
    resetForm()
  }

  const startEdit = (t: PromptTemplate) => {
    setEditing(t)
    setCreating(false)
    setForm({ name: t.name, content: t.content, tags: (t.tags ?? []).join(', ') })
  }

  if (isLoading) return <div className="text-sm opacity-60">Loading templates…</div>

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold text-sm">Prompt Templates</h3>
        <button
          className="text-xs px-2 py-1 rounded border border-edge hover:bg-surface-raised"
          onClick={() => { setCreating(true); setEditing(null); resetForm() }}
        >
          + New template
        </button>
      </div>

      {(creating || editing) && (
        <div className="border border-edge rounded-md p-3 space-y-2 text-sm">
          <div className="font-medium">{editing ? `Edit "${editing.name}"` : 'New template'}</div>
          <label className="flex flex-col gap-0.5">
            <span className="text-xs opacity-70">Name *</span>
            <input
              className="border border-edge rounded px-2 py-1 text-sm bg-transparent"
              placeholder="e.g. PR review checklist"
              value={form.name}
              onChange={e => setForm(f => ({ ...f, name: e.target.value }))}
            />
          </label>
          <label className="flex flex-col gap-0.5">
            <span className="text-xs opacity-70">{'Content (use {{variable}} for placeholders)'}</span>
            <textarea
              className="border border-edge rounded px-2 py-1 text-sm bg-transparent resize-none font-mono"
              rows={6}
              placeholder={'Review {{repo}} PR #{{pr_number}} using our checklist…'}
              value={form.content}
              onChange={e => setForm(f => ({ ...f, content: e.target.value }))}
            />
          </label>
          <label className="flex flex-col gap-0.5">
            <span className="text-xs opacity-70">Tags (comma-separated)</span>
            <input
              className="border border-edge rounded px-2 py-1 text-sm bg-transparent"
              placeholder="review, pr, checklist"
              value={form.tags}
              onChange={e => setForm(f => ({ ...f, tags: e.target.value }))}
            />
          </label>
          <div className="flex gap-2">
            <button
              className="text-xs px-3 py-1 rounded bg-accent text-accent-ink disabled:opacity-50"
              disabled={!form.name.trim() || createTemplate.isPending || updateTemplate.isPending}
              onClick={handleSave}
            >
              {editing ? 'Save' : 'Create'}
            </button>
            <button
              className="text-xs px-3 py-1 rounded border border-edge hover:bg-surface-raised"
              onClick={() => { setCreating(false); setEditing(null); resetForm() }}
            >Cancel</button>
          </div>
        </div>
      )}

      {templates.length === 0 && !creating ? (
        <p className="text-sm opacity-60">{'No templates yet. Create reusable prompts with {{variable}} placeholders.'}</p>
      ) : (
        <div className="space-y-1">
          {templates.map(t => (
            <div key={t.id} className="flex items-start justify-between gap-2 px-3 py-2 rounded border border-edge text-sm">
              <div className="min-w-0">
                <div className="font-medium truncate">{t.name}</div>
                <div className="text-xs opacity-60 line-clamp-2">{t.content.slice(0, 80)}{t.content.length > 80 ? '…' : ''}</div>
                {t.tags.length > 0 && (
                  <div className="flex flex-wrap gap-1 mt-1">
                    {t.tags.map(tag => (
                      <span key={tag} className="text-xs px-1.5 py-0.5 rounded-full bg-surface-raised opacity-80">{tag}</span>
                    ))}
                  </div>
                )}
              </div>
              <div className="flex gap-1 shrink-0">
                <button
                  className="text-xs px-2 py-0.5 rounded border border-edge hover:bg-surface-raised"
                  onClick={() => startEdit(t)}
                >Edit</button>
                <button
                  className="text-xs px-2 py-0.5 rounded border border-edge text-red-500 hover:bg-red-50 dark:hover:bg-red-950"
                  onClick={() => deleteTemplate.mutate(t.id)}
                >Delete</button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
