/**
 * `ConfigInput` — one automation config field rendered as the control that
 * matches its `ConfigField.type`, plus its repo-typed sub-inputs
 * (`RepoMultiselect`, `RepoNumberMap`). Moved out of `ConfigPanel.tsx` (where
 * it lived beside the automations Config tab) and exported so a second config
 * surface outside the automations feature — the product policy phase 1
 * `AutomationPolicyCard` — renders through the SAME field-type switch rather
 * than growing a parallel copy of it.
 */
import { useState, type KeyboardEvent } from 'react'
import type { ConfigField } from '@/api/automations'
import { useGithubRepos } from '@/api/hooks'
import { Badge } from '@/ui/Badge'
import { Checkbox } from '@/ui/Checkbox'
import { Input } from '@/ui/Input'
import { Select } from '@/ui/Select'

export interface FieldValueProps {
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

// A freeform list of tags (e.g. product `skip_labels`): unlike `RepoMultiselect`,
// there is no fixed catalog to pick from, so this is type-and-commit rather than
// check-a-box. Enter or comma commits the draft as a chip; Backspace on an empty
// draft pops the last one, matching common tag-input muscle memory.
function TagListInput({ field, value, onChange }: FieldValueProps) {
  const tags: string[] = Array.isArray(value) ? (value as string[]) : []
  const [draft, setDraft] = useState('')

  function addTag(raw: string) {
    const tag = raw.trim()
    if (!tag) { setDraft(''); return }
    if (tags.some((t) => t.toLowerCase() === tag.toLowerCase())) { setDraft(''); return }
    onChange(field.key, [...tags, tag])
    setDraft('')
  }

  function removeTag(tag: string) {
    onChange(field.key, tags.filter((t) => t !== tag))
  }

  function handleKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === 'Enter' || e.key === ',') {
      e.preventDefault()
      addTag(draft)
    } else if (e.key === 'Backspace' && draft === '' && tags.length > 0) {
      removeTag(tags[tags.length - 1])
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-1.5 rounded-md border border-edge bg-surface p-1.5">
      {tags.map((tag) => (
        <Badge key={tag} tone="neutral" className="gap-1 pr-1">
          {tag}
          <button
            type="button"
            onClick={() => removeTag(tag)}
            aria-label={`Remove ${tag}`}
            className="leading-none text-ink-muted hover:text-ink"
          >
            ×
          </button>
        </Badge>
      ))}
      <Input
        id={field.key}
        type="text"
        size="sm"
        value={draft}
        placeholder={tags.length === 0 ? 'Add a label…' : ''}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={handleKeyDown}
        onBlur={() => addTag(draft)}
        className="min-w-[8rem] flex-1 border-none bg-transparent shadow-none focus:ring-0"
      />
    </div>
  )
}

export function ConfigInput({ field, value, onChange, enabledRepos }: FieldValueProps & {
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
          // The floor comes from the schema (`ConfigField.min`), never a literal
          // per call site: the server 422s on the same bound, and a hard-coded
          // `min={0}` here would be a second, silently drifting copy of it.
          min={field.min ?? undefined}
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
    case 'string-list':
      return <TagListInput field={field} value={value} onChange={onChange} />
    case 'repo-number-map':
      return <RepoNumberMap field={field} value={value} onChange={onChange}
        enabledRepos={enabledRepos ?? []} />
    default:
      return null
  }
}
