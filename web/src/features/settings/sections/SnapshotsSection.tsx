/**
 * SnapshotsSection — config snapshot / export / import / diff, inline in the
 * Settings tab (issue #604).
 *
 * Snapshots capture the shared Claude config (skills, agents, settings, hooks,
 * commands, plugins) but NEVER credentials. Restore and import overwrite the
 * live shared config, so both auto-create a pre-restore / pre-import backup
 * first and require an explicit confirm().
 *
 * react-query is used inline here rather than through the shared api/hooks.ts:
 * these endpoints are settings-only and one caller. JSON endpoints go through
 * the `api` client; the binary export and the multipart import calls use
 * `fetch` directly (the `api` client only speaks JSON).
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { cn } from '@/lib/utils'
import { Button } from '@/ui/Button'
import { api } from '@/api/client'

type SnapshotKind = 'manual' | 'auto' | 'pre-restore' | 'pre-import'

interface Snapshot {
  id: string
  label: string | null
  kind: SnapshotKind
  createdAt: string
  fileCount: number
  sizeBytes: number
}

interface SnapshotDiff {
  added: string[]
  removed: string[]
  modified: string[]
}

interface ImportPreview {
  creates: string[]
  overwrites: string[]
  unchanged: string[]
}

interface RestoreResult {
  restored: number
  backupId: string
}

interface ImportApplyResult {
  created: number
  overwritten: number
  backupId: string
}

const KIND_LABELS: Record<SnapshotKind, string> = {
  manual: 'Manual',
  auto: 'Auto',
  'pre-restore': 'Pre-restore',
  'pre-import': 'Pre-import',
}

function humanSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let value = bytes / 1024
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit++
  }
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[unit]}`
}

function formatDate(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString()
}

function errMessage(err: unknown): string {
  if (err instanceof Error) return err.message
  return String(err)
}

async function multipartPost<T>(path: string, file: File): Promise<T> {
  const formData = new FormData()
  formData.append('file', file)
  const res = await fetch(`/api${path}`, { method: 'POST', body: formData })
  const data = (await res.json()) as Record<string, unknown>
  if (!res.ok) {
    const detail = typeof data.detail === 'string' ? data.detail : `Request failed (${res.status})`
    throw new Error(detail)
  }
  return data as T
}

export function SnapshotsSection() {
  const queryClient = useQueryClient()
  const { data: snapshots, isLoading } = useQuery<Snapshot[]>({
    queryKey: ['snapshots'],
    queryFn: () => api.get<Snapshot[]>('/snapshots'),
  })

  const [label, setLabel] = useState('')

  const createSnapshot = useMutation({
    mutationFn: (body: { label?: string }) => api.post<Snapshot>('/snapshots', body),
    onSuccess: () => {
      setLabel('')
      void queryClient.invalidateQueries({ queryKey: ['snapshots'] })
    },
  })

  const inputClass = cn(
    'rounded-md border border-gray-300 dark:border-gray-600',
    'bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100',
    'px-3 py-2 text-sm min-h-[44px]',
    'focus:outline-none focus:ring-2 focus:ring-blue-500',
    'placeholder:text-gray-400 dark:placeholder:text-gray-500',
  )

  return (
    <div className="space-y-6 max-w-2xl">
      {/* Header / actions */}
      <div className="space-y-3">
        <div className="flex flex-wrap items-end gap-2">
          <div className="flex-1 min-w-[12rem] space-y-1">
            <label
              htmlFor="snapshot-label"
              className="block text-sm font-medium text-gray-700 dark:text-gray-300"
            >
              Snapshot label <span className="text-gray-400">(optional)</span>
            </label>
            <input
              id="snapshot-label"
              type="text"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="Before big refactor"
              className={cn(inputClass, 'w-full')}
            />
          </div>
          <Button
            type="button"
            onClick={() => createSnapshot.mutate(label.trim() ? { label: label.trim() } : {})}
            disabled={createSnapshot.isPending}
            className="min-h-[44px]"
          >
            {createSnapshot.isPending ? 'Saving…' : 'Snapshot now'}
          </Button>
          <a
            href="/api/snapshots/export"
            download
            className={cn(
              'inline-flex items-center justify-center rounded-md text-sm font-medium min-h-[44px] px-4',
              'border border-edge bg-canvas hover:bg-accent-wash hover:text-ink',
            )}
          >
            Export bundle
          </a>
        </div>
        {createSnapshot.isError && (
          <p role="alert" className="text-sm text-red-600 dark:text-red-400">
            {errMessage(createSnapshot.error)}
          </p>
        )}
        <p className="text-xs text-gray-500 dark:text-gray-400">
          Snapshots and exports capture your shared Claude config only. Credentials
          are never included.
        </p>
      </div>

      {/* Snapshot list */}
      <div className="space-y-2">
        <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100">Snapshots</h3>
        {isLoading ? (
          <div className="text-sm opacity-70 p-2">Loading…</div>
        ) : !snapshots || snapshots.length === 0 ? (
          <div className="text-sm opacity-60 p-2">No snapshots yet.</div>
        ) : (
          <ul className="space-y-2">
            {snapshots.map((snap) => (
              <SnapshotRow key={snap.id} snapshot={snap} />
            ))}
          </ul>
        )}
      </div>

      {/* Import */}
      <ImportPanel />
    </div>
  )
}

function SnapshotRow({ snapshot }: { snapshot: Snapshot }) {
  const queryClient = useQueryClient()
  const [diff, setDiff] = useState<SnapshotDiff | null>(null)
  const [showFiles, setShowFiles] = useState(false)

  const loadDiff = useMutation({
    mutationFn: () => api.get<SnapshotDiff>(`/snapshots/${snapshot.id}/diff`),
    onSuccess: (result) => setDiff(result),
  })

  const restore = useMutation({
    mutationFn: () => api.post<RestoreResult>(`/snapshots/${snapshot.id}/restore`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['snapshots'] })
    },
  })

  function handleRestore() {
    const ok = window.confirm(
      'Restore this snapshot? This overwrites your live shared Claude config. ' +
        'A pre-restore backup is created automatically first.',
    )
    if (ok) restore.mutate()
  }

  return (
    <li className="rounded-md border border-gray-200 dark:border-gray-700 p-3 space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium text-gray-900 dark:text-gray-100">
          {snapshot.label ?? '(unlabeled)'}
        </span>
        <span className="text-xs rounded px-1.5 py-0.5 bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300">
          {KIND_LABELS[snapshot.kind]}
        </span>
        <span className="text-xs text-gray-500 dark:text-gray-400">{formatDate(snapshot.createdAt)}</span>
        <span className="text-xs text-gray-500 dark:text-gray-400">
          {snapshot.fileCount} file{snapshot.fileCount === 1 ? '' : 's'} · {humanSize(snapshot.sizeBytes)}
        </span>
        <div className="ml-auto flex gap-2">
          <Button
            type="button"
            variant="secondary"
            size="sm"
            onClick={() => loadDiff.mutate()}
            disabled={loadDiff.isPending}
          >
            {loadDiff.isPending ? 'Diffing…' : 'Diff'}
          </Button>
          <Button
            type="button"
            variant="danger"
            size="sm"
            onClick={handleRestore}
            disabled={restore.isPending}
          >
            {restore.isPending ? 'Restoring…' : 'Restore'}
          </Button>
        </div>
      </div>

      {restore.isError && (
        <p role="alert" className="text-sm text-red-600 dark:text-red-400">
          {errMessage(restore.error)}
        </p>
      )}
      {restore.isSuccess && restore.data && (
        <p className="text-sm text-green-700 dark:text-green-400">
          Restored {restore.data.restored} file{restore.data.restored === 1 ? '' : 's'} (backup {restore.data.backupId}).
        </p>
      )}

      {loadDiff.isError && (
        <p role="alert" className="text-sm text-red-600 dark:text-red-400">
          {errMessage(loadDiff.error)}
        </p>
      )}
      {diff && (
        <div className="text-xs space-y-2 border-t border-gray-100 dark:border-gray-700 pt-2">
          <div className="flex flex-wrap gap-3">
            <span className="text-green-700 dark:text-green-400">+{diff.added.length} added</span>
            <span className="text-red-600 dark:text-red-400">−{diff.removed.length} removed</span>
            <span className="text-amber-700 dark:text-amber-400">~{diff.modified.length} modified</span>
            <button
              type="button"
              onClick={() => setShowFiles((v) => !v)}
              className="text-blue-600 dark:text-blue-400 underline"
            >
              {showFiles ? 'Hide files' : 'Show files'}
            </button>
          </div>
          {showFiles && (
            <div className="space-y-1 font-mono">
              <FileList label="Added" files={diff.added} className="text-green-700 dark:text-green-400" />
              <FileList label="Removed" files={diff.removed} className="text-red-600 dark:text-red-400" />
              <FileList label="Modified" files={diff.modified} className="text-amber-700 dark:text-amber-400" />
            </div>
          )}
        </div>
      )}
    </li>
  )
}

function FileList({ label, files, className }: { label: string; files: string[]; className?: string }) {
  if (files.length === 0) return null
  return (
    <div>
      <div className={cn('font-semibold not-italic', className)}>{label}</div>
      <ul className="pl-3">
        {files.map((f) => (
          <li key={f} className="text-gray-600 dark:text-gray-300 break-all">
            {f}
          </li>
        ))}
      </ul>
    </div>
  )
}

function ImportPanel() {
  const queryClient = useQueryClient()
  const [file, setFile] = useState<File | null>(null)

  const preview = useMutation({
    mutationFn: (f: File) => multipartPost<ImportPreview>('/snapshots/import/preview', f),
  })

  const apply = useMutation({
    mutationFn: (f: File) => multipartPost<ImportApplyResult>('/snapshots/import/apply', f),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['snapshots'] })
    },
  })

  function handleFile(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0] ?? null
    setFile(f)
    apply.reset()
    preview.reset()
    if (f) preview.mutate(f)
  }

  function handleApply() {
    if (!file) return
    const ok = window.confirm(
      'Apply this import? This overwrites your live shared Claude config. ' +
        'A pre-import backup is created automatically first.',
    )
    if (ok) apply.mutate(file)
  }

  return (
    <div className="space-y-3 border-t border-gray-200 dark:border-gray-700 pt-4">
      <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100">Import bundle</h3>
      <div className="space-y-1">
        <label
          htmlFor="snapshot-import-file"
          className="block text-sm font-medium text-gray-700 dark:text-gray-300"
        >
          Select an exported bundle
        </label>
        <input
          id="snapshot-import-file"
          type="file"
          accept=".gz,.tar,.tgz"
          onChange={handleFile}
          className="block text-sm text-gray-700 dark:text-gray-300"
        />
      </div>

      {preview.isPending && <p className="text-sm opacity-70">Reading bundle…</p>}
      {preview.isError && (
        <p role="alert" className="text-sm text-red-600 dark:text-red-400">
          {errMessage(preview.error)}
        </p>
      )}

      {preview.data && (
        <div className="space-y-3">
          <div className="text-xs space-y-2 rounded-md border border-gray-200 dark:border-gray-700 p-3">
            <div className="flex flex-wrap gap-3">
              <span className="text-green-700 dark:text-green-400">
                +{preview.data.creates.length} new
              </span>
              <span className="text-amber-700 dark:text-amber-400">
                ~{preview.data.overwrites.length} overwritten
              </span>
              <span className="text-gray-500 dark:text-gray-400">
                {preview.data.unchanged.length} unchanged
              </span>
            </div>
            <div className="space-y-1 font-mono">
              <FileList label="New" files={preview.data.creates} className="text-green-700 dark:text-green-400" />
              <FileList
                label="Overwritten"
                files={preview.data.overwrites}
                className="text-amber-700 dark:text-amber-400"
              />
            </div>
          </div>
          <Button
            type="button"
            variant="danger"
            onClick={handleApply}
            disabled={apply.isPending}
            className="min-h-[44px]"
          >
            {apply.isPending ? 'Applying…' : 'Apply import'}
          </Button>
        </div>
      )}

      {apply.isError && (
        <p role="alert" className="text-sm text-red-600 dark:text-red-400">
          {errMessage(apply.error)}
        </p>
      )}
      {apply.isSuccess && apply.data && (
        <p className="text-sm text-green-700 dark:text-green-400">
          Imported: {apply.data.created} created, {apply.data.overwritten} overwritten (backup {apply.data.backupId}).
        </p>
      )}
    </div>
  )
}
