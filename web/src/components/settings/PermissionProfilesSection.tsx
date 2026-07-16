/**
 * PermissionProfilesSection — manage named tool-permission profiles (#600).
 */
import { useState } from 'react'
import { usePermissionProfiles, useCreatePermissionProfile, useUpdatePermissionProfile, useDeletePermissionProfile } from '@/api/hooks'
import type { PermissionProfile } from '@/api/types'

const MODES = ['interactive', 'auto', 'allowlist', 'full-auto']

export function PermissionProfilesSection() {
  const { data: profiles = [], isLoading } = usePermissionProfiles()
  const createProfile = useCreatePermissionProfile()
  const updateProfile = useUpdatePermissionProfile()
  const deleteProfile = useDeletePermissionProfile()

  const [editing, setEditing] = useState<PermissionProfile | null>(null)
  const [creating, setCreating] = useState(false)
  const [form, setForm] = useState({ name: '', description: '', mode: '', patterns: '' })

  const resetForm = () => setForm({ name: '', description: '', mode: '', patterns: '' })
  const parsePatterns = (s: string) => s.split('\n').map(p => p.trim()).filter(Boolean)

  const handleSave = async () => {
    const body = {
      name: form.name,
      description: form.description || undefined,
      mode: form.mode || undefined,
      patterns: parsePatterns(form.patterns),
    }
    if (editing) {
      await updateProfile.mutateAsync({ id: editing.id, ...body })
      setEditing(null)
    } else {
      await createProfile.mutateAsync(body)
      setCreating(false)
    }
    resetForm()
  }

  const startEdit = (p: PermissionProfile) => {
    setEditing(p)
    setCreating(false)
    setForm({
      name: p.name,
      description: p.description ?? '',
      mode: p.mode ?? '',
      patterns: (p.patterns ?? []).join('\n'),
    })
  }

  if (isLoading) return <div className="text-sm opacity-60">Loading profiles…</div>

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold text-sm">Permission Profiles</h3>
        <button
          className="text-xs px-2 py-1 rounded border border-[var(--border)] hover:bg-[var(--muted)]"
          onClick={() => { setCreating(true); setEditing(null); resetForm() }}
        >
          + New profile
        </button>
      </div>

      {(creating || editing) && (
        <div className="border border-[var(--border)] rounded-md p-3 space-y-2 text-sm">
          <div className="font-medium">{editing ? `Edit "${editing.name}"` : 'New profile'}</div>
          <label className="flex flex-col gap-0.5">
            <span className="text-xs opacity-70">Name *</span>
            <input
              className="border border-[var(--border)] rounded px-2 py-1 text-sm bg-transparent"
              placeholder="e.g. read-only research"
              value={form.name}
              onChange={e => setForm(f => ({ ...f, name: e.target.value }))}
            />
          </label>
          <label className="flex flex-col gap-0.5">
            <span className="text-xs opacity-70">Description</span>
            <input
              className="border border-[var(--border)] rounded px-2 py-1 text-sm bg-transparent"
              placeholder="Optional description"
              value={form.description}
              onChange={e => setForm(f => ({ ...f, description: e.target.value }))}
            />
          </label>
          <label className="flex flex-col gap-0.5">
            <span className="text-xs opacity-70">Permission mode</span>
            <select
              className="border border-[var(--border)] rounded px-2 py-1 text-sm bg-transparent"
              value={form.mode}
              onChange={e => setForm(f => ({ ...f, mode: e.target.value }))}
            >
              <option value="">— inherit from session —</option>
              {MODES.map(m => <option key={m} value={m}>{m}</option>)}
            </select>
          </label>
          <label className="flex flex-col gap-0.5">
            <span className="text-xs opacity-70">{'Tool patterns (one per line, e.g. Bash(git *))'}</span>
            <textarea
              className="border border-[var(--border)] rounded px-2 py-1 text-sm bg-transparent font-mono resize-none"
              rows={4}
              placeholder={'Read\nBash(git *)\nWebFetch'}
              value={form.patterns}
              onChange={e => setForm(f => ({ ...f, patterns: e.target.value }))}
            />
          </label>
          <div className="flex gap-2">
            <button
              className="text-xs px-3 py-1 rounded bg-[var(--accent)] text-[var(--accent-foreground)] disabled:opacity-50"
              disabled={!form.name.trim() || createProfile.isPending || updateProfile.isPending}
              onClick={handleSave}
            >{editing ? 'Save' : 'Create'}</button>
            <button
              className="text-xs px-3 py-1 rounded border border-[var(--border)] hover:bg-[var(--muted)]"
              onClick={() => { setCreating(false); setEditing(null); resetForm() }}
            >Cancel</button>
          </div>
        </div>
      )}

      {profiles.length === 0 && !creating ? (
        <p className="text-sm opacity-60">No profiles yet. Create named allowlists to apply at session spawn.</p>
      ) : (
        <div className="space-y-1">
          {profiles.map(p => (
            <div key={p.id} className="flex items-start justify-between gap-2 px-3 py-2 rounded border border-[var(--border)] text-sm">
              <div className="min-w-0">
                <div className="font-medium truncate">{p.name}</div>
                <div className="text-xs opacity-60">
                  {p.mode && <span className="mr-2">{p.mode}</span>}
                  {p.patterns.length > 0 && <span>{p.patterns.length} pattern{p.patterns.length !== 1 ? 's' : ''}</span>}
                </div>
              </div>
              <div className="flex gap-1 shrink-0">
                <button
                  className="text-xs px-2 py-0.5 rounded border border-[var(--border)] hover:bg-[var(--muted)]"
                  onClick={() => startEdit(p)}
                >Edit</button>
                <button
                  className="text-xs px-2 py-0.5 rounded border border-[var(--border)] text-red-500 hover:bg-red-50 dark:hover:bg-red-950"
                  onClick={() => deleteProfile.mutate(p.id)}
                >Delete</button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
