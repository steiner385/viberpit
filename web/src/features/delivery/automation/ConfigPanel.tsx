/**
 * The Config tab: an automation's `configSchema` rendered as controls that
 * patch on every change. Moved from `AutomationCard.tsx` (`ConfigPanel`,
 * `ConfigInput`, `RepoMultiselect`, `RepoNumberMap`) onto `Field`/`Input`/
 * `Select`/`Checkbox` — same live-patch-on-change behaviour.
 */
import { useState } from 'react'
import type { Automation, ConfigField } from '@/api/automations'
import { useGithubRepos, usePatchAutomation } from '@/api/hooks'
import { Checkbox } from '@/ui/Checkbox'
import { Field } from '@/ui/Field'
import { Input } from '@/ui/Input'
import { Select } from '@/ui/Select'

interface FieldValueProps {
  field: ConfigField
  value: unknown
  onChange: (key: string, val: unknown) => void
}

function RepoMultiselect({ field, value, onChange }: FieldValueProps) {
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
    <div className="space-y-1 max-h-40 overflow-y-auto border border-edge rounded-md p-2">
      {(repos.data ?? []).map((repo) => (
        <Checkbox
          key={repo.nameWithOwner}
          id={`${field.key}-${repo.nameWithOwner}`}
          label={repo.nameWithOwner}
          checked={selected.includes(repo.nameWithOwner)}
          onChange={() => toggle(repo.nameWithOwner)}
        />
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
function RepoNumberMap({ field, value, onChange, enabledRepos }: FieldValueProps & {
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
    <div className="space-y-1 border border-edge rounded-md p-2">
      {enabledRepos.map((repo) => (
        <label key={repo} className="flex items-center justify-between gap-2 text-sm">
          <span className="truncate">{repo}</span>
          <Input
            type="number"
            size="sm"
            min={0}
            aria-label={`${field.label}: ${repo}`}
            value={repo in map ? String(map[repo]) : ''}
            onChange={(e) => setRepo(repo, e.target.value)}
            className="w-24 shrink-0"
          />
        </label>
      ))}
    </div>
  )
}

function ConfigInput({ field, value, onChange, enabledRepos }: FieldValueProps & {
  enabledRepos?: string[]
}) {
  switch (field.type) {
    case 'bool':
      return (
        <Checkbox
          id={field.key}
          label={field.label}
          checked={Boolean(value)}
          onChange={(next) => onChange(field.key, next)}
        />
      )
    case 'int':
      return (
        <Input
          id={field.key}
          type="number"
          size="sm"
          value={value == null ? '' : String(value)}
          onChange={(e) => { const v = e.target.valueAsNumber; if (!Number.isNaN(v)) onChange(field.key, v) }}
          className="w-28"
        />
      )
    case 'string':
      return (
        <Input
          id={field.key}
          type="text"
          size="sm"
          value={value == null ? '' : String(value)}
          onChange={(e) => onChange(field.key, e.target.value)}
        />
      )
    case 'enum':
      return (
        <Select
          id={field.key}
          size="sm"
          value={value == null ? '' : String(value)}
          onChange={(e) => onChange(field.key, e.target.value)}
        >
          {(field.choices ?? []).map((c) => (
            <option key={c} value={c}>{c}</option>
          ))}
        </Select>
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

export function ConfigPanel({ automation: a }: { automation: Automation }) {
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
          field.type === 'bool'
            ? (
              <div key={field.key}>
                <ConfigInput field={field} value={localConfig[field.key]} onChange={handleChange} enabledRepos={enabledRepos} />
                {field.help && <p className="text-xs text-ink-muted mt-0.5">{field.help}</p>}
              </div>
            )
            : (
              <Field key={field.key} label={field.label} htmlFor={field.key} help={field.help || undefined}>
                <ConfigInput field={field} value={localConfig[field.key]} onChange={handleChange} enabledRepos={enabledRepos} />
              </Field>
            )
        ))}
      </div>
    </section>
  )
}
