import { useState, useEffect } from 'react'
import { toast } from 'sonner'
import { cn } from '@/lib/utils'
import {
  useAutomationEvents, useAutomationSessions, useGithubRepos, usePatchAutomation,
  useAutomationActionable, useAutomationTrigger,
} from '@/api/hooks'
import type { Automation, AutomationPatch, ConfigField, ActionSpec } from '@/api/automations'
import { ConsolidatePanel } from './ConsolidatePanel'

const LEVEL_DOT: Record<string, string> = {
  idle: 'bg-gray-400', active: 'bg-blue-500', warn: 'bg-amber-500', error: 'bg-red-500',
}

// --- Config field renderers ---------------------------------------------------

interface FieldProps {
  field: ConfigField
  value: unknown
  onChange: (key: string, val: unknown) => void
}

function RepoMultiselect({ field, value, onChange }: FieldProps) {
  const repos = useGithubRepos()
  const selected: string[] = Array.isArray(value) ? (value as string[]) : []

  function toggle(nameWithOwner: string) {
    const next = selected.includes(nameWithOwner)
      ? selected.filter((r) => r !== nameWithOwner)
      : [...selected, nameWithOwner]
    onChange(field.key, next)
  }

  if (repos.isLoading) {
    return <p className="text-sm text-muted-foreground">Loading repos…</p>
  }

  return (
    <div className="space-y-1 max-h-40 overflow-y-auto border border-border rounded p-2">
      {(repos.data ?? []).map((repo) => (
        <label key={repo.nameWithOwner} className="flex items-center gap-2 text-sm cursor-pointer">
          <input
            type="checkbox"
            checked={selected.includes(repo.nameWithOwner)}
            onChange={() => toggle(repo.nameWithOwner)}
            className="rounded"
          />
          {repo.nameWithOwner}
        </label>
      ))}
      {(repos.data ?? []).length === 0 && (
        <p className="text-sm text-muted-foreground">No repos available.</p>
      )}
    </div>
  )
}

function ConfigInput({ field, value, onChange }: FieldProps) {
  switch (field.type) {
    case 'bool':
      return (
        <input
          type="checkbox"
          checked={Boolean(value)}
          onChange={(e) => onChange(field.key, e.target.checked)}
          className="rounded"
        />
      )
    case 'int':
      return (
        <input
          type="number"
          value={value == null ? '' : String(value)}
          onChange={(e) => { const v = e.target.valueAsNumber; if (!Number.isNaN(v)) onChange(field.key, v) }}
          className="text-sm rounded border border-border bg-background px-2 py-1 w-28"
        />
      )
    case 'string':
      return (
        <input
          type="text"
          value={value == null ? '' : String(value)}
          onChange={(e) => onChange(field.key, e.target.value)}
          className="text-sm rounded border border-border bg-background px-2 py-1 w-full"
        />
      )
    case 'enum':
      return (
        <select
          value={value == null ? '' : String(value)}
          onChange={(e) => onChange(field.key, e.target.value)}
          className="text-sm rounded border border-border bg-background px-2 py-1"
        >
          {(field.choices ?? []).map((c) => (
            <option key={c} value={c}>{c}</option>
          ))}
        </select>
      )
    case 'repo-multiselect':
      return <RepoMultiselect field={field} value={value} onChange={onChange} />
    default:
      return null
  }
}

// --- Config panel ------------------------------------------------------------

interface ConfigPanelProps {
  automation: Automation
}

function ConfigPanel({ automation: a }: ConfigPanelProps) {
  const patch = usePatchAutomation()
  // Local config state seeded from server values (fall back to field defaults)
  const [localConfig, setLocalConfig] = useState<Record<string, unknown>>(() => {
    const init: Record<string, unknown> = {}
    for (const f of a.configSchema) {
      init[f.key] = a.config[f.key] ?? f.default
    }
    return init
  })

  function handleChange(key: string, val: unknown) {
    const next = { ...localConfig, [key]: val }
    setLocalConfig(next)
    patch.mutate({ id: a.id, body: { config: next } })
  }

  return (
    <section>
      <h4 className="text-xs font-semibold uppercase text-muted-foreground mb-2">Configuration</h4>
      <div className="space-y-3">
        {a.configSchema.map((field) => (
          <div key={field.key}>
            <label className="block text-sm font-medium mb-1">
              {field.type === 'bool' ? (
                <span className="flex items-center gap-2">
                  <ConfigInput field={field} value={localConfig[field.key]} onChange={handleChange} />
                  {field.label}
                </span>
              ) : (
                <>
                  <span className="block mb-1">{field.label}</span>
                  <ConfigInput field={field} value={localConfig[field.key]} onChange={handleChange} />
                </>
              )}
            </label>
            {field.help && (
              <p className="text-xs text-muted-foreground mt-0.5">{field.help}</p>
            )}
          </div>
        ))}
      </div>
    </section>
  )
}

// --- Spawned sessions section ------------------------------------------------

// Session statuses that mean the spawned session is no longer live. Mirrors the
// backend's INACTIVE_SESSION_STATUSES (server/db/models.py), which drives the
// automation's activeSessions count — kept in sync so the list agrees with the badge.
const INACTIVE_SESSION_STATUSES = new Set(['ended', 'archived'])

interface SpawnedSessionsSectionProps {
  id: string
  enabled: boolean
  onOpenSession?: (id: string) => void
  onReviewSession?: (id: string) => void
}

function SpawnedSessionsSection({ id, enabled, onOpenSession, onReviewSession }: SpawnedSessionsSectionProps) {
  const sessions = useAutomationSessions(id, enabled)
  const list = sessions.data ?? []
  const activeCount = list.filter((s) => !INACTIVE_SESSION_STATUSES.has(s.status)).length

  return (
    <section>
      <h4 className="text-xs font-semibold uppercase text-muted-foreground mb-2">
        Spawned Sessions{list.length > 0 && ` (${activeCount} active)`}
      </h4>
      {list.length === 0
        ? <p className="text-sm text-muted-foreground">No sessions spawned.</p>
        : (
          <ul className="space-y-1 text-sm">
            {list.map((s) => (
              <li key={s.id} className="flex items-center gap-2">
                <button
                  type="button"
                  // Inactive (ended/archived) sessions have no live driver, so the
                  // live-attach route 404s — route them to the read-only review view.
                  onClick={() => (INACTIVE_SESSION_STATUSES.has(s.status) ? onReviewSession : onOpenSession)?.(s.id)}
                  className="text-blue-600 hover:underline text-left"
                >
                  {s.name}
                </button>
                <span className="text-muted-foreground">· {s.status}</span>
              </li>
            ))}
          </ul>
        )}
    </section>
  )
}

// --- AutomationActions section -----------------------------------------------

function outcomeToast(r: { level: string; message: string }) {
  if (r.level === 'ok') toast.success(r.message)
  else if (r.level === 'warn') toast.warning(r.message)
  else toast.error(r.message)
}

function ActionButton(
  { item, action, automationId }:
  { item: { id: string }; action: ActionSpec; automationId: string },
) {
  const trigger = useAutomationTrigger(automationId)
  const [armed, setArmed] = useState(false)     // risky: first click arms confirm
  const [outcome, setOutcome] = useState<{ level: string; message: string } | null>(null)
  const busy = trigger.isPending

  useEffect(() => {
    if (!outcome) return
    const id = setTimeout(() => setOutcome(null), 4000)
    return () => clearTimeout(id)
  }, [outcome])

  async function fire() {
    setArmed(false)
    const r = await trigger.mutateAsync({ item_id: item.id, kind: action.kind })
    setOutcome({ level: r.level, message: r.message })
    outcomeToast(r)
  }

  function onClick() {
    if (action.style === 'risky' && !armed) { setArmed(true); return }
    void fire()
  }

  const base = 'rounded border px-2 py-1 text-xs font-semibold disabled:opacity-50'
  const btnTone = action.style === 'risky'
    ? 'border-red-300 text-red-700 hover:bg-red-600 hover:text-white hover:border-red-600'
    : 'border-border hover:bg-primary hover:text-primary-foreground hover:border-primary'
  return (
    <span className="inline-flex items-center gap-2">
      <button type="button" disabled={busy} onClick={onClick} className={`${base} ${btnTone}`}>
        {busy ? 'Working…' : armed ? `Confirm ${action.label}?` : action.label}
      </button>
      {outcome && (() => {
        const badgeTone = outcome.level === 'ok' ? 'text-green-700 bg-green-50'
          : outcome.level === 'warn' ? 'text-amber-700 bg-amber-50'
          : 'text-red-700 bg-red-50'
        return <span className={`rounded px-2 py-1 text-xs font-semibold ${badgeTone}`}>{outcome.message}</span>
      })()}
    </span>
  )
}

function AutomationActions({ automationId, enabled }: { automationId: string; enabled: boolean }) {
  const actionable = useAutomationActionable(automationId, enabled)
  const list = actionable.data ?? []
  return (
    <section>
      <h4 className="text-xs font-semibold uppercase text-muted-foreground mb-2">Actionable items</h4>
      {list.length === 0
        ? <p className="text-sm text-muted-foreground">No actionable items.</p>
        : (
          <ul className="space-y-1 text-sm">
            {list.map((item) => (
              <li key={item.id} className="flex items-center gap-2">
                <span className="flex-1 truncate">
                  <span className="text-muted-foreground tabular-nums">{item.id.split('#')[1] ? `#${item.id.split('#')[1]}` : ''}</span>{' '}
                  {item.title}
                </span>
                {item.actions.map((action) => (
                  <ActionButton key={action.kind} item={item} action={action} automationId={automationId} />
                ))}
              </li>
            ))}
          </ul>
        )}
    </section>
  )
}

// --- Main card ---------------------------------------------------------------

export function AutomationCard({ automation: a, onOpenSession, onReviewSession }: {
  automation: Automation
  onOpenSession?: (id: string) => void
  onReviewSession?: (id: string) => void
}) {
  const [open, setOpen] = useState(false)
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
          <div className="flex items-center gap-2">
            <span className="font-medium">{a.name}</span>
            {a.activeSessions > 0 && (
              <span className="text-xs tabular-nums rounded-full bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300 px-2 py-0.5 leading-none">
                {a.activeSessions} active
              </span>
            )}
          </div>
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
          onClick={() => {
            const enabling = !a.enabled
            const body: AutomationPatch = { enabled: enabling }
            // A 2-mode automation (off/propose) hides the mode <select>, so the
            // toggle is its only control: enabling with no active mode must also
            // set one, else the row is enabled but stuck 'off'. Never stomp an
            // already-chosen active mode (e.g. a 3-mode 'auto').
            const active = a.modes.filter((m) => m !== 'off')
            if (enabling && active.length > 0 && !active.includes(a.mode ?? '')) {
              body.mode = active[0]
            }
            patch.mutate({ id: a.id, body })
          }}
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
                            onClick={() => onOpenSession?.(e.sessionId!)}
                            className="text-blue-600 hover:underline text-left"
                          >{e.summary}</button>
                        : <span>{e.summary}</span>}
                    </li>
                  ))}
                </ul>}
          </section>
          <AutomationActions automationId={a.id} enabled={open} />
          {a.id === 'pr-doctor' && <ConsolidatePanel enabled={open} />}
          <SpawnedSessionsSection id={a.id} enabled={open} onOpenSession={onOpenSession} onReviewSession={onReviewSession} />
          {a.configSchema.length > 0 && <ConfigPanel automation={a} />}
        </div>
      )}
    </div>
  )
}
