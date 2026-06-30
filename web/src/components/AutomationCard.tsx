import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { cn } from '@/lib/utils'
import {
  useAutomationEvents, useAutomationSessions, useGithubRepos, usePatchAutomation,
} from '@/api/hooks'
import type { Automation, ConfigField } from '@/api/automations'

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

interface SpawnedSessionsSectionProps {
  id: string
  enabled: boolean
}

function SpawnedSessionsSection({ id, enabled }: SpawnedSessionsSectionProps) {
  const sessions = useAutomationSessions(id, enabled)
  const list = sessions.data ?? []

  return (
    <section>
      <h4 className="text-xs font-semibold uppercase text-muted-foreground mb-2">Spawned Sessions</h4>
      {list.length === 0
        ? <p className="text-sm text-muted-foreground">No sessions spawned.</p>
        : (
          <ul className="space-y-1 text-sm">
            {list.map((s) => (
              <li key={s.id} className="flex items-center gap-2">
                <Link
                  to={`/session/${s.id}`}
                  className="text-blue-600 hover:underline"
                >
                  {s.name}
                </Link>
                <span className="text-muted-foreground">· {s.status}</span>
              </li>
            ))}
          </ul>
        )}
    </section>
  )
}

// --- Main card ---------------------------------------------------------------

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
          <SpawnedSessionsSection id={a.id} enabled={open} />
          {a.configSchema.length > 0 && <ConfigPanel automation={a} />}
        </div>
      )}
    </div>
  )
}
