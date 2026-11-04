/**
 * Settings › Claude config › Memory (D-13): the editor for the shared user
 * memory file, `~/.claude/CLAUDE.md` on the edge host, which every account
 * loads as its user memory. Lazy (`settingsDefs`), so the editor is not in the
 * Settings page's chunk.
 *
 * Saves are optimistic-concurrency: the PUT carries the etag the draft was
 * based on (`""` for a file that does not exist yet, which creates it). A file
 * changed elsewhere since is a 409 `MEMORY_CONFLICT` carrying the current
 * `{text, etag}`; the section shows that text beside the untouched draft until
 * the user chooses Reload (take theirs) or Keep my draft (the next Save
 * overwrites theirs, knowingly). A file that cannot be edited safely here — a
 * link, over the size cap, not UTF-8 — is explained, with no editor.
 *
 * The footer is the Settings `SaveBar`, and a dirty draft is guarded against
 * navigation by the same `useUnsavedChangesGuard` the form sections use.
 */
import { useId, useState } from 'react'
import { useSaveUserMemory, useUserMemory } from '@/api/hooks'
import { ApiRequestError, apiErrorCode } from '@/api/client'
import { extractMutationErrorMessage } from '@/api/queryClient'
import type { UserMemory } from '@/api/types'
import { MAX_MEMORY_BYTES, kib } from '@/features/sessions/files/types'
import { Banner } from '@/ui/Banner'
import { Button } from '@/ui/Button'
import { Field } from '@/ui/Field'
import { QueryFailureBanner } from '@/ui/QueryFailureBanner'
import { Textarea } from '@/ui/Textarea'
import { SaveBar } from '../chassis/SaveBar'
import { useUnsavedChangesGuard } from '../useUnsavedChangesGuard'

const FILE = '~/.claude/CLAUDE.md'
const CAP = kib(MAX_MEMORY_BYTES)

/** Verbatim (D-13 spec): a running session read its memory at start. */
const APPLIES_COPY = 'Applies to new sessions. Running sessions keep the instructions they started with.'

/** Why the file cannot be edited here, per refusal code; shared by the read and the save. */
const REFUSAL: Record<string, string> = {
  MEMORY_FILE_IS_LINK: `${FILE} is a symbolic link, so it is not edited here. Edit the file it points to on the host.`,
  MEMORY_TOO_LARGE: `${FILE} is larger than ${CAP}, too large to edit here. Edit it on the host.`,
  MEMORY_NOT_UTF8: `${FILE} is not valid UTF-8 text, so it is not edited here: saving would rewrite its bytes. Edit it on the host.`,
}
const SAVE_TOO_LARGE = `The text is larger than ${CAP}, the most the user memory file can hold. Nothing was saved.`

export default function MemorySection() {
  const { data, error, isError, isFetchedAfterMount, refetch } = useUserMemory()
  // Seeded only from a read made for THIS visit: a cached copy from an earlier
  // one may predate an edit made elsewhere, and its etag would turn the first
  // save into a needless conflict.
  if (isFetchedAfterMount && isError) {
    const refusal = REFUSAL[apiErrorCode(error) ?? '']
    if (refusal) return <p className="text-sm text-ink-muted">{refusal}</p>
    return <QueryFailureBanner message="Couldn’t load the user memory file." hasData={false} onRetry={() => void refetch()} />
  }
  if (isFetchedAfterMount && data) return <MemoryEditor initial={data} />
  return <p className="text-sm text-ink-muted" aria-busy="true">Loading…</p>
}

interface Snapshot { text: string; etag: string }

/** The current file a 409 `MEMORY_CONFLICT` carries in its detail. */
function conflictSnapshot(err: unknown): Snapshot | null {
  if (!(err instanceof ApiRequestError) || apiErrorCode(err) !== 'MEMORY_CONFLICT') return null
  const d = (err.body as { detail?: { text?: unknown; etag?: unknown } }).detail
  return typeof d?.text === 'string' && typeof d.etag === 'string' ? { text: d.text, etag: d.etag } : null
}

function saveErrorMessage(err: unknown): string {
  const code = apiErrorCode(err)
  if (code === 'MEMORY_TOO_LARGE') return SAVE_TOO_LARGE
  return REFUSAL[code ?? ''] ?? extractMutationErrorMessage(err, 'Couldn’t save the user memory file.')
}

/**
 * Mounted once the file has loaded, and seeded from it once: the draft's base
 * (`base`) moves only on a save, or on Reload / Keep my draft after a conflict —
 * never under the user from a background refetch.
 */
function MemoryEditor({ initial }: { initial: UserMemory }) {
  const id = useId()
  const save = useSaveUserMemory()
  const [base, setBase] = useState<Snapshot>({ text: initial.text, etag: initial.etag })
  const [exists, setExists] = useState(initial.exists)
  const [draft, setDraft] = useState(initial.text)
  const [conflict, setConflict] = useState<Snapshot | null>(null)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [propagation, setPropagation] = useState<string | null>(null)
  const [savedAt, setSavedAt] = useState<number | null>(null)

  const dirty = draft !== base.text || (!exists && draft !== '')
  const blocker = useUnsavedChangesGuard(dirty)

  const onSave = async (): Promise<boolean> => {
    const text = draft
    setSaveError(null)
    setPropagation(null)
    try {
      const out = await save.mutateAsync({ text, etag: base.etag })
      setBase({ text, etag: out.etag })
      setExists(true)
      setConflict(null)
      setPropagation(out.propagation ?? null)
      setSavedAt(Date.now())
      return true
    } catch (err) {
      const current = conflictSnapshot(err)
      if (current) setConflict(current)
      else setSaveError(saveErrorMessage(err))
      return false
    }
  }

  /** Adopt the file as it now is; `""` is the etag of a file deleted elsewhere. */
  const adopt = (current: Snapshot) => {
    setBase(current)
    setExists(current.etag !== '')
    setConflict(null)
    setSaveError(null)
  }
  const onReload = () => {
    if (!conflict) return
    adopt(conflict)
    setDraft(conflict.text)
  }
  const onKeepDraft = () => { if (conflict) adopt(conflict) }

  return (
    <div className="flex flex-col gap-4">
      {!exists && (
        <p className="text-sm text-ink-muted">No user memory file yet. Saving creates {FILE}.</p>
      )}
      {conflict && (
        <Banner
          tone="warning"
          title="Changed elsewhere"
          actions={(
            <>
              <Button size="sm" variant="secondary" onClick={onReload}>Reload</Button>
              <Button size="sm" variant="ghost" onClick={onKeepDraft}>Keep my draft</Button>
            </>
          )}
        >
          <p className="text-sm">
            {FILE} changed since you opened it. Reload replaces your draft with the current file;
            Keep my draft keeps it, and the next Save overwrites the other change.
          </p>
          <div
            role="region"
            aria-label="Current file"
            tabIndex={0}
            className="mt-2 max-h-48 overflow-auto rounded-md border border-edge bg-canvas p-2 font-mono text-xs whitespace-pre-wrap [overflow-wrap:anywhere]"
          >
            {conflict.text}
          </div>
        </Banner>
      )}
      <Field label="User memory (CLAUDE.md)" htmlFor={id} help={APPLIES_COPY} error={saveError}>
        <Textarea
          id={id}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          rows={18}
          spellCheck={false}
          className="font-mono"
        />
      </Field>
      {propagation && !dirty && (
        <p role="note" className="text-sm text-ink-muted"><span>{propagation}</span></p>
      )}
      <SaveBar form={{
        dirty,
        saving: save.isPending,
        savedAt,
        save: onSave,
        discard: () => { setDraft(base.text); setSaveError(null) },
        blocker,
      }} />
    </div>
  )
}
