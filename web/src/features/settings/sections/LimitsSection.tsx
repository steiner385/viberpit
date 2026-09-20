import * as React from 'react'
import { Link } from 'react-router-dom'
import {
  useSettings, useUpdateSettings, useCapacity, type AppSettings, type GlobalCapDefaults,
} from '@/api/hooks'
import { Input } from '@/ui/Input'
import { Banner } from '@/ui/Banner'
import { Skeleton } from '@/ui/Skeleton'
import { settingsSectionPath } from '@/shell/navDefs'
import { deliveryPath } from '@/features/delivery/deliveryNavState'
import { buildCapacityStack } from '@/features/delivery/capacity/capacityStack'
import { SETTINGS_BY_ID } from '../settingsDefs'
import { SettingsSection } from '../chassis/SettingsSection'
import { SettingsGroup } from '../chassis/SettingsGroup'
import { SettingRow } from '../chassis/SettingRow'
import { SaveBar } from '../chassis/SaveBar'
import { ManagedEnvTable } from '../chassis/ManagedEnvTable'
import { useSettingsForm } from '../useSettingsForm'

/** Env-managed ceilings that sit OUTSIDE the five-layer stack (spec 2026-09-20 D9). */
// eslint-disable-next-line react-refresh/only-export-components
export const CAPACITY_MANAGED_KEYS = [
  'CCSWITCH_TEST_SLOT_CAP',
  'CCSWITCH_CHILD_SESSION_CAP',
  'CCSWITCH_STT_MAX_CONCURRENT',
  'VIBERPIT_CAPACITY_GOVERNOR_ENABLED',
] as const

interface LimitsDraft { attention: string }

function toDraft(s: AppSettings): LimitsDraft {
  return { attention: s.maxActiveSessions != null ? String(s.maxActiveSessions) : '' }
}

/**
 * Blank clears the attention ceiling (the server treats an explicit null as
 * "none"). A value that is not a whole number ≥ 1 is also sent as null rather
 * than becoming 1 — the old form's clamp turned a half-deleted "10" into 1.
 */
function toPayload(d: LimitsDraft): Partial<AppSettings> {
  const n = Number.parseInt(d.attention, 10)
  return { maxActiveSessions: Number.isFinite(n) && n >= 1 ? n : null }
}

// eslint-disable-next-line react-refresh/only-export-components
export function physicalLine(gc: GlobalCapDefaults): string {
  if (gc.hosts.length === 0) return 'unbounded — no hosts registered'
  const uncapped = gc.hosts.filter((h) => h.cap === null).map((h) => h.name)
  if (uncapped.length > 0) {
    return `unbounded — ${uncapped.join(', ')} ${uncapped.length === 1 ? 'has' : 'have'} no cap`
  }
  return `${gc.physical} — ${gc.hosts.map((h) => `${h.name} ${h.cap}`).join(' · ')}`
}

// eslint-disable-next-line react-refresh/only-export-components
export function effectiveLine(gc: GlobalCapDefaults): string {
  switch (gc.binding) {
    case 'physical':
      return `${gc.effective} — the physical ceiling binds`
    case 'attention':
      return gc.physical !== null && gc.attention === gc.physical
        ? `${gc.effective} — the attention ceiling binds (equal to the physical ceiling)`
        : `${gc.effective} — the attention ceiling binds`
    default:
      return `${gc.effective} — default; set a host cap on Hosts or an attention ceiling`
  }
}

/**
 * Settings › Capacity › Limits (spec 2026-09-20 §5.1): the global cap as its two
 * ceilings, a read-only copy of the live ceiling stack, and the env-managed
 * ceilings outside the stack. Form save model; the attention ceiling is the
 * only editable field.
 */
export function LimitsSection() {
  const def = SETTINGS_BY_ID.capacity
  const { data: settings, isError, error } = useSettings()
  const update = useUpdateSettings()
  const { data: capacity } = useCapacity()
  const initial = React.useMemo(() => (settings ? toDraft(settings) : undefined), [settings])
  const form = useSettingsForm<LimitsDraft>({ initial, save: (d) => update.mutateAsync(toPayload(d)) })

  if (!settings) {
    return (
      <SettingsSection id="capacity" title={def.label} scope="server" description={def.description}>
        {isError
          ? (
            <Banner tone="danger" role="alert" title="Settings could not be loaded">
              {error?.message ?? 'The server did not answer.'}
            </Banner>
          )
          : <Skeleton lines={4} />}
      </SettingsSection>
    )
  }
  if (!form.draft) return null
  const { draft, set } = form
  const gc = settings.effectiveDefaults.globalCap
  const stack = capacity ? buildCapacityStack(capacity) : null

  return (
    <SettingsSection
      id="capacity"
      title={def.label}
      scope="server"
      description={def.description}
      footer={<SaveBar form={form} />}
    >
      {form.error && <Banner tone="danger" role="alert" title="Not saved">{form.error}</Banner>}

      <SettingsGroup
        legend="Global cap"
        description="Sessions beyond the effective cap queue and start when a slot frees. The cap is the lower of the two ceilings."
      >
        <SettingRow
          label="Physical ceiling"
          htmlFor="limits-physical"
          description="The sum of the host caps on enabled hosts."
          selfLabelled
        >
          <span id="limits-physical" className="text-sm text-ink">
            {physicalLine(gc)}
            {gc.physical === null && (
              <>
                {' '}
                <Link to={settingsSectionPath('fleet')} className="text-accent hover:underline">Set a cap on Hosts</Link>
              </>
            )}
          </span>
        </SettingRow>
        <SettingRow
          label="Attention ceiling"
          htmlFor="limits-attention"
          description="Never run more than this many sessions at once, whatever the hardware could take. Blank leaves the physical ceiling in charge."
        >
          <Input
            id="limits-attention"
            type="number"
            min={1}
            step={1}
            inputMode="numeric"
            placeholder={gc.physical === null ? 'None' : `None · physical ceiling ${gc.physical} applies`}
            value={draft.attention}
            onChange={(e) => set('attention', e.target.value)}
          />
        </SettingRow>
        <SettingRow
          label="Effective global cap"
          htmlFor="limits-effective"
          description="What the admission gate compares against right now."
          selfLabelled
        >
          <span id="limits-effective" className="text-sm font-medium text-ink">{effectiveLine(gc)}</span>
        </SettingRow>
      </SettingsGroup>

      <SettingsGroup
        legend="Live readout"
        description={(
          <>
            The whole ceiling stack as automation sees it.{' '}
            <Link to={deliveryPath('capacity')} className="text-accent hover:underline">Open Delivery → Capacity</Link>
          </>
        )}
      >
        {stack === null
          ? <p className="py-3 text-sm text-ink-muted">Loading the readout…</p>
          : (
            <ul className="divide-y divide-edge">
              {stack.map((row) => (
                <li key={row.key} className="flex items-baseline justify-between gap-3 py-2 text-sm">
                  <span className={row.binding ? 'font-medium text-warning' : 'text-ink'}>{row.label}</span>
                  <span className="tabular-nums text-ink-muted">
                    {`${row.used} / ${row.cap ?? '∞'} · ${row.free ?? '∞'} free`}
                  </span>
                </li>
              ))}
            </ul>
          )}
        {capacity?.binding && (
          <Banner tone="warning" role="status" title="Binding constraint">{capacity.binding.text}</Banner>
        )}
      </SettingsGroup>

      <SettingsGroup
        legend="Managed by env"
        description="Ceilings outside the stack, read at daemon start. Not editable here."
      >
        <ManagedEnvTable flags={settings.managed} keys={CAPACITY_MANAGED_KEYS} />
      </SettingsGroup>
    </SettingsSection>
  )
}
