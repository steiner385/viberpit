import { useMemo, useState } from 'react'
import { toast } from 'sonner'
import { useBacklogCandidates, useDispatchBacklogGroup } from '@/api/hooks'
import type { BacklogCandidate } from '@/api/automations'

export function ManualDispatchDialog({
  open,
  onClose,
  onOpenSession,
}: {
  open: boolean
  onClose(): void
  onOpenSession?: (id: string) => void
}) {
  const { data, isLoading } = useBacklogCandidates(open)
  const dispatch = useDispatchBacklogGroup()
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [search, setSearch] = useState('')

  const candidates = useMemo(() => data?.candidates ?? [], [data?.candidates])

  const filtered = useMemo(() => {
    const q = search.toLowerCase()
    return q
      ? candidates.filter(
          (c) => c.title.toLowerCase().includes(q) || c.ref.toLowerCase().includes(q),
        )
      : candidates
  }, [candidates, search])

  const byRepo = useMemo(() => {
    const m = new Map<string, BacklogCandidate[]>()
    for (const c of filtered) {
      if (!m.has(c.repo)) m.set(c.repo, [])
      m.get(c.repo)!.push(c)
    }
    return m
  }, [filtered])

  function toggle(ref: string) {
    setSelected((s) => {
      const next = new Set(s)
      if (next.has(ref)) next.delete(ref)
      else next.add(ref)
      return next
    })
  }

  async function run() {
    const refs = Array.from(selected)
    try {
      const r = await dispatch.mutateAsync(refs)
      if (r.status === 'spawned') {
        toast.success(r.session_name ? `Session spawned: ${r.session_name}` : 'Session spawned')
        if (r.session_id) onOpenSession?.(r.session_id)
        setSelected(new Set())
        onClose()
      } else {
        toast.error(`Dispatch failed: ${r.status}`)
      }
    } catch {
      toast.error('Failed to dispatch')
    }
  }

  if (!open) return null

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      role="dialog"
      aria-modal="true"
      aria-label="Custom dispatch"
    >
      <div className="bg-[--bg] rounded-lg border border-[--border] p-4 w-full max-w-lg max-h-[80vh] flex flex-col gap-3 shadow-xl">
        <div className="flex items-center justify-between">
          <h3 className="font-semibold">Custom dispatch</h3>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="text-muted-foreground hover:text-foreground"
          >
            ✕
          </button>
        </div>
        <input
          type="search"
          placeholder="Filter issues…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="rounded border border-[--border] bg-[--bg] px-3 py-1 text-sm w-full"
        />
        <div className="overflow-y-auto flex-1 min-h-0">
          {isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
          {!isLoading && candidates.length === 0 && (
            <p className="text-sm text-muted-foreground">No eligible issues.</p>
          )}
          {Array.from(byRepo.entries()).map(([repo, items]) => (
            <div key={repo} className="mb-3">
              <p className="text-xs font-medium text-muted-foreground mb-1">{repo}</p>
              <ul className="space-y-1">
                {items.map((c) => (
                  <li key={c.ref} className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      aria-label={`select ${c.ref}`}
                      checked={selected.has(c.ref)}
                      onChange={() => toggle(c.ref)}
                    />
                    <span className="truncate">
                      <span className="text-muted-foreground">#{c.number}</span>{' '}
                      {c.title}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
        <div className="flex justify-end">
          <button
            type="button"
            onClick={run}
            disabled={selected.size === 0 || dispatch.isPending}
            className="rounded bg-blue-600 px-3 py-1.5 text-sm text-white hover:bg-blue-700 disabled:opacity-40"
          >
            {dispatch.isPending
              ? 'Dispatching…'
              : `Dispatch ${selected.size} issue${selected.size !== 1 ? 's' : ''}`}
          </button>
        </div>
      </div>
    </div>
  )
}
