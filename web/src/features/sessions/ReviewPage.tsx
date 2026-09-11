/**
 * ArchivedReviewPage — an archived session's transcript as a Reader page
 * (finish-line row 6; page-migration §2.2).
 *
 * Fetches a bounded tail of the transcript via `useSessionTranscript`, folds
 * the events through `transcriptReducer` into turns, and renders them with
 * `TranscriptView` — no composer, no WS connection. Scrolling to the top pages
 * in older turns (mirrors `useSessionRuntime`'s onLoadOlder, self-contained
 * over plain HTTP). The shell hides `app.dock` behind this route
 * (`readerRouteForPath`), and `PageContent scroll={false}` because the
 * virtualizer inside `TranscriptView` owns the scroll region.
 *
 * The title is the session's name from the archived list (the transcript
 * payload carries none); until the list arrives — or if the row is gone, as
 * after a permanent delete with a stale link — the id's first 8 characters.
 */
import { useReducer, useEffect, useRef, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { Archive } from 'lucide-react'
import { transcriptReducer, initialTranscript } from '@/session/reducer'
import { useSessionTranscript, useUnarchiveSession, useArchivedSessions } from '@/api/hooks'
import { fetchOlderTurns } from '@/api/transcript'
import { isValidTimestamp } from '@/lib/relativeTime'
import { ARCHIVED_PATH } from '@/session/sessionNavState'
import { TranscriptView } from '@/components/session/TranscriptView'
import { PageHeader } from '@/ui/PageHeader'
import { PageContent } from '@/ui/PageContent'
import { Skeleton } from '@/ui/Skeleton'
import { EmptyState } from '@/ui/EmptyState'
import { Button } from '@/ui/Button'

/** Initial payload cap: the last N turns; older history pages in on scroll-up. */
const INITIAL_TURN_LIMIT = 100

export function ArchivedReviewPage({ sessionId }: { sessionId: string }) {
  const navigate = useNavigate()
  const { data, isLoading } = useSessionTranscript(sessionId, true, INITIAL_TURN_LIMIT)
  const [state, dispatch] = useReducer(transcriptReducer, initialTranscript)
  const unarchive = useUnarchiveSession()
  const record = useArchivedSessions().data?.find((s) => s.id === sessionId)
  const title = record?.name || sessionId.slice(0, 8)

  // Fold every transcript event into turns once the data arrives, then close
  // the replay: replayComplete stores earliestTurn/totalTurns (which drive the
  // scroll-up paging trigger) and clears the replaying flag.
  useEffect(() => {
    if (!data?.events) return
    for (const ev of data.events) dispatch(ev)
    dispatch({ type: 'replayComplete', earliestTurn: data.earliestTurn, totalTurns: data.totalTurns })
  }, [data])

  // Scroll-up paging — mirrors useSessionRuntime.onLoadOlder.
  const loadingOlderRef = useRef(false)
  const onLoadOlder = useCallback(() => {
    if (loadingOlderRef.current) return
    if (state.earliestTurn <= 0) return // already at session start
    loadingOlderRef.current = true
    dispatch({ type: '_setLoadingOlder', value: true })
    fetchOlderTurns(sessionId, state.earliestTurn)
      .then((res) => dispatch({ type: '_prependHistory', events: res.events, earliestTurn: res.earliestTurn }))
      .catch(() => dispatch({ type: '_setLoadingOlder', value: false }))
      .finally(() => { loadingOlderRef.current = false })
  }, [sessionId, state.earliestTurn])

  // `archivedAt` is typed as a string but the server sends null for a row with
  // no archive time; the sibling card guards it the same way.
  const archivedOn = isValidTimestamp(record?.archivedAt) ? new Date(record.archivedAt).toLocaleDateString() : null
  const meta = [archivedOn ? `Archived ${archivedOn}` : 'Archived', 'read-only', record?.repo ?? null]
    .filter(Boolean).join(' · ')

  return (
    <div className="flex flex-col h-full min-h-0" data-page-variant="reader">
      <PageHeader
        title={title}
        back={{ to: ARCHIVED_PATH, label: 'Back to archived sessions' }}
        meta={meta}
        actions={
          <Button
            variant="secondary"
            size="sm"
            disabled={unarchive.isPending}
            onClick={() => unarchive.mutate(sessionId, { onSuccess: () => navigate(ARCHIVED_PATH) })}
            aria-label="Restore this session from archive"
          >
            {unarchive.isPending ? 'Restoring…' : 'Unarchive'}
          </Button>
        }
      />
      <PageContent scroll={false} busy={isLoading}>
        {isLoading ? (
          <Skeleton lines={8} className="px-4 py-4 md:px-6" />
        ) : state.turns.length === 0 ? (
          <EmptyState icon={<Archive size={32} />} title="No transcript available for this session." />
        ) : (
          <TranscriptView
            turns={state.turns}
            toolDetailMode="auto"
            thinkingCollapsed={true}
            onAnswer={() => { /* read-only */ }}
            earliestTurn={state.earliestTurn}
            loadingOlder={state.loadingOlder}
            onLoadOlder={onLoadOlder}
          />
        )}
      </PageContent>
    </div>
  )
}
