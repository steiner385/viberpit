import { useState } from 'react'
import { cn } from '@/lib/utils'
import {
  useGithubRepos, usePatchAutomation,
} from '@/api/hooks'
import type { Automation, AutomationPatch, ConfigField } from '@/api/automations'
import { ScheduleEditor } from './ScheduleEditor'

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
    return <p className="text-sm text-ink-muted">Loading repos…</p>
  }

  return (
    <div className="space-y-1 max-h-40 overflow-y-auto border border-edge rounded p-2">
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
        <p className="text-sm text-ink-muted">No repos available.</p>
      )}
    </div>
  )
}

// A per-repo number map (weights / concurrency caps). One numeric input per repo
// currently in `enabled_repos`: an empty input leaves the repo out of the map
// (default weight / uncapped); typing a number stores it, clearing removes it.
function RepoNumberMap({ field, value, onChange, enabledRepos }: FieldProps & {
  enabledRepos: string[]
}) {
  const map: Record<string, number> =
    value && typeof value === 'object' ? (value as Record<string, number>) : {}

  function setRepo(repo: string, raw: string) {
    const next = { ...map }
    if (raw === '') delete next[repo]
    else {
      const n = Number(raw)
      if (Number.isNaN(n)) return
      next[repo] = n
    }
    onChange(field.key, next)
  }

  if (enabledRepos.length === 0) {
    return <p className="text-sm text-ink-muted">Select repositories above first.</p>
  }

  return (
    <div className="space-y-1 border border-edge rounded p-2">
      {enabledRepos.map((repo) => (
        <label key={repo} className="flex items-center justify-between gap-2 text-sm">
          <span className="truncate">{repo}</span>
          <input
            type="number"
            min={0}
            aria-label={`${field.label}: ${repo}`}
            value={repo in map ? String(map[repo]) : ''}
            onChange={(e) => setRepo(repo, e.target.value)}
            className="text-sm rounded border border-edge bg-canvas px-2 py-1 w-24 shrink-0"
          />
        </label>
      ))}
    </div>
  )
}

function ConfigInput({ field, value, onChange, enabledRepos }: FieldProps & {
  enabledRepos?: string[]
}) {
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
          className="text-sm rounded border border-edge bg-canvas px-2 py-1 w-28"
        />
      )
    case 'string':
      return (
        <input
          type="text"
          value={value == null ? '' : String(value)}
          onChange={(e) => onChange(field.key, e.target.value)}
          className="text-sm rounded border border-edge bg-canvas px-2 py-1 w-full"
        />
      )
    case 'enum':
      return (
        <select
          value={value == null ? '' : String(value)}
          onChange={(e) => onChange(field.key, e.target.value)}
          className="text-sm rounded border border-edge bg-canvas px-2 py-1"
        >
          {(field.choices ?? []).map((c) => (
            <option key={c} value={c}>{c}</option>
          ))}
        </select>
      )
    case 'repo-multiselect':
      return <RepoMultiselect field={field} value={value} onChange={onChange} />
    case 'repo-number-map':
      return <RepoNumberMap field={field} value={value} onChange={onChange}
        enabledRepos={enabledRepos ?? []} />
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

  // The per-repo weight/cap widgets render one input per selected repo, so they
  // track the live `enabled_repos` value (which the user can edit in the same panel).
  const enabledRepos = Array.isArray(localConfig['enabled_repos'])
    ? (localConfig['enabled_repos'] as string[])
    : []

  return (
    <section>
      <h4 className="text-xs font-semibold uppercase text-ink-muted mb-2">Configuration</h4>
      <div className="space-y-3">
        {a.configSchema.map((field) => (
          <div key={field.key}>
            <label className="block text-sm font-medium mb-1">
              {field.type === 'bool' ? (
                <span className="flex items-center gap-2">
                  <ConfigInput field={field} value={localConfig[field.key]} onChange={handleChange} enabledRepos={enabledRepos} />
                  {field.label}
                </span>
              ) : (
                <>
                  <span className="block mb-1">{field.label}</span>
                  <ConfigInput field={field} value={localConfig[field.key]} onChange={handleChange} enabledRepos={enabledRepos} />
                </>
              )}
            </label>
            {field.help && (
              <p className="text-xs text-ink-muted mt-0.5">{field.help}</p>
            )}
          </div>
        ))}
      </div>
    </section>
  )
}

// --- Schedule panel ----------------------------------------------------------

function SchedulePanel({ automation: a }: ConfigPanelProps) {
  const patch = usePatchAutomation()
  return (
    <section>
      <h4 className="text-xs font-semibold uppercase text-ink-muted mb-2">Schedule</h4>
      <p className="text-xs text-ink-muted mb-2">
        Restrict when this automation runs (active-hours window) or preview a cron cadence.
        Outside its window the loop pauses and logs a "paused — outside scheduled active
        hours" entry in the activity feed.
      </p>
      <ScheduleEditor
        schedule={a.schedule ?? null}
        saving={patch.isPending}
        onSave={(schedule) => patch.mutate({ id: a.id, body: { schedule } })}
      />
    </section>
  )
}

/** Label for the engine this automation actually launches on, or null when
 *  there is nothing to say (Phase 7).
 *
 *  Deliberately silent for a `claude_only` origin: every origin is claude_only
 *  until it passes a Codex acceptance suite, so a badge on all of them would be
 *  noise. It reads the CONFIG rather than the policy, because `codex_allowed` is
 *  only permission — an origin can hold it and still be running Claude. */
function engineBadge(a: Automation): string | null {
  if (!a.enginePolicy || a.enginePolicy === 'claude_only') return null
  const selected = a.config?.engine ?? a.configSchema.find((f) => f.key === 'engine')?.default
  return selected === 'codex' ? 'Codex' : 'Claude'
}

// --- Main card ---------------------------------------------------------------

export function AutomationCard({ automation: a }: {
  automation: Automation
}) {
  const [open, setOpen] = useState(false)
  const patch = usePatchAutomation()
  const hasConfig = a.configSchema.length > 0
  // No control loop → no toggle and no schedule editor. Both would be controls
  // the server refuses (422) and, worse, an operator reading a grey switch as
  // "PM classification is off" when it is simply not a thing you turn on.
  const operatorTriggered = a.operatorTriggered ?? a.modes.length === 0

  return (
    <div className="rounded-lg border border-edge bg-surface">
      <div className="flex items-center gap-3 p-4">
        <div className="text-left flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="font-medium">{a.name}</span>
            {a.activeSessions > 0 && (
              <span className="text-xs tabular-nums rounded-full bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300 px-2 py-0.5 leading-none">
                {a.activeSessions} active
              </span>
            )}
            {engineBadge(a) && (
              <span
                className="text-xs rounded-full bg-violet-100 dark:bg-violet-900/40 text-violet-700 dark:text-violet-300 px-2 py-0.5 leading-none"
                title="Agent engine this automation's unattended sessions run on"
              >
                {engineBadge(a)}
              </span>
            )}
          </div>
          {a.description && (
            <p className="text-xs text-ink-muted mt-0.5 leading-snug">{a.description}</p>
          )}
          <div className="flex items-center gap-2 text-sm text-ink-muted mt-0.5">
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
              className="text-sm rounded border border-edge bg-canvas px-2 py-1"
            >
              {a.modes.map((m) => <option key={m} value={m}>{m}</option>)}
            </select>
            {a.mode && MODE_DESCRIPTIONS[a.mode] && (
              <span className="text-xs text-ink-muted text-right max-w-36">
                {MODE_DESCRIPTIONS[a.mode]}
              </span>
            )}
          </div>
        )}
        {!operatorTriggered && (
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
        )}
        {hasConfig && (
          <button
            type="button"
            aria-label={open ? 'Collapse' : 'Expand'}
            aria-expanded={open}
            onClick={() => setOpen((v) => !v)}
            className="text-ink-muted hover:text-ink shrink-0 p-1"
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
        <div className="border-t border-edge p-4 space-y-5">
          <ConfigPanel automation={a} />
          {!operatorTriggered && <SchedulePanel automation={a} />}
        </div>
      )}
    </div>
  )
}
