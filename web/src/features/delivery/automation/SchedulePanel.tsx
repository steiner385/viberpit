/**
 * The Schedule tab: active-hours / cron editor for one automation. Moved
 * verbatim (behaviour) from `AutomationCard.tsx`'s `SchedulePanel` — the
 * editor itself (`ScheduleEditor`) already lives on tokens/`ui/Button` and is
 * untouched.
 */
import type { Automation } from '@/api/automations'
import { usePatchAutomation } from '@/api/hooks'
import { ScheduleEditor } from '@/components/ScheduleEditor'

export function SchedulePanel({ automation: a }: { automation: Automation }) {
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
