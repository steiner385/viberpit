import { useState } from 'react'
import { cn } from '@/lib/utils'
import {
  useGithubRepos, usePatchAutomation,
} from '@/api/hooks'
import type { Automation, AutomationPatch, ConfigField } from '@/api/automations'

const LEVEL_DOT: Record<string, string> = {
  idle: 'bg-gray-400', active: 'bg-blue-500', warn: 'bg-amber-500', error: 'bg-red-500',
}

const MODE_DESCRIPTIONS: Record<string, string> = {
  off: 'Not monitoring.',
  propose: 'Suggests actions — you approve before anything runs.',
  auto: 'Acts automatically without confirmation.',
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

// --- Main card ---------------------------------------------------------------

export function AutomationCard({ automation: a }: {
  automation: Automation
}) {
  const [open, setOpen] = useState(false)
  const patch = usePatchAutomation()
  const hasConfig = a.configSchema.length > 0

  return (
    <div className="rounded-lg border border-border bg-card">
      <div className="flex items-center gap-3 p-4">
        <div className="text-left flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="font-medium">{a.name}</span>
            {a.activeSessions > 0 && (
              <span className="text-xs tabular-nums rounded-full bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300 px-2 py-0.5 leading-none">
                {a.activeSessions} active
              </span>
            )}
          </div>
          {a.description && (
            <p className="text-xs text-muted-foreground mt-0.5 leading-snug">{a.description}</p>
          )}
          <div className="flex items-center gap-2 text-sm text-muted-foreground mt-0.5">
            <span className={cn('inline-block w-2 h-2 rounded-full shrink-0', LEVEL_DOT[a.status.level])} />
            {a.status.text}
          </div>
        </div>
        {a.modes.length > 2 && (
          <div className="flex flex-col items-end gap-0.5 shrink-0">
            <select
              aria-label="Mode"
              value={a.mode ?? ''}
              onChange={(e) => patch.mutate({ id: a.id, body: { mode: e.target.value } })}
              className="text-sm rounded border border-border bg-background px-2 py-1"
            >
              {a.modes.map((m) => <option key={m} value={m}>{m}</option>)}
            </select>
            {a.mode && MODE_DESCRIPTIONS[a.mode] && (
              <span className="text-xs text-muted-foreground text-right max-w-36">
                {MODE_DESCRIPTIONS[a.mode]}
              </span>
            )}
          </div>
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
            // set one, else the row is enabled but stuck 'off'.
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
        {hasConfig && (
          <button
            type="button"
            aria-label={open ? 'Collapse' : 'Expand'}
            aria-expanded={open}
            onClick={() => setOpen((v) => !v)}
            className="text-muted-foreground hover:text-foreground shrink-0 p-1"
          >
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
              <path
                d={open ? 'M4 10l4-4 4 4' : 'M4 6l4 4 4-4'}
                stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"
              />
            </svg>
          </button>
        )}
      </div>
      {open && hasConfig && (
        <div className="border-t border-border p-4">
          <ConfigPanel automation={a} />
        </div>
      )}
    </div>
  )
}
