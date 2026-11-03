/**
 * The "Rewind to here" confirm (D-9), lazy-loaded by `useRewind`. It runs the dry run
 * on mount and lists what the rewind restores — `+insertions −deletions` and the files,
 * deletions marked — above the caveat line. A prompt with no file checkpoint says so
 * and rewinds the conversation only. Confirm rewinds; with `edit`, the cut prompt goes
 * back in the composer. Every outcome settles `request.done` once. Built on the shared
 * `ConfirmDialog`, so it brings no new primitive onto the page.
 */
import { useEffect, useRef, useState } from 'react'
import type { RewindPreview } from '@/api/hooks'
import { ConfirmDialog } from '@/ui/ConfirmDialog'
import * as copy from './rewindCopy'
import type { RewindRequest } from './useRewind'

export default function RewindConfirmDialog({ request }: { request: RewindRequest }) {
  const { toTurnId, edit, preview: runPreview, rewind, setDraft, fail, done } = request
  const [preview, setPreview] = useState<RewindPreview | null>(null)
  const working = useRef(false)

  useEffect(() => {
    let live = true
    runPreview(toTurnId).then(
      (p) => { if (live) setPreview(p) },
      (err) => { if (live) { fail(err); done(null) } },
    )
    return () => { live = false }
  }, [runPreview, toTurnId, fail, done])

  async function confirm() {
    if (!preview) return
    working.current = true
    try {
      const result = await rewind(toTurnId, preview.checkpointAvailable ? 'files_and_conversation' : 'conversation')
      if (edit && result.prefill !== null) setDraft(result.prefill)
      done(result)
    } catch (err) {
      fail(err)
      done(null)
    }
  }

  const deleted = new Set(preview?.deleted)
  // Spans, not a list: ConfirmDialog renders its description inside a <p>.
  const description = !preview ? copy.REWIND_LOADING : !preview.checkpointAvailable ? copy.REWIND_CONVERSATION_ONLY : (
    <>
      {preview.files.length === 0 ? <span className="block">{copy.REWIND_NO_FILES}</span> : (
        <span className="block font-mono text-xs">
          <span className="text-success">+{preview.insertions}</span>{' '}
          <span className="text-danger">−{preview.deletions}</span>
          <span role="list" aria-label="Files restored" className="mt-1 block max-h-48 overflow-y-auto text-ink">
            {preview.files.map((f) => (
              <span role="listitem" key={f} className="block break-all">
                {f}{deleted.has(f) && <span className="ml-2 text-danger">(deleted)</span>}
              </span>
            ))}
          </span>
        </span>
      )}
      <span className="mt-2 block">{copy.REWIND_CAVEAT}</span>
    </>
  )
  return (
    <ConfirmDialog
      open
      onClose={() => { if (!working.current) done(null) }}
      onConfirm={confirm}
      title={edit ? copy.EDIT_TITLE : copy.REWIND_TITLE}
      description={description}
      confirmLabel={edit ? copy.EDIT_CONFIRM : copy.REWIND_CONFIRM}
      tone="warning"
      busy={!preview}
    />
  )
}
