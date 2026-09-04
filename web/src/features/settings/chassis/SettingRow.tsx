import * as React from 'react'
import { cn } from '@/lib/utils'

interface SettingRowBase {
  htmlFor: string
  description?: React.ReactNode
  error?: React.ReactNode
  status?: React.ReactNode
  wide?: boolean
  children: React.ReactElement<{ 'aria-describedby'?: string }>
}

/**
 * How the row is labelled — a union, so a row can never compile with no label
 * at all (an unlabelled setting is the one shape that has no correct rendering).
 *
 * `selfLabelled` means the control already carries its own visible label:
 * `ui/Checkbox`, which wraps its input in a `<label>`. The row then renders its
 * text as plain text instead of a second `<label htmlFor>` — two labels
 * pointing at one control do not compete, they CONCATENATE ("Auto-archive
 * Auto-archive completed sessions"), which is axe's
 * `form-field-multiple-labels` and is what a screen reader reads out.
 *
 * R14b: `selfLabelled` is also required for a control that is a GROUP rather
 * than one field — a `RadioGroup` named by its own `legend`, or a read-only
 * value beside a reset button. `htmlFor` cannot name a group, so a `<label
 * htmlFor>` would dangle at an id no element carries and focus nothing when
 * clicked. With a VISIBLE legend the group's own legend is the row's label, so
 * the row passes no `label` of its own and prints the words once.
 *
 * `htmlFor` is required either way: it still derives the description/error ids.
 */
type SettingRowLabelling =
  | { label: string; selfLabelled?: boolean }
  | { label?: never; selfLabelled: true }

export type SettingRowProps = SettingRowBase & SettingRowLabelling

/**
 * Label · description · one control (Microsoft SettingsCard shape, C4). The
 * description is always visible — never tooltip-only (S24). The control's
 * `aria-describedby` is appended with the description and error ids, in that
 * order, preserving anything the caller already set (same rule as ui/Field).
 */
export function SettingRow({ label, htmlFor, description, error, status, wide = false, selfLabelled = false, children }: SettingRowProps) {
  const descId = description ? `${htmlFor}-desc` : undefined
  const errId = error ? `${htmlFor}-error` : undefined
  const own = children.props['aria-describedby']
  const describedBy = [own, descId, errId].filter(Boolean).join(' ') || undefined
  const control = describedBy ? React.cloneElement(children, { 'aria-describedby': describedBy }) : children

  return (
    <div className={cn('grid gap-3 py-3', wide ? 'grid-cols-1' : 'sm:grid-cols-[minmax(0,1fr)_minmax(200px,42%)] sm:items-center')}>
      <div className="min-w-0">
        {label && (selfLabelled
          ? <p className="text-sm font-medium text-ink">{label}</p>
          : <label htmlFor={htmlFor} className="block text-sm font-medium text-ink">{label}</label>)}
        {description && <p id={descId} className="mt-0.5 text-xs text-ink-muted">{description}</p>}
        {error && <p id={errId} role="alert" className="mt-0.5 text-xs text-danger"><span className="sr-only">Error: </span>{error}</p>}
      </div>
      <div className={cn('flex items-center gap-2', !wide && 'sm:justify-end')}>
        {/* Always mounted, even empty (R7): a live region added to the DOM at the
            same moment it gains text is not announced by most screen readers —
            they only report changes to regions they were already observing.
            `empty:sr-only` takes the empty span out of flow so it costs no flex
            gap next to the control. */}
        <span role="status" aria-live="polite" className="empty:sr-only text-xs text-success whitespace-nowrap">{status}</span>
        {control}
      </div>
    </div>
  )
}
