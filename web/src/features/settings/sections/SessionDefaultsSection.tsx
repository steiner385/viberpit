import * as React from 'react'
import { useSettings, useUpdateSettings, type AppSettings } from '@/api/hooks'
import { PERMISSION_MODES } from '@/session/permissionModes'
import { Select } from '@/ui/Select'
import { Input } from '@/ui/Input'
import { Banner } from '@/ui/Banner'
import { Checkbox } from '@/ui/Checkbox'
import { Skeleton } from '@/ui/Skeleton'
import { RadioGroup } from '@/ui/RadioGroup'
import { ConfirmDialog } from '@/ui/ConfirmDialog'
import { SETTINGS_BY_ID } from '../settingsDefs'
import { SettingsSection } from '../chassis/SettingsSection'
import { SettingsGroup } from '../chassis/SettingsGroup'
import { SettingRow } from '../chassis/SettingRow'
import { SaveBar } from '../chassis/SaveBar'
import { useSettingsForm } from '../useSettingsForm'

/** '' is the draft's "server default" — the payload turns it back into null. */
type ApprovalAction = '' | 'require_approval' | 'block' | 'allow'
type ToolDetail = AppSettings['defaultToolDetail']

interface DefaultsDraft {
  permissionMode: string
  modelApprovalAction: ApprovalAction
  denylist: string
  toolDetail: ToolDetail
  thinkingCollapsed: boolean
}

/** Modes that let a session act without a human in the loop; saving one asks first (S17). */
const UNSAFE_MODES = new Set(['full-auto', 'bypassPermissions'])

const ACTION_LABEL: Record<Exclude<ApprovalAction, ''>, string> = {
  require_approval: 'Require approval',
  block: 'Block',
  allow: 'Allow',
}

/** The server's effective action is a plain `string`; show its label when we know one. */
function actionLabel(action: string): string {
  return ACTION_LABEL[action as Exclude<ApprovalAction, ''>] ?? action
}

/**
 * S21. Every field comes off the server payload — `thinkingCollapsed` in
 * particular, which the old form initialised to a hard-coded `false` and so
 * silently offered to turn a server-side `true` back off on the next save.
 */
function toDraft(s: AppSettings): DefaultsDraft {
  return {
    permissionMode: s.defaultPermissionMode,
    modelApprovalAction: (s.modelApprovalAction ?? '') as ApprovalAction,
    denylist: s.modelApprovalDenylist ?? '',
    toolDetail: s.defaultToolDetail,
    thinkingCollapsed: s.thinkingCollapsed,
  }
}

/**
 * The PUT body. Both model-approval fields clear to the SERVER default with
 * `null`, so blank never means "the empty policy": an unselected action and an
 * empty denylist both send null and the server keeps deciding.
 */
function toPayload(d: DefaultsDraft): Partial<AppSettings> {
  return {
    defaultPermissionMode: d.permissionMode,
    modelApprovalAction: d.modelApprovalAction || null,
    modelApprovalDenylist: d.denylist.trim() || null,
    defaultToolDetail: d.toolDetail,
    thinkingCollapsed: d.thinkingCollapsed,
  }
}

/**
 * Settings › Sessions › Defaults (spec §8): what every new session starts with —
 * tool approvals, model gating and transcript display — on the section chassis
 * with the Form save model.
 *
 * ## What changed from `components/settings/GeneralSection.tsx`
 *
 * These four controls used to sit in the same flat column as the paths, the
 * archive policy and the capacity limits, under one Save. Here they are three
 * named groups in the section that owns them, and two behaviours are new.
 *
 * S17: choosing a mode that runs shell commands with no prompt is a
 * consequential change, and it used to be one click away from being saved with
 * nothing said about it. It now shows a standing warning as soon as it is
 * selected and a ConfirmDialog on Save — but only when the mode CHANGED to an
 * unsafe one, so a server already set to full auto is not re-confirmed for an
 * unrelated edit to the denylist.
 *
 * DEC-4: a mode the server build does not accept is DISABLED with the reason
 * spelled out, never dropped from the list. A silently shorter list reads as "we
 * removed that mode", and the user cannot tell an unsupported mode from a
 * mistyped one.
 */
export function SessionDefaultsSection() {
  const { data: settings, isLoading, isError, error } = useSettings()
  const update = useUpdateSettings()
  // The gate is a PROMISE handed to `useSettingsForm`, resolved by the dialog, so
  // Save is genuinely suspended until the user answers (same shape as
  // GeneralSection's archive-move gate; R8 covers the racing clicks).
  const [confirm, setConfirm] = React.useState<{ resolve: (ok: boolean) => void; mode: string } | null>(null)

  const initial = React.useMemo(() => (settings ? toDraft(settings) : undefined), [settings])
  const form = useSettingsForm<DefaultsDraft>({
    initial,
    save: (d) => update.mutateAsync(toPayload(d)),
    beforeSave: (d, base) => (
      UNSAFE_MODES.has(d.permissionMode) && d.permissionMode !== base.permissionMode
        ? new Promise<boolean>((resolve) => setConfirm({ resolve, mode: d.permissionMode }))
        : true
    ),
  })

  // Loading and error are STATES OF THIS SECTION, not nothing at all — see the
  // same block in `GeneralSection`.
  if (isError || isLoading) {
    return (
      <SettingsSection id="sessions" title="Defaults" scope="server" description={SETTINGS_BY_ID.sessions.description}>
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
  if (!form.draft || !settings) return null
  const { draft, set } = form
  const accepted = new Set(settings.acceptedPermissionModes)
  const rejected = PERMISSION_MODES.filter((m) => !accepted.has(m.value))
  const eff = settings.effectiveDefaults
  const confirmMode = confirm?.mode
  const confirmLabel = PERMISSION_MODES.find((m) => m.value === confirmMode)?.label ?? confirmMode

  return (
    <SettingsSection
      id="sessions"
      title="Defaults"
      scope="server"
      description={SETTINGS_BY_ID.sessions.description}
      footer={<SaveBar form={form} />}
    >
      {form.error && <Banner tone="danger" role="alert" title="Not saved">{form.error}</Banner>}

      <SettingsGroup legend="Tool approvals">
        <SettingRow
          label="Default tool approvals"
          htmlFor="defaults-permission-mode"
          description={rejected.length
            ? `Applied to new sessions. ${rejected.map((m) => m.label).join(', ')}: not accepted by this server version.`
            : 'Applied to new sessions and to automations that launch sessions.'}
        >
          <Select
            id="defaults-permission-mode"
            value={draft.permissionMode}
            onChange={(e) => set('permissionMode', e.target.value)}
          >
            {PERMISSION_MODES.map((m) => (
              <option key={m.value} value={m.value} disabled={!accepted.has(m.value)}>{m.label}</option>
            ))}
          </Select>
        </SettingRow>
        {UNSAFE_MODES.has(draft.permissionMode) && (
          <Banner tone="warning" role="alert" title="Sessions will run without prompting">
            Anyone who can reach this server can run code as you. Saving asks you to confirm.
          </Banner>
        )}
      </SettingsGroup>

      <SettingsGroup legend="Model approval">
        {/* R14b: a radio group is named by its own legend, and `htmlFor` can only
            name one labelable control — so the row passes `selfLabelled` (no
            `<label htmlFor>` dangling at an id no element carries) and the legend
            is the row's one visible label. */}
        <SettingRow
          htmlFor="defaults-model-action"
          wide
          description={<>Server default is <code>{eff.modelApprovalAction}</code> from <code>CCSWITCH_MODEL_APPROVAL_ACTION</code>.</>}
          selfLabelled
        >
          <RadioGroup
            name="defaults-model-action"
            legend="When a session asks for a gated model"
            value={draft.modelApprovalAction}
            onChange={(v) => set('modelApprovalAction', v)}
            options={[
              { value: '', label: `Server default (${actionLabel(eff.modelApprovalAction).toLowerCase()})` },
              { value: 'require_approval', label: ACTION_LABEL.require_approval },
              { value: 'block', label: ACTION_LABEL.block },
              { value: 'allow', label: ACTION_LABEL.allow },
            ]}
          />
        </SettingRow>
        <SettingRow
          label="Gated models"
          htmlFor="defaults-denylist"
          description="Comma-separated tiers or model ids that need approval. Blank uses the server default."
        >
          <Input
            id="defaults-denylist"
            value={draft.denylist}
            // R14a, mirroring R13 in GeneralSection: with no gated tiers configured
            // there is nothing to name, and the literal-plus-suffix form would read
            // "Server default · server default".
            placeholder={eff.modelApprovalDenylist.trim() ? `${eff.modelApprovalDenylist} · server default` : 'Server default'}
            onChange={(e) => set('denylist', e.target.value)}
          />
        </SettingRow>
      </SettingsGroup>

      <SettingsGroup legend="Transcript">
        <SettingRow
          htmlFor="defaults-tool-detail"
          wide
          description="How tool-call groups open when a transcript loads."
          selfLabelled
        >
          <RadioGroup
            name="defaults-tool-detail"
            legend="Tool calls"
            value={draft.toolDetail}
            onChange={(v) => set('toolDetail', v)}
            options={[
              { value: 'auto', label: 'Auto-fold long groups' },
              { value: 'all-open', label: 'Expand all' },
              { value: 'all-closed', label: 'Collapse all' },
            ]}
          />
        </SettingRow>
        <SettingRow
          label="Thinking blocks"
          htmlFor="defaults-thinking"
          description="Start collapsed; expand any block by clicking it."
          selfLabelled
        >
          <Checkbox
            id="defaults-thinking"
            label="Collapse thinking by default"
            checked={draft.thinkingCollapsed}
            onChange={(v) => set('thinkingCollapsed', v)}
          />
        </SettingRow>
      </SettingsGroup>

      <ConfirmDialog
        open={confirm !== null}
        onClose={() => { confirm?.resolve(false); setConfirm(null) }}
        onConfirm={() => { confirm?.resolve(true); setConfirm(null) }}
        title="Allow sessions to run without prompting?"
        description={`New sessions will start in “${confirmLabel}”. Anyone who can reach this server can run shell commands and edit files as you.`}
        confirmLabel={confirmMode === 'full-auto' ? 'Use full auto' : 'Bypass permissions'}
        tone="warning"
      />
    </SettingsSection>
  )
}
