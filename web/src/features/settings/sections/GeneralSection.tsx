import * as React from 'react'
import { useSettings, useUpdateSettings, useArchivedSessions, type AppSettings } from '@/api/hooks'
import { Input } from '@/ui/Input'
import { Banner } from '@/ui/Banner'
import { Button } from '@/ui/Button'
import { Checkbox } from '@/ui/Checkbox'
import { ConfirmDialog } from '@/ui/ConfirmDialog'
import { SettingsSection } from '../chassis/SettingsSection'
import { SettingsGroup } from '../chassis/SettingsGroup'
import { SettingRow } from '../chassis/SettingRow'
import { SaveBar } from '../chassis/SaveBar'
import { useSettingsForm } from '../useSettingsForm'

/**
 * Every field is a STRING so that blank can mean "server default" — the one
 * distinction the old form lost by coercing to numbers as the user typed, which
 * turned a half-deleted "10" into 1 and then into an accidental save.
 */
interface GeneralDraft {
  repoRoot: string
  archiveHome: string
  autoArchiveEnabled: boolean
  maxArchived: string
  maxActive: string
}

function toDraft(s: AppSettings): GeneralDraft {
  return {
    repoRoot: s.repoRoot ?? '',
    archiveHome: s.archiveHome ?? '',
    autoArchiveEnabled: s.autoArchiveEnabled,
    maxArchived: s.maxArchivedSessions != null ? String(s.maxArchivedSessions) : '',
    maxActive: s.maxActiveSessions != null ? String(s.maxActiveSessions) : '',
  }
}

/**
 * The PUT body, unchanged from the old form (inventory §1a) — the server's
 * null/0/"" conventions are not symmetrical and each one is load-bearing:
 * `repoRoot` null clears it, `archiveHome` clears with "" (null would mean "no
 * change"), `maxArchivedSessions` clears with 0 (the backend maps <=0 to null)
 * and `maxActiveSessions` clears with null.
 */
function toPayload(d: GeneralDraft): Partial<AppSettings> {
  const archived = Number.parseInt(d.maxArchived, 10)
  const active = Number.parseInt(d.maxActive, 10)
  return {
    repoRoot: d.repoRoot.trim() || null,
    // "" clears to default (null would mean "no change"), so this is a string
    // and never null. Trimmed, unlike the old form, for one reason: the S16
    // gate compares trimmed paths, so an untrimmed payload would let a
    // whitespace-only edit change the stored path with NO confirmation, and
    // would save "/mnt/slow " after the dialog made the user type "/mnt/slow".
    archiveHome: d.archiveHome.trim(),
    maxArchivedSessions: Number.isFinite(archived) && archived > 0 ? archived : 0, // 0 = unlimited
    maxActiveSessions: Number.isFinite(active) && active > 0 ? active : null,
    autoArchiveEnabled: d.autoArchiveEnabled,
  }
}

/**
 * A path input plus its Reset-to-default affordance, as the ONE control a
 * SettingRow holds. It forwards `aria-describedby` to the input the way
 * `ui/Checkbox` does: the row clones its child to attach the description id, and
 * a bare wrapping `<span>` would swallow it — leaving the input undescribed and
 * the description announced on nothing.
 */
function PathControl({ id, value, placeholder, resetLabel, onChange, ...aria }: {
  id: string
  value: string
  placeholder: string
  resetLabel: string
  onChange: (next: string) => void
  'aria-describedby'?: string
}) {
  return (
    <span className="flex w-full items-center gap-2">
      <Input
        id={id}
        value={value}
        placeholder={placeholder}
        aria-describedby={aria['aria-describedby']}
        onChange={(e) => onChange(e.target.value)}
      />
      <Button
        type="button"
        variant="ghost"
        size="sm"
        aria-label={resetLabel}
        onClick={() => onChange('')}
        disabled={value === ''}
      >
        Reset
      </Button>
    </span>
  )
}

function archiveCount(n: number): string {
  return `about ${n} archived session${n === 1 ? '' : 's'}`
}

/**
 * Settings › General (spec §8): the server-wide paths, the archive policy and
 * the session capacity, on the section chassis with the Form save model.
 *
 * ## What changed from `components/settings/GeneralSection.tsx`
 *
 * That form put nine unrelated controls in one flat column with a single Save
 * button — permission mode, model approval, transcript display and
 * notifications alongside these five — so every field the user could see was a
 * field a mistyped Save could change. Here it is five fields in three named
 * groups, and the fields that moved live in the sections that own them
 * (Defaults, Notifications).
 *
 * The other change is friction where it was missing (S16). Editing the archive
 * path used to be one keystroke away from relocating every archived session on
 * disk, with no warning and no count; it is now the Typed tier of
 * `ConfirmDialog`, and the user has to type the destination path to proceed.
 * Clearing the path back to the server default is still a move, but a
 * recoverable and expected one, so it drops to the Confirm tier.
 */
export function GeneralSection() {
  const { data: settings } = useSettings()
  const update = useUpdateSettings()
  const { data: archived = [] } = useArchivedSessions()
  // The gate is a PROMISE handed to `useSettingsForm`: the dialog is what
  // resolves it, so Save is genuinely suspended (not fired-and-forgotten) until
  // the user answers. `useSettingsForm`'s in-flight latch (R8) is what keeps a
  // second Save click, or a Discard, from racing the open dialog.
  const [confirmPath, setConfirmPath] = React.useState<{ resolve: (ok: boolean) => void; path: string } | null>(null)

  const initial = React.useMemo(() => (settings ? toDraft(settings) : undefined), [settings])
  const form = useSettingsForm<GeneralDraft>({
    initial,
    save: (d) => update.mutateAsync(toPayload(d)),
    beforeSave: (d, base) => (
      d.archiveHome.trim() === base.archiveHome.trim()
        ? true
        : new Promise<boolean>((resolve) => setConfirmPath({ resolve, path: d.archiveHome.trim() }))
    ),
  })

  if (!form.draft || !settings) return null
  const { draft, set } = form
  const eff = settings.effectiveDefaults
  const target = confirmPath?.path || 'the server default location'

  return (
    <SettingsSection
      id="general"
      title="General"
      scope="server"
      description="Where repos are cloned and archives are stored, and how many sessions run and are kept."
      footer={<SaveBar form={form} />}
    >
      {form.error && <Banner tone="danger" role="alert" title="Not saved">{form.error}</Banner>}

      <SettingsGroup legend="Paths">
        <SettingRow
          label="Repo clone root"
          htmlFor="general-repo-root"
          description="New sessions clone repositories under this directory."
        >
          <PathControl
            id="general-repo-root"
            value={draft.repoRoot}
            placeholder={`${eff.repoRoot} · server default`}
            resetLabel="Reset repo clone root to default"
            onChange={(v) => set('repoRoot', v)}
          />
        </SettingRow>
        <SettingRow
          label="Archive storage path"
          htmlFor="general-archive-home"
          description="Where archived session files live. Changing it moves every existing archive."
        >
          <PathControl
            id="general-archive-home"
            value={draft.archiveHome}
            // R13. When the server reports no configured archive home there is no
            // path to show, so the placeholder is just "Server default" — the
            // literal-plus-suffix form read "Server default · server default",
            // and an unconfigured archive home is the common deployment.
            placeholder={eff.archiveHome ? `${eff.archiveHome} · server default` : 'Server default'}
            resetLabel="Reset archive storage path to default"
            onChange={(v) => set('archiveHome', v)}
          />
        </SettingRow>
      </SettingsGroup>

      <SettingsGroup legend="Archive" description="What happens to sessions after they finish.">
        <SettingRow
          label="Auto-archive"
          htmlFor="general-auto-archive"
          description="A session that finishes its goal is archived automatically. Off keeps it on the dashboard until you archive it."
          selfLabelled
        >
          <Checkbox
            id="general-auto-archive"
            label="Auto-archive completed sessions"
            checked={draft.autoArchiveEnabled}
            onChange={(v) => set('autoArchiveEnabled', v)}
          />
        </SettingRow>
        <SettingRow
          label="Max archived sessions"
          htmlFor="general-max-archived"
          description="Above this count the oldest archives are permanently purged. Blank keeps everything."
        >
          <Input
            id="general-max-archived"
            type="number"
            min={1}
            step={1}
            inputMode="numeric"
            placeholder="Unlimited"
            value={draft.maxArchived}
            onChange={(e) => set('maxArchived', e.target.value)}
          />
        </SettingRow>
      </SettingsGroup>

      <SettingsGroup legend="Capacity">
        <SettingRow
          label="Max active sessions"
          htmlFor="general-max-active"
          description="Sessions beyond this limit queue and start when a slot frees."
        >
          <Input
            id="general-max-active"
            type="number"
            min={1}
            step={1}
            inputMode="numeric"
            placeholder={`${eff.maxActiveSessions} · server default`}
            value={draft.maxActive}
            onChange={(e) => set('maxActive', e.target.value)}
          />
        </SettingRow>
      </SettingsGroup>

      <ConfirmDialog
        open={confirmPath !== null}
        onClose={() => { confirmPath?.resolve(false); setConfirmPath(null) }}
        onConfirm={() => { confirmPath?.resolve(true); setConfirmPath(null) }}
        title="Move archived sessions?"
        description={`Saving this path moves ${archiveCount(archived.length)} to ${target}. If any of them fails to move, nothing is saved and the path is left alone.`}
        confirmLabel="Move archives"
        tone="warning"
        // Typed tier for a real path, Confirm tier for clearing back to the
        // default: `typed` must be a non-empty string or the input would ask the
        // user to type nothing and gate on it.
        typed={confirmPath?.path || undefined}
      />
    </SettingsSection>
  )
}
