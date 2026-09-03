/**
 * RenameDialog — the session-rename confirm dialog.
 *
 * Lifted verbatim out of `components/Dashboard.tsx` (Task 17). It has always had
 * three call sites (the phone dashboard, `DesktopShell` and `SessionActions`);
 * two of those shells are deleted in the same change, and `AppShell` +
 * `SessionActions` now import it from here instead of from a shell module.
 */
import * as React from 'react'
import { useState, useRef } from 'react'
import { Dialog } from '@/ui/Dialog'
import { Button } from '@/ui/Button'
import { Input } from '@/ui/Input'

interface RenameDialogProps {
  sessionId: string
  currentName: string
  onClose: () => void
  onSubmit: (id: string, name: string) => void
  isPending: boolean
}

export function RenameDialog({ sessionId, currentName, onClose, onSubmit, isPending }: RenameDialogProps) {
  const [name, setName] = useState(currentName)
  const inputRef = useRef<HTMLInputElement>(null)

  // Select the pre-filled name so typing immediately overwrites it. Dialog's
  // initialFocus only focuses the ref; the extra `.select()` is this dialog's
  // own behavior, layered on top.
  React.useEffect(() => {
    inputRef.current?.select()
  }, [])

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (name.trim()) onSubmit(sessionId, name.trim())
  }

  return (
    <Dialog open onClose={onClose} title="Rename Session" size="sm" initialFocus={inputRef}>
      <form onSubmit={handleSubmit} className="space-y-4">
        <Input
          ref={inputRef}
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          aria-label="Session name"
        />
        <div className="flex gap-3 justify-end">
          <Button type="button" variant="secondary" onClick={onClose} disabled={isPending}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" size="lg" disabled={!name.trim()} loading={isPending}>
            {isPending ? 'Saving…' : 'Save'}
          </Button>
        </div>
      </form>
    </Dialog>
  )
}
