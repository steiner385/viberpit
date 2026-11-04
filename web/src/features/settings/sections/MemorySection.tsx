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
 * the user chooses Reload. A file that cannot be edited safely here — a link,
 * over the size cap, not UTF-8 — is explained, with no editor.
 *
 * It does not import `Banner`/`QueryFailureBanner`: those live in a preloaded
 * shared chunk, and a lazy importer splits that chunk (+0.6 kB gzip on the
 * initial load, over the budget). The conflict notice uses the warning tokens.
 */
import { useId, useState } from 'react'
import { useSaveUserMemory, useUserMemory } from '@/api/hooks'
import { ApiRequestError, apiErrorCode } from '@/api/client'
import { extractMutationErrorMessage } from '@/api/queryClient'
import type { UserMemory } from '@/api/types'
import { Button } from '@/ui/Button'
import { Field } from '@/ui/Field'
import { Textarea } from '@/ui/Textarea'

const FILE = '~/.claude/CLAUDE.md'

/** Verbatim (D-13 spec): a running session read its memory at start. */
const APPLIES_COPY = 'Applies to new sessions. Running sessions keep the instructions they started with.'

/** Why the file cannot be edited here, per refusal code; shared by the read and the save. */
const REFUSAL: Record<string, string> = {
  MEMORY_FILE_IS_LINK: `${FILE} is a symbolic link, so it is not edited here. Edit the file it points to on the host.`,
  MEMORY_TOO_LARGE: `${FILE} is larger than 256 kB, too large to edit here. Edit it on the host.`,
  MEMORY_NOT_UTF8: `${FILE} is not valid UTF-8 text, so it is not edited here: saving would rewrite its bytes. Edit it on the host.`,
}
const SAVE_TOO_LARGE = 'The text is larger than 256 kB, the most the user memory file can hold. Nothing was saved.'

export default function MemorySection() {
  const { data, error, isError, refetch } = useUserMemory()
  if (data) return <MemoryEditor initial={data} />
  if (isError) {
    const refusal = REFUSAL[apiErrorCode(error) ?? '']
    if (refusal) return <p className="text-sm text-ink-muted">{refusal}</p>
    return (
      <div role="alert" className="flex flex-wrap items-center gap-3 text-sm text-danger">
        Couldn’t load the user memory file.
        <Button size="sm" variant="secondary" onClick={() => void refetch()}>Retry</Button>
      </div>
    )
  }
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
 * (`base`) moves only on a save, or on Reload after a conflict — never under the
 * user from a background refetch.
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

  const dirty = draft !== base.text || (!exists && draft !== '')

  const onSave = () => {
    const text = draft
    setSaveError(null)
    setPropagation(null)
    save.mutate({ text, etag: base.etag }, {
      onSuccess: (out) => {
        setBase({ text, etag: out.etag })
        setExists(true)
        setConflict(null)
        setPropagation(out.propagation ?? null)
      },
      onError: (err) => {
        const current = conflictSnapshot(err)
        if (current) setConflict(current)
        else setSaveError(saveErrorMessage(err))
      },
    })
  }

  const onReload = () => {
    if (!conflict) return
    setBase(conflict)
    setDraft(conflict.text)
    setExists(true)
    setConflict(null)
    setSaveError(null)
  }

  return (
    <div className="flex flex-col gap-4">
      {!exists && (
        <p className="text-sm text-ink-muted">No user memory file yet. Saving creates {FILE}.</p>
      )}
      {conflict && (
        <div role="status" className="flex flex-col gap-2 rounded-lg border border-warning/40 bg-warning-wash px-4 py-3 text-ink">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="font-medium">Changed elsewhere</p>
            <Button size="sm" variant="secondary" onClick={onReload}>Reload</Button>
          </div>
          <p className="text-sm">
            {FILE} changed since you opened it. Your draft is kept below; Reload replaces it with the current file.
          </p>
          <div
            role="region"
            aria-label="Current file"
            tabIndex={0}
            className="max-h-48 overflow-auto rounded-md border border-edge bg-canvas p-2 font-mono text-xs whitespace-pre-wrap [overflow-wrap:anywhere]"
          >
            {conflict.text}
          </div>
        </div>
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
      <div className="flex flex-wrap items-center gap-3">
        {propagation && !dirty && (
          <p role="status" className="text-sm text-ink-muted">Saved. <span>{propagation}</span></p>
        )}
        <Button type="button" variant="primary" size="lg" className="ml-auto" onClick={onSave} disabled={!dirty} loading={save.isPending}>
          Save
        </Button>
      </div>
    </div>
  )
}
