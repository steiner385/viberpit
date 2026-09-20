/**
 * The Config tab: an automation's `configSchema` rendered as controls that
 * patch on every change. Moved from `AutomationCard.tsx` (`ConfigPanel`,
 * `ConfigInput`, `RepoMultiselect`, `RepoNumberMap`) onto `Field`/`Input`/
 * `Select`/`Checkbox` — same live-patch-on-change behaviour. `ConfigInput`
 * itself now lives in `ConfigInput.tsx`, exported for the product policy
 * phase 1 `AutomationPolicyCard` to reuse.
 */
import { useState } from 'react'
import { Link } from 'react-router-dom'
import type { Automation } from '@/api/automations'
import { usePatchAutomation } from '@/api/hooks'
import { Field } from '@/ui/Field'
import { ConfigInput } from './ConfigInput'
import { deliveryPath } from '../deliveryNavState'

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
          field.surface === 'capacity'
            ? (
              // Owned by Delivery → Capacity (spec 2026-09-20 D7): shown, never
              // edited here. Reads the live `a.config` prop, not `localConfig`
              // (the mount-time edit buffer) — this value is owned by another
              // page, so it can change under this card without a re-mount.
              <div key={field.key} className="flex flex-col gap-0.5">
                <p className="text-sm font-medium text-ink">{field.label}</p>
                <p className="text-sm text-ink">{String(a.config[field.key] ?? field.default ?? '—')}</p>
                <p className="text-xs text-ink-muted">
                  {field.help}{field.help ? ' ' : ''}
                  <Link to={deliveryPath('capacity')} className="text-accent hover:underline">Set on Delivery → Capacity</Link>
                </p>
              </div>
            )
            : field.type === 'bool'
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
