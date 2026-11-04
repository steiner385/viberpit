/**
 * The Context card's read-only viewer for one loaded memory file (D-13). Lazy:
 * the card imports it only when a row is opened, so neither it nor the shared
 * file-viewer frame is on the rail's path.
 *
 * The text comes from `GET /sessions/{id}/memory-file`, which reads it on the
 * session's own host and only for a path the CLI reported loading. The frame is
 * the Files tab's (`FileLines`). Nothing here edits: the user file — the only
 * editable one — links to Settings › Memory instead.
 */
import { Link } from 'react-router-dom'
import { useMemoryFile } from '@/api/hooks'
import type { MemoryFile } from '@/session/events'
import { settingsSectionPath } from '@/shell/navDefs'
import { cn } from '@/lib/utils'
import { Button, buttonVariants } from '@/ui/Button'
import { Dialog } from '@/ui/Dialog'
import { FileError, FileLines, TruncatedNote } from '../files/FileViewer'
import { MAX_MEMORY_BYTES } from '../files/types'
import { memoryTypeLabel } from './memoryFiles'

/** `MEMORY_FILE_UNKNOWN`: every viewer refusal, not only a missing file. */
const NOT_LOADED = 'This file is no longer loaded or no longer readable. It may have been moved, or the session reloaded its memory.'

export default function MemoryFileDialog({ sessionId, file, displayPath, onClose }: {
  sessionId: string
  file: MemoryFile
  displayPath: string
  onClose: () => void
}) {
  const { data, isError, error, refetch } = useMemoryFile(sessionId, file.path)
  const label = memoryTypeLabel(file.type)

  return (
    <Dialog
      open
      onClose={onClose}
      title={displayPath}
      description={`${label} memory · ${file.path}`}
      size="xl"
      // A path is one unbroken word: let the title and description wrap anywhere
      // rather than push a 390px dialog sideways. Code rows are `whitespace-pre`.
      className="[overflow-wrap:anywhere]"
    >
      <div className="flex flex-col gap-3">
        <div className="flex max-h-[60vh] min-h-0 flex-col overflow-hidden rounded-md border border-edge">
          {data ? (
            <>
              {data.truncated && <TruncatedNote limit={MAX_MEMORY_BYTES} />}
              <FileLines path={data.path} text={data.text} line={null} />
            </>
          ) : isError ? (
            <FileError error={error} onRetry={() => void refetch()} notFound={NOT_LOADED} />
          ) : (
            <p className="p-4 text-sm text-ink-muted" aria-busy="true">Loading file…</p>
          )}
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          {data?.editable && (
            <Link
              to={settingsSectionPath('memory')}
              onClick={onClose}
              className={cn(buttonVariants({ variant: 'secondary', size: 'lg' }), 'no-underline')}
            >
              Edit in Settings
            </Link>
          )}
          <Button type="button" variant="primary" size="lg" onClick={onClose}>Close</Button>
        </div>
      </div>
    </Dialog>
  )
}
