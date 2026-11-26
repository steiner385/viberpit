// web/src/features/sessions/remote/NameMachineDialog.tsx
//
// Name a Remote Control machine ViberPit does not run on. claude.ai tags a remote
// session with an opaque machine id only; a ViberPit host is named automatically, any
// other machine by the label stored here (Settings `remoteMachineLabels`). Lives in the
// lazy remote chunk with the section that opens it.
import { useId, useRef, useState } from 'react'
import { useNameRemoteMachine } from '@/api/remoteSessionsHooks'
import { Button } from '@/ui/Button'
import { Dialog } from '@/ui/Dialog'
import { Field } from '@/ui/Field'
import { Input } from '@/ui/Input'

/** The server's ceiling (`server/remote_sessions/machines.py` MAX_LABEL_CHARS). */
export const MACHINE_LABEL_MAX = 64

export function NameMachineDialog({ machineId, onClose }: { machineId: string; onClose: () => void }) {
  const name = useNameRemoteMachine()
  const id = useId()
  const [label, setLabel] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)
  const ready = label.trim() !== '' && !name.isPending

  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!ready) return
    name.mutate({ machineId, label: label.trim() }, { onSuccess: onClose })
  }

  return (
    <Dialog
      open
      onClose={onClose}
      title="Name this machine"
      description={`Remote Control machine ${machineId.slice(0, 8)}. The name shows on every session it runs.`}
      size="sm"
      initialFocus={inputRef}
    >
      <form onSubmit={submit} className="flex flex-col gap-4">
        <Field label="Name" htmlFor={`${id}-label`} required
          error={name.error ? 'Could not save the name. Try again.' : undefined}>
          <Input id={`${id}-label`} value={label} ref={inputRef} maxLength={MACHINE_LABEL_MAX}
            placeholder="e.g. Work laptop" onChange={(e) => setLabel(e.target.value)} />
        </Field>
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" size="lg" onClick={onClose}>Cancel</Button>
          <Button type="submit" size="lg" disabled={!ready}>Save</Button>
        </div>
      </form>
    </Dialog>
  )
}
