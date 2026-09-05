import { Button } from '@/ui/Button'
import { Dialog } from '@/ui/Dialog'
import type { SettingsForm } from '../useSettingsForm'

export interface SaveBarProps {
  form: Pick<SettingsForm<object>, 'dirty' | 'saving' | 'savedAt' | 'save' | 'discard' | 'blocker'>
}

function savedLabel(savedAt: number | null): string {
  if (savedAt === null) return ''
  const s = Math.round((Date.now() - savedAt) / 1000)
  return s < 5 ? 'Saved · just now' : s < 60 ? `Saved · ${s}s ago` : 'Saved'
}

/**
 * Sticky Save / Discard footer for form sections (spec §7.2). `role="status"`
 * announces dirty and saved states (WCAG 4.1.3). The parent scroll container
 * sets `scroll-padding-bottom` so a focused field is never hidden under it
 * (WCAG 2.4.11) — see SettingsPage.
 */
export function SaveBar({ form }: SaveBarProps) {
  const { dirty, saving, savedAt, save, discard, blocker } = form
  const status = dirty ? 'Unsaved changes' : savedLabel(savedAt)
  return (
    <>
      <div className="sticky bottom-0 z-sticky -mx-4 md:-mx-6 mt-2 flex items-center justify-between gap-3 border-t border-edge bg-surface px-4 py-3 md:px-6">
        <span role="status" aria-live="polite" className={dirty ? 'text-sm text-warning' : 'text-sm text-success'}>{status}</span>
        <div className="flex items-center gap-2">
          <Button type="button" variant="ghost" onClick={discard} disabled={!dirty || saving}>Discard</Button>
          <Button type="button" variant="primary" onClick={() => { void save() }} disabled={!dirty || saving} loading={saving}>Save</Button>
        </div>
      </div>
      <Dialog
        open={blocker.state === 'blocked'}
        onClose={() => blocker.reset?.()}
        title="Unsaved changes"
        description="You have changes that have not been saved."
        size="sm"
      >
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={() => blocker.reset?.()}>Keep editing</Button>
          <Button type="button" variant="danger" onClick={() => { discard(); blocker.proceed?.() }}>Discard and leave</Button>
        </div>
      </Dialog>
    </>
  )
}
