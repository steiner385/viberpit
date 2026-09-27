/**
 * The Schedule tab: active-hours / cron editor for one automation. The tab is
 * the heading (docs/PAGE-TABS.md §7), so the panel opens with the sentence
 * that says what the editor changes, not a second "Schedule" label.
 */
import type { Automation } from '@/api/automations'
import { usePatchAutomation } from '@/api/hooks'
import { ScheduleEditor } from './ScheduleEditor'

export function SchedulePanel({ automation: a }: { automation: Automation }) {
  const patch = usePatchAutomation()
  return (
    <div className="space-y-3">
      <p className="text-sm text-ink-muted">
        Restrict when this automation runs (active-hours window) or preview a cron cadence.
        Outside its window the loop pauses and logs a "paused — outside scheduled active
        hours" entry in the Events tab.
      </p>
      <ScheduleEditor
        schedule={a.schedule ?? null}
        saving={patch.isPending}
        onSave={(schedule) => patch.mutate({ id: a.id, body: { schedule } })}
      />
    </div>
  )
}
