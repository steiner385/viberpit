// Advance the ladder one rung (spec 2026-11-05 §7, §9 actions). A met gate
// advances on a confirm. An unmet one is an owner override: the dialog lists what
// is unmet or unknown (the server snapshots the same list onto the exited card)
// and needs a reason. The server owns both rules; this only mirrors the minimum
// length so the button says when it can be pressed. The mutation is the page's
// (`MilestonesPage`): a 409 `stale_current` refetches the view, the card stops
// being current and this dialog unmounts, so the page says what happened.
import { useId, useState } from 'react'
import type { AdvanceMutation, Milestone } from '@/api/milestones'
import { OVERRIDE_REASON_MIN, milestoneErrorText } from '@/api/milestones'
import { Button } from '@/ui/Button'
import { Dialog } from '@/ui/Dialog'
import { Field } from '@/ui/Field'
import { Textarea } from '@/ui/Textarea'
import { STATUS_META, conditionValue, milestoneTitle } from './format'

export function AdvanceDialog({ open, onClose, advance, milestone: m, next }: {
  open: boolean
  onClose: () => void
  advance: AdvanceMutation
  /** The current milestone. */
  milestone: Milestone
  /** The rung it advances to, or null when this completes the ladder. */
  next: Milestone | null
}) {
  const reasonId = useId()
  const [reason, setReason] = useState('')
  const override = !m.gateSummary.ready
  const open_ = m.gate.filter((c) => c.status !== 'met')
  const trimmed = reason.trim()
  const ready = !override || trimmed.length >= OVERRIDE_REASON_MIN
  const error = milestoneErrorText(advance.error)
  const title = override ? 'Advance with override' : 'Advance'

  const close = () => {
    advance.reset()
    onClose()
  }

  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!ready) return
    advance.mutate(
      override ? { expectedCurrent: m.key, overrideReason: trimmed } : { expectedCurrent: m.key },
      { onSuccess: close },
    )
  }

  return (
    <Dialog open={open} onClose={close} title={title}>
      <form onSubmit={submit} className="flex flex-col gap-4">
        <p className="text-sm text-ink">
          {next
            ? <>From <strong>{milestoneTitle(m)}</strong> to <strong>{milestoneTitle(next)}</strong>.</>
            : <>Exit <strong>{milestoneTitle(m)}</strong>. This completes the ladder.</>}
        </p>
        {override && open_.length > 0 && (
          <div className="flex flex-col gap-1">
            <p id={`${reasonId}-list`} className="text-sm font-medium text-ink">Not met yet</p>
            <ul aria-labelledby={`${reasonId}-list`} className="flex flex-col gap-1 text-sm">
              {open_.map((c) => {
                const value = conditionValue(c)
                return (
                  <li key={c.key} className="rounded-md border border-edge px-3 py-2">
                    <span className="text-ink">{c.label}</span>
                    <span className="block text-xs text-ink-muted">
                      {(STATUS_META[c.status] ?? STATUS_META.unknown).text}{value ? ` · ${value}` : ''}
                    </span>
                  </li>
                )
              })}
            </ul>
          </div>
        )}
        {override && (
          <Field
            label="Reason"
            htmlFor={reasonId}
            required
            help={`At least ${OVERRIDE_REASON_MIN} characters. It is recorded on the exited milestone with what was unmet.`}
          >
            <Textarea id={reasonId} rows={3} value={reason} onChange={(e) => setReason(e.target.value)} />
          </Field>
        )}
        {error && <p role="alert" className="text-sm text-danger">{error}</p>}
        <div className="flex flex-wrap justify-end gap-2">
          <Button type="button" variant="secondary" size="lg" onClick={close}>Cancel</Button>
          <Button type="submit" size="lg" disabled={!ready} loading={advance.isPending}>{title}</Button>
        </div>
      </form>
    </Dialog>
  )
}
