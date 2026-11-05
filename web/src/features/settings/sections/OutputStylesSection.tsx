/**
 * Settings › Claude config › Output styles (D-16a): the style new sessions start
 * with (`defaultOutputStyle`), and the styles every account can use — the five
 * built-ins and the shared `~/.claude/output-styles/*.md` files, each with its
 * source. A user style's file opens read-only; editing a style is out of scope
 * here (an editor is a later slice), so the copy points at the directory.
 *
 * The list comes from the edge (`GET /settings/output-styles`), which has no live
 * session to ask: plugin styles are not offered as the default, though a running
 * session can pick one from its own `+` menu. Lazy (`settingsDefs`), like Memory.
 */
import { useId, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/api/client'
import { useSettings, useUpdateSettings } from '@/api/hooks'
import { extractMutationErrorMessage } from '@/api/queryClient'
import type { OutputStyleFile, OutputStyleInfo } from '@/api/types'
import { FileError, FileLines } from '@/features/sessions/files/FileViewer'
import { Badge } from '@/ui/Badge'
import { Button } from '@/ui/Button'
import { Dialog } from '@/ui/Dialog'
import { Field } from '@/ui/Field'
import { QueryFailureBanner } from '@/ui/QueryFailureBanner'
import { Select } from '@/ui/Select'
import { SaveBar } from '../chassis/SaveBar'
import { useUnsavedChangesGuard } from '../useUnsavedChangesGuard'

const DIR = '~/.claude/output-styles/'
/** The CLI's own style: a NULL default means it. */
const CLI_DEFAULT = 'default'

// The two reads only this section makes live here, in its lazy chunk, not in the
// eagerly loaded `api/hooks` (bundle budget, D-16a review).
const STYLES_KEY = ['output-styles'] as const

/** The styles the Settings default can be: the five built-ins in CLI order, then the
 *  shared `~/.claude/output-styles/*.md` files. */
function useOutputStyles() {
  return useQuery<{ styles: OutputStyleInfo[] }>({
    queryKey: STYLES_KEY,
    queryFn: () => api.get<{ styles: OutputStyleInfo[] }>('/settings/output-styles'),
  })
}

/** One shared user style's file for the read-only viewer. Any refusal (not a `.md`
 *  directly in the shared dir, gone) is a 404. */
function useOutputStyleFile(path: string) {
  return useQuery<OutputStyleFile>({
    queryKey: [...STYLES_KEY, 'file', path],
    queryFn: () => api.get<OutputStyleFile>(`/settings/output-styles/${encodeURIComponent(path)}`),
    retry: false,
  })
}

const SOURCE_LABEL: Record<OutputStyleInfo['source'], string> = { builtin: 'Built-in', user: 'User' }

export default function OutputStylesSection() {
  const styles = useOutputStyles()
  const settings = useSettings()
  if (styles.isError || settings.isError) {
    return (
      <QueryFailureBanner
        message="Couldn’t load the output styles."
        hasData={false}
        onRetry={() => { void styles.refetch(); void settings.refetch() }}
      />
    )
  }
  if (!styles.data || !settings.data) return <p className="text-sm text-ink-muted" aria-busy="true">Loading…</p>
  return (
    <OutputStylesBody
      styles={styles.data.styles}
      saved={settings.data.defaultOutputStyle ?? CLI_DEFAULT}
    />
  )
}

function OutputStylesBody({ styles, saved }: { styles: OutputStyleInfo[]; saved: string }) {
  const id = useId()
  const update = useUpdateSettings()
  const [draft, setDraft] = useState(saved)
  const [savedAt, setSavedAt] = useState<number | null>(null)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [viewing, setViewing] = useState<OutputStyleInfo | null>(null)
  const dirty = draft !== saved
  const blocker = useUnsavedChangesGuard(dirty)
  // A saved default the edge no longer lists (its file was removed) still shows, so
  // the picker never silently claims a different value than the server holds.
  const options = styles.some((s) => s.name === saved) ? styles : [...styles, { name: saved, source: 'user' as const, description: null }]

  const save = async (): Promise<boolean> => {
    setSaveError(null)
    try {
      await update.mutateAsync({ defaultOutputStyle: draft === CLI_DEFAULT ? null : draft })
      setSavedAt(Date.now())
      return true
    } catch (err) {
      setSaveError(extractMutationErrorMessage(err, 'Couldn’t save the default output style.'))
      return false
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <Field
        label="Default style"
        htmlFor={id}
        help="New sessions start with it. A running session keeps its style; change it from the session's + menu."
        error={saveError}
      >
        <Select id={id} value={draft} onChange={(e) => setDraft(e.target.value)} className="max-w-xs">
          {options.map((s) => <option key={s.name} value={s.name}>{s.name}</option>)}
        </Select>
      </Field>

      <section aria-labelledby={`${id}-list`} className="flex flex-col gap-2">
        <h3 id={`${id}-list`} className="text-sm font-semibold text-ink">Available styles</h3>
        <p className="text-sm text-ink-muted">
          Add your own as a Markdown file in <code className="font-mono [overflow-wrap:anywhere]">{DIR}</code> — every
          account sees it.
        </p>
        <ul className="flex flex-col divide-y divide-edge rounded-md border border-edge">
          {styles.map((s) => (
            <li key={s.name} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 min-h-target">
              <span className="font-medium text-ink [overflow-wrap:anywhere]">{s.name}</span>
              <Badge tone={s.source === 'user' ? 'accent' : 'neutral'}>{SOURCE_LABEL[s.source]}</Badge>
              {s.path && (
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  className="ml-auto"
                  aria-label={`View ${s.name}`}
                  onClick={() => setViewing(s)}
                >
                  View
                </Button>
              )}
              {s.description && <span className="basis-full text-sm text-ink-muted">{s.description}</span>}
            </li>
          ))}
        </ul>
      </section>

      {viewing?.path && <StyleFileDialog style={viewing} path={viewing.path} onClose={() => setViewing(null)} />}

      <SaveBar form={{
        dirty,
        saving: update.isPending,
        savedAt,
        save,
        discard: () => { setDraft(saved); setSaveError(null) },
        blocker,
      }} />
    </div>
  )
}

/** A user style's file, read-only — the Files tab's frame, as the memory viewer uses. */
function StyleFileDialog({ style, path, onClose }: { style: OutputStyleInfo; path: string; onClose: () => void }) {
  const { data, isError, error, refetch } = useOutputStyleFile(path)
  return (
    <Dialog
      open
      onClose={onClose}
      title={style.name}
      description={`User style · ${DIR}${path} · read-only`}
      size="xl"
      className="[overflow-wrap:anywhere]"
    >
      <div className="flex flex-col gap-3">
        <div className="flex max-h-[60vh] min-h-0 flex-col overflow-hidden rounded-md border border-edge">
          {data ? (
            <FileLines path={data.path} text={data.content} line={null} />
          ) : isError ? (
            <FileError error={error} onRetry={() => void refetch()} notFound="This style file is gone or can't be read here." />
          ) : (
            <p className="p-4 text-sm text-ink-muted" aria-busy="true">Loading file…</p>
          )}
        </div>
        <div className="flex justify-end">
          <Button type="button" variant="primary" size="lg" onClick={onClose}>Close</Button>
        </div>
      </div>
    </Dialog>
  )
}
