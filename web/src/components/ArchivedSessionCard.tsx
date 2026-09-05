// One archived session, as a card.
//
// This is the whole of what archived rows lost by having their own renderer: a
// rail, swipe actions, a keyboard-reachable open control, an overflow menu, and
// a relative timestamp that matches every other time in the app. The rail is
// deliberately MUTED rather than a status colour — an archived session has no
// status, and borrowing one would put a live-looking signal on a dead row.
//
// It shows nothing a live card shows that ArchivedSession has no field for: no
// status pill, model chip, host, PRs, sub-agents, quick replies or autopilot
// marker. See the design spec's D5.
import { Archive, ArchiveRestore, FolderGit2, Trash2 } from 'lucide-react'
import { cn } from '@/lib/utils'
import { formatRepoLabel } from '@/lib/repoLabel'
import { repoFromCwd } from '@/lib/repoFromCwd'
import { isValidTimestamp, relTime } from '@/lib/relativeTime'
import { useNow } from '@/hooks/useNow'
import type { ArchivedSession } from '@/api/types'
import { CardFrame } from './card/CardFrame'
import { CardMenu, Sep, menuItem } from './card/CardMenu'

/** An archived row's display name: its name, or a short id for the unnamed.
 *  Exported alongside the component (not split into a separate module) because
 *  the card is what renders the name — Task 9's list needs the same rule for a
 *  delete-dialog's names, and duplicating it there would be the parallel
 *  implementation this plan exists to avoid. This trips
 *  react-refresh/only-export-components; waived per the same precedent as
 *  card/CardMenu.tsx's `menuItem`/`actionBtn` (and ui/Button.tsx's
 *  `buttonVariants`) — one concept belongs in one file even when Fast Refresh
 *  would rather it didn't. */
// eslint-disable-next-line react-refresh/only-export-components
export function displayNameOf(s: ArchivedSession): string {
  return s.name || s.id.slice(0, 8)
}

interface ArchivedSessionCardProps {
  session: ArchivedSession
  selected?: boolean
  listMode?: boolean
  /** Open the read-only transcript. */
  onReview: (id: string) => void
  onUnarchive: (id: string) => void
  /** Destructive: the caller confirms it before mutating. */
  onDelete: (session: ArchivedSession) => void
  unarchiving?: boolean
}

export function ArchivedSessionCard({
  session, selected = false, listMode = false, onReview, onUnarchive, onDelete, unarchiving = false,
}: ArchivedSessionCardProps) {
  const name = displayNameOf(session)
  // Prefer the accurate repo field (GitHub nameWithOwner); fall back to the
  // cwd-derived name only for legacy/scratch rows that never recorded one.
  const repoLabel = formatRepoLabel(session.repo ?? repoFromCwd(session.cwd))
  // 60s, not the SessionCard default of 1s: an archived timestamp is days old
  // (see the "Archived Nd ago" label below), so the displayed text changes at
  // most once a day. ArchivedManager's QUICK_PURGE_COUNTS goes up to 200 rows
  // and neither this card nor CardFrame is memoized, so a per-second tick here
  // would be 200 needless re-renders/sec for a label that never visibly moves.
  const now = useNow(60_000)

  return (
    <CardFrame
      id={session.id}
      openLabel={`Review ${name}`}
      // Muted: an archived session has no status tier to colour.
      railClass="bg-neutral/40"
      selected={selected}
      listMode={listMode}
      onOpen={onReview}
      swipeEnabled={!unarchiving}
      swipeRight={{
        label: `Unarchive ${name}`,
        icon: <ArchiveRestore size={20} aria-hidden="true" />,
        onCommit: () => onUnarchive(session.id),
        testId: 'swipe-panel-unarchive',
        className: 'bg-blue-600 text-white',
      }}
      swipeLeft={{
        label: `Delete ${name}`,
        icon: <Trash2 size={20} aria-hidden="true" />,
        onCommit: () => onDelete(session),
        testId: 'swipe-panel-delete',
        className: 'bg-red-600 text-white',
      }}
      identity={
        <span className="min-w-0 truncate text-sm font-semibold text-gray-900 dark:text-gray-100">
          {name}
        </span>
      }
      trailing={
        <>
          <span
            data-testid="card-account"
            className="flex-shrink-0 max-w-[30%] truncate text-xs text-gray-500 dark:text-gray-400 bg-gray-100 dark:bg-gray-700 px-1.5 py-0.5 rounded"
            title={session.account}
          >
            {session.account}
          </span>
          <CardMenu label={`More actions for ${name}`}>
            {(close) => (
              <>
                <button
                  type="button"
                  role="menuitem"
                  onClick={() => { close(); onReview(session.id) }}
                  className={cn(menuItem, 'text-gray-700 dark:text-gray-200')}
                >
                  <Archive size={16} aria-hidden="true" />
                  Review
                </button>
                <button
                  type="button"
                  role="menuitem"
                  disabled={unarchiving}
                  onClick={() => { close(); onUnarchive(session.id) }}
                  className={cn(menuItem, 'text-gray-700 dark:text-gray-200 disabled:opacity-40')}
                >
                  <ArchiveRestore size={16} aria-hidden="true" />
                  {unarchiving ? 'Restoring…' : 'Unarchive'}
                </button>
                <button
                  type="button"
                  role="menuitem"
                  onClick={() => { close(); onDelete(session) }}
                  className={cn(menuItem, 'text-red-600 dark:text-red-400')}
                >
                  <Trash2 size={16} aria-hidden="true" />
                  Delete permanently
                </button>
              </>
            )}
          </CardMenu>
        </>
      }
    >
      <div data-testid="card-context" className="mt-0.5 flex items-center gap-1.5 min-w-0 text-xs">
        <span
          className={cn(
            'min-w-0 inline-flex items-center gap-1',
            repoLabel.isScratch
              ? 'text-gray-400 dark:text-gray-500 italic'
              : 'text-gray-600 dark:text-gray-300',
          )}
          title={repoLabel.text}
        >
          <FolderGit2 size={12} aria-hidden="true" className="flex-shrink-0" />
          <span className="truncate">{repoLabel.text}</span>
        </span>
        {isValidTimestamp(session.archivedAt) && (
          <>
            <Sep />
            <span
              data-testid="archived-at"
              className="ml-auto flex-shrink-0 text-gray-400 dark:text-gray-500 tabular-nums"
              title={`Archived ${new Date(session.archivedAt).toLocaleString()}`}
            >
              Archived {relTime(session.archivedAt, now || undefined)}
            </span>
          </>
        )}
      </div>
    </CardFrame>
  )
}
