// Tick a manual condition (spec 2026-11-05 §9 actions, §10 PUT): Met / Not met for
// a gate condition, a number for a numbers-panel entry. Evidence is required (the
// server refuses a tick without it); a link is optional. Who ticked it comes from
// the request's identity, never from the form.
import { useId, useState } from 'react'
import type { MilestoneCondition } from '@/api/milestones'
import { NOTE_TEXT_MAX, NUMBERS_SCOPE, milestoneErrorText, useTickCondition } from '@/api/milestones'
import { Button } from '@/ui/Button'
import { Dialog } from '@/ui/Dialog'
import { Field } from '@/ui/Field'
import { Input } from '@/ui/Input'
import { Select } from '@/ui/Select'
import { Textarea } from '@/ui/Textarea'

type Met = 'met' | 'unmet'

function storedMet(c: MilestoneCondition): Met | null {
  const met = c.value && (c.value as Record<string, unknown>).met
  return met === true ? 'met' : met === false ? 'unmet' : null
}

function storedNumber(c: MilestoneCondition): string {
  const n = c.value && (c.value as Record<string, unknown>).n
  return typeof n === 'number' ? String(n) : ''
}

export function TickDialog({ open, onClose, slug, scope, condition: c }: {
  open: boolean
  onClose: () => void
  slug: string
  /** The milestone key, or `NUMBERS_SCOPE` for a numbers-panel entry. */
  scope: string
  condition: MilestoneCondition
}) {
  const tick = useTickCondition(slug)
  const id = useId()
  const isNumber = scope === NUMBERS_SCOPE
  const [met, setMet] = useState<Met | null>(() => storedMet(c))
  const [value, setValue] = useState(() => storedNumber(c))
  const [evidence, setEvidence] = useState(c.manualEvidence ?? '')
  // The stored link, so an Update that only edits the evidence keeps it.
  const [url, setUrl] = useState(c.manualUrl ?? '')

  const number = value.trim() === '' ? NaN : Number(value)
  const reading = isNumber ? Number.isFinite(number) : met !== null
  const ready = reading && evidence.trim() !== ''
  const error = milestoneErrorText(tick.error)

  const close = () => {
    tick.reset()
    onClose()
  }

  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!ready) return
    tick.mutate(
      {
        scope,
        conditionKey: c.key,
        ...(isNumber ? { value: number } : { met: met === 'met' }),
        evidence: evidence.trim(),
        url: url.trim() || null,
      },
      { onSuccess: close },
    )
  }

  return (
    <Dialog open={open} onClose={close} title="Tick condition" description={c.label}>
      <form onSubmit={submit} className="flex flex-col gap-4">
        {isNumber
          ? (
              <Field label="Value" htmlFor={`${id}-value`} required>
                <Input id={`${id}-value`} type="number" inputMode="decimal" step="any" value={value}
                  onChange={(e) => setValue(e.target.value)} />
              </Field>
            )
          : (
              // A Select, not RadioGroup: RadioGroup shares a startup chunk, and
              // importing it here splits that chunk onto the critical path (bundle budget).
              <Field label="Reading" htmlFor={`${id}-met`} required>
                <Select id={`${id}-met`} value={met ?? ''} onChange={(e) => setMet((e.target.value || null) as Met | null)}>
                  <option value="">Choose…</option>
                  <option value="met">Met</option>
                  <option value="unmet">Not met</option>
                </Select>
              </Field>
            )}
        <Field label="Evidence" htmlFor={`${id}-evidence`} required help="What shows this is true: a note, a date, who confirmed it.">
          <Textarea id={`${id}-evidence`} rows={3} maxLength={NOTE_TEXT_MAX} value={evidence} onChange={(e) => setEvidence(e.target.value)} />
        </Field>
        <Field label="Link (optional)" htmlFor={`${id}-url`}>
          <Input id={`${id}-url`} type="url" inputMode="url" placeholder="https://" value={url}
            onChange={(e) => setUrl(e.target.value)} />
        </Field>
        {error && <p role="alert" className="text-sm text-danger">{error}</p>}
        <div className="flex flex-wrap justify-end gap-2">
          <Button type="button" variant="secondary" size="lg" onClick={close}>Cancel</Button>
          <Button type="submit" size="lg" disabled={!ready} loading={tick.isPending}>Save tick</Button>
        </div>
      </form>
    </Dialog>
  )
}
