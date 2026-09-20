import * as React from 'react'
import { Link } from 'react-router-dom'
import {
  useSettings, useUpdateSettings, type AppSettings, type GlobalCapDefaults,
} from '@/api/hooks'
import { Input } from '@/ui/Input'
import { Banner } from '@/ui/Banner'
import { Skeleton } from '@/ui/Skeleton'
import { settingsSectionPath } from '@/shell/navDefs'
import { SETTINGS_BY_ID } from '../settingsDefs'
import { SettingsSection } from '../chassis/SettingsSection'
import { SettingsGroup } from '../chassis/SettingsGroup'
import { SettingRow } from '../chassis/SettingRow'
import { SaveBar } from '../chassis/SaveBar'
import { ManagedEnvTable } from '../chassis/ManagedEnvTable'
import { LiveReadoutCard } from '../chassis/LiveReadoutCard'
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
 * "none"); anything else must be a whole number >= 1 — see `attentionError`.
 * This must NEVER silently coerce a bad value to null: "0" or "-3" reads as
 * "the user wants a lower cap" and silently handing them the (often much
 * higher) physical ceiling instead is worse than refusing the save.
 */
function toPayload(d: LimitsDraft): Partial<AppSettings> {
  if (d.attention.trim() === '') return { maxActiveSessions: null }
  return { maxActiveSessions: Number.parseInt(d.attention, 10) }
}

/**
 * Pure validation for the attention-ceiling draft (F3). Blank is valid (clears
 * the ceiling); anything else must be a whole number >= 1 — mirrors the Hosts
 * "cap for {name}" field's own validation in FleetSection.
 */
// eslint-disable-next-line react-refresh/only-export-components
export function attentionError(draft: string): string | null {
  if (draft.trim() === '') return null
  const n = Number(draft.trim())
  if (!Number.isInteger(n) || n < 1) {
    return 'Attention ceiling must be a whole number ≥ 1, or blank'
  }
  return null
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
  const initial = React.useMemo(() => (settings ? toDraft(settings) : undefined), [settings])
  const form = useSettingsForm<LimitsDraft>({
    initial,
    save: (d) => update.mutateAsync(toPayload(d)),
    // An invalid attention ceiling must never reach the server — beforeSave
    // aborts before `save` runs. It does not surface as `form.error` (that
    // banner is reserved for a save that was ATTEMPTED and rejected), so the
    // row-level error below is driven straight off the live draft instead.
    beforeSave: (d) => attentionError(d.attention) === null,
  })

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
  const attnError = attentionError(draft.attention)

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
          error={attnError}
        >
          <Input
            id="limits-attention"
            // `type="text"`, not `number`: a number input SANITIZES non-numeric
            // keystrokes at the DOM level, so "abc" would never reach `set` —
            // the same reasoning as the Hosts "cap for {name}" field.
            type="text"
            inputMode="numeric"
            aria-invalid={attnError ? true : undefined}
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

      <LiveReadoutCard />

      <SettingsGroup
        legend="Managed by env"
        description="Ceilings outside the stack, read at daemon start. Not editable here."
      >
        <ManagedEnvTable flags={settings.managed} keys={CAPACITY_MANAGED_KEYS} />
      </SettingsGroup>
    </SettingsSection>
  )
}
