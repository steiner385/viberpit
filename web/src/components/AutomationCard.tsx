import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { cn } from '@/lib/utils'
import { useAutomationEvents, usePatchAutomation } from '@/api/hooks'
import type { Automation } from '@/api/automations'

const LEVEL_DOT: Record<string, string> = {
  idle: 'bg-gray-400', active: 'bg-blue-500', warn: 'bg-amber-500', error: 'bg-red-500',
}

export function AutomationCard({ automation: a }: { automation: Automation }) {
  const [open, setOpen] = useState(false)
  const navigate = useNavigate()
  const patch = usePatchAutomation()
  const events = useAutomationEvents(a.id, open)

  return (
    <div className="rounded-lg border border-border bg-card">
      <div className="flex items-center gap-3 p-4">
        <button
          type="button"
          aria-label={open ? 'Collapse' : 'Expand'}
          onClick={() => setOpen((v) => !v)}
          className="text-left flex-1"
        >
          <div className="font-medium">{a.name}</div>
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <span className={cn('inline-block w-2 h-2 rounded-full', LEVEL_DOT[a.status.level])} />
            {a.status.text}
          </div>
        </button>
        {a.modes.length > 2 && (
          <select
            aria-label="Mode"
            value={a.mode ?? ''}
            onChange={(e) => patch.mutate({ id: a.id, body: { mode: e.target.value } })}
            className="text-sm rounded border border-border bg-background px-2 py-1"
          >
            {a.modes.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
        )}
        <button
          type="button"
          role="switch"
          aria-checked={a.enabled}
          aria-label={`${a.name} enabled`}
          onClick={() => patch.mutate({ id: a.id, body: { enabled: !a.enabled } })}
          className={cn('relative w-10 h-6 rounded-full transition-colors',
            a.enabled ? 'bg-blue-600' : 'bg-gray-300 dark:bg-gray-600')}
        >
          <span className={cn('absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white transition-transform',
            a.enabled && 'translate-x-4')} />
        </button>
      </div>
      {open && (
        <div className="border-t border-border p-4 space-y-4">
          <section>
            <h4 className="text-xs font-semibold uppercase text-muted-foreground mb-2">Activity</h4>
            {(events.data ?? []).length === 0
              ? <p className="text-sm text-muted-foreground">No activity yet.</p>
              : <ul className="space-y-1 text-sm">
                  {(events.data ?? []).map((e) => (
                    <li key={`${e.ts}-${e.sessionId ?? ''}`} className="flex gap-2">
                      <span className="text-muted-foreground tabular-nums">{e.ts.slice(11, 19)}</span>
                      {e.sessionId
                        ? <button
                            type="button"
                            onClick={() => navigate(`/session/${e.sessionId}`)}
                            className="text-blue-600 hover:underline text-left"
                          >{e.summary}</button>
                        : <span>{e.summary}</span>}
                    </li>
                  ))}
                </ul>}
          </section>
          {a.configSchema.length > 0 && (
            <section>
              <h4 className="text-xs font-semibold uppercase text-muted-foreground mb-2">Configuration</h4>
              <p className="text-sm text-muted-foreground">
                {a.configSchema.map((f) => f.label).join(', ')}
              </p>
            </section>
          )}
        </div>
      )}
    </div>
  )
}
