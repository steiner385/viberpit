/**
 * PresetsSection — manage named session launch presets (#597).
 * Lists all presets with edit/delete, and a form to create new ones.
 */
import { useState } from 'react'
import { usePresets, useCreatePreset, useUpdatePreset, useDeletePreset } from '@/api/hooks'
import type { SessionPreset } from '@/api/types'

export function PresetsSection() {
  const { data: presets = [], isLoading } = usePresets()
  const createPreset = useCreatePreset()
  const updatePreset = useUpdatePreset()
  const deletePreset = useDeletePreset()

  const [editing, setEditing] = useState<SessionPreset | null>(null)
  const [creating, setCreating] = useState(false)
  const [form, setForm] = useState({ name: '', repo: '', account: '', model: '', permissionMode: '', initialPrompt: '' })

  const resetForm = () => setForm({ name: '', repo: '', account: '', model: '', permissionMode: '', initialPrompt: '' })

  const handleSave = async () => {
    const body = {
      name: form.name,
      repo: form.repo || null,
      account: form.account || null,
      model: form.model || null,
      permissionMode: form.permissionMode || null,
      initialPrompt: form.initialPrompt || null,
    }
    if (editing) {
      await updatePreset.mutateAsync({ id: editing.id, ...body })
      setEditing(null)
    } else {
      await createPreset.mutateAsync(body)
      setCreating(false)
    }
    resetForm()
  }

  const startEdit = (p: SessionPreset) => {
    setEditing(p)
    setCreating(false)
    setForm({
      name: p.name,
      repo: p.repo ?? '',
      account: p.account ?? '',
      model: p.model ?? '',
      permissionMode: p.permissionMode ?? '',
      initialPrompt: p.initialPrompt ?? '',
    })
  }

  if (isLoading) return <div className="text-sm opacity-60">Loading presets…</div>

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold text-sm">Session Presets</h3>
        <button
          className="text-xs px-2 py-1 rounded border border-edge hover:bg-surface-raised"
          onClick={() => { setCreating(true); setEditing(null); resetForm() }}
        >
          + New preset
        </button>
      </div>

      {(creating || editing) && (
        <div className="border border-edge rounded-md p-3 space-y-2 text-sm">
          <div className="font-medium">{editing ? `Edit "${editing.name}"` : 'New preset'}</div>
          {[
            { key: 'name', label: 'Name *', placeholder: 'e.g. KinDash bugfix' },
            { key: 'repo', label: 'Repo', placeholder: 'owner/name' },
            { key: 'account', label: 'Account', placeholder: 'leave blank for auto-select' },
            { key: 'model', label: 'Model', placeholder: 'e.g. opus' },
            { key: 'permissionMode', label: 'Permission mode', placeholder: 'interactive / auto / full-auto' },
          ].map(({ key, label, placeholder }) => (
            <label key={key} className="flex flex-col gap-0.5">
              <span className="text-xs opacity-70">{label}</span>
              <input
                className="border border-edge rounded px-2 py-1 text-sm bg-transparent"
                placeholder={placeholder}
                value={(form as Record<string, string>)[key]}
                onChange={e => setForm(f => ({ ...f, [key]: e.target.value }))}
              />
            </label>
          ))}
          <label className="flex flex-col gap-0.5">
            <span className="text-xs opacity-70">Initial prompt</span>
            <textarea
              className="border border-edge rounded px-2 py-1 text-sm bg-transparent resize-none"
              rows={3}
              placeholder="Optional starting prompt…"
              value={form.initialPrompt}
              onChange={e => setForm(f => ({ ...f, initialPrompt: e.target.value }))}
            />
          </label>
          <div className="flex gap-2">
            <button
              className="text-xs px-3 py-1 rounded bg-accent text-accent-ink disabled:opacity-50"
              disabled={!form.name.trim() || createPreset.isPending || updatePreset.isPending}
              onClick={handleSave}
            >
              {editing ? 'Save' : 'Create'}
            </button>
            <button
              className="text-xs px-3 py-1 rounded border border-edge hover:bg-surface-raised"
              onClick={() => { setCreating(false); setEditing(null); resetForm() }}
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {presets.length === 0 && !creating ? (
        <p className="text-sm opacity-60">No presets yet. Create one to speed up session launch.</p>
      ) : (
        <div className="space-y-1">
          {presets.map(p => (
            <div key={p.id} className="flex items-center justify-between gap-2 px-3 py-2 rounded border border-edge text-sm">
              <div className="min-w-0">
                <div className="font-medium truncate">{p.name}</div>
                <div className="text-xs opacity-60 truncate">
                  {[p.repo, p.account, p.model, p.permissionMode].filter(Boolean).join(' · ') || 'No fields set'}
                </div>
              </div>
              <div className="flex gap-1 shrink-0">
                <button
                  className="text-xs px-2 py-0.5 rounded border border-edge hover:bg-surface-raised"
                  onClick={() => startEdit(p)}
                >Edit</button>
                <button
                  className="text-xs px-2 py-0.5 rounded border border-edge text-red-500 hover:bg-red-50 dark:hover:bg-red-950"
                  onClick={() => deletePreset.mutate(p.id)}
                >Delete</button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
