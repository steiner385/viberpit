/**
 * NotificationsSection — this device's web push and the server's quiet hours
 * (issue #602). Delivery channels and routing rules are sibling sections
 * (`NotificationChannelsSection`, `NotificationRulesSection`, #2077): two CRUD
 * lists stacked under two forms made one 600-line column separated only by
 * rules, and Split-variant pages never take a tab strip (docs/PAGE-TABS.md §2).
 */
import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { cn } from '@/lib/utils'
import { Button } from '@/ui/Button'
import { api } from '@/api/client'
import { SETTINGS_BY_ID } from '../settingsDefs'
import { SettingsSection } from '../chassis/SettingsSection'
import { SettingsGroup } from '../chassis/SettingsGroup'
import { NotificationsToggle } from './NotificationsToggle'
import { NOTIF_KEYS, errMessage, inputClass, type QuietHours } from './notificationsApi'

export function NotificationsSection() {
  const def = SETTINGS_BY_ID.notifications
  const qc = useQueryClient()
  const { data, isLoading } = useQuery({
    queryKey: NOTIF_KEYS.quiet,
    queryFn: () => api.get<QuietHours>('/notifications/quiet-hours'),
  })

  return (
    <SettingsSection id={def.id} title={def.label} description={def.description} scope={def.scope}>
      <SettingsGroup legend="Phone notifications">
        <div className="py-3">
          <NotificationsToggle />
        </div>
      </SettingsGroup>

      {/* Quiet hours are stored on the server, so they hold for every device —
          unlike the push switch above, which is this browser's subscription. */}
      <SettingsGroup legend="Quiet hours" description="Applies to every device.">
        <div className="py-3">
          {isLoading ? (
            <p className="text-sm text-ink-muted">Loading…</p>
          ) : (
            <QuietHoursForm key={`${data?.start}-${data?.end}-${data?.timezone}`} initial={data} qc={qc} />
          )}
        </div>
      </SettingsGroup>
    </SettingsSection>
  )
}

// The browser's own zone, offered as the Timezone field's starting value so a
// freshly-configured window defaults to "local to me" rather than silently UTC (#2562).
const BROWSER_TIMEZONE = Intl.DateTimeFormat().resolvedOptions().timeZone

function QuietHoursForm({ initial, qc }: { initial: QuietHours | undefined; qc: ReturnType<typeof useQueryClient> }) {
  const [start, setStart] = useState(initial?.start ?? '')
  const [end, setEnd] = useState(initial?.end ?? '')
  const [tz, setTz] = useState(initial?.timezone ?? BROWSER_TIMEZONE)

  const save = useMutation({
    mutationFn: (body: QuietHours) => api.put<QuietHours>('/notifications/quiet-hours', body),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: NOTIF_KEYS.quiet }) },
  })

  function handleSave() {
    save.mutate({ start: start || null, end: end || null, timezone: tz || null })
  }
  function handleClear() {
    setStart('')
    setEnd('')
    setTz('')
    save.mutate({ start: null, end: null, timezone: null })
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-3">
        <div className="space-y-1">
          <label htmlFor="quiet-start" className="block text-sm font-medium text-ink">Start</label>
          <input
            id="quiet-start"
            type="time"
            value={start}
            onChange={(e) => setStart(e.target.value)}
            className={cn(inputClass, 'w-auto')}
          />
        </div>
        <div className="space-y-1">
          <label htmlFor="quiet-end" className="block text-sm font-medium text-ink">End</label>
          <input
            id="quiet-end"
            type="time"
            value={end}
            onChange={(e) => setEnd(e.target.value)}
            className={cn(inputClass, 'w-auto')}
          />
        </div>
        <div className="space-y-1">
          <label htmlFor="quiet-timezone" className="block text-sm font-medium text-ink">Timezone</label>
          <input
            id="quiet-timezone"
            type="text"
            value={tz}
            onChange={(e) => setTz(e.target.value)}
            placeholder="UTC"
            className={cn(inputClass, 'w-48')}
          />
        </div>
        <Button type="button" onClick={handleSave} disabled={save.isPending} className="min-h-target">
          {save.isPending ? 'Saving…' : 'Save'}
        </Button>
        <Button type="button" variant="secondary" onClick={handleClear} disabled={save.isPending} className="min-h-target">
          Clear
        </Button>
      </div>
      <p className="text-xs text-ink-muted">
        Start/End are local to the timezone above (an IANA name, e.g. &lsquo;America/New_York&rsquo;;
        blank means UTC). Rules marked &lsquo;critical&rsquo; bypass quiet hours.
      </p>
      {save.isError && (
        <p role="alert" className="text-sm text-danger">{errMessage(save.error)}</p>
      )}
    </div>
  )
}
