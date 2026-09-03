import { useMemo, useState } from 'react'
import { toast } from 'sonner'
import { useBacklogCandidates, useDispatchBacklogGroup } from '@/api/hooks'
import type { BacklogCandidate } from '@/api/automations'
import { Dialog } from '@/ui/Dialog'
import { Button } from '@/ui/Button'
import { Input } from '@/ui/Input'

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

  return (
    <Dialog open={open} onClose={onClose} title="Custom dispatch" size="lg" bodyClassName="flex flex-col gap-3 max-h-[80vh]">
      <Input
        type="search"
        placeholder="Filter issues…"
        value={search}
        onChange={(e) => setSearch(e.target.value)}
      />
      <div className="overflow-y-auto flex-1 min-h-0">
        {isLoading && <p className="text-sm text-ink-muted">Loading…</p>}
        {!isLoading && candidates.length === 0 && (
          <p className="text-sm text-ink-muted">No eligible issues.</p>
        )}
        {Array.from(byRepo.entries()).map(([repo, items]) => (
          <div key={repo} className="mb-3">
            <p className="text-xs font-medium text-ink-muted mb-1">{repo}</p>
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
                    <span className="text-ink-muted">#{c.number}</span>{' '}
                    {c.title}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
      <div className="flex justify-end gap-3">
        <Button type="button" variant="ghost" onClick={onClose}>
          Close
        </Button>
        <Button type="button" variant="primary" size="lg" onClick={run} disabled={selected.size === 0} loading={dispatch.isPending}>
          {dispatch.isPending
            ? 'Dispatching…'
            : `Dispatch ${selected.size} issue${selected.size !== 1 ? 's' : ''}`}
        </Button>
      </div>
    </Dialog>
  )
}
