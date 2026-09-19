/**
 * The Inbox scope of the Roadmap list column (finish-line §4.3, roadmap spec
 * §12): the pending proposals, grouped by product, with the j/k a r e x A key
 * map and the bulk bar re-homed from the old standalone Triage page.
 *
 * Since spec 2026-09-18 §6 the queue carries TWO kinds — classification
 * proposals and decision proposals — through one ordering spec and one key map.
 * A decision is not bulk-acceptable (the accept carries the option the person
 * chose) and has no scores to edit, so `x`, `e` and `A` do not reach one; `a`
 * OPENS it rather than accepting, because there is no chosen option in a list
 * row and a decision has no undo window to take a guess back with. `r` rejects
 * either kind: refusing needs no choice, and it parks the item for its owner.
 *
 * Two cursors, deliberately: SELECTION is the URL (`?proposal=`, arriving as
 * `selectedKey`) because the detail pane is what it opens, while the KEY cursor
 * is local state anchored to a row id (`inboxKeys.ts`). The queue moves under
 * the user's hands — accepts are optimistic, `useTriageLive` invalidates on
 * every `/ws` status frame — and an accept is irreversible past the undo
 * window, so the cursor rides its row, never an index.
 *
 * The editor is the detail pane (spec §12), so `e` does not open an inline
 * form the way the old page did: it opens the row with `edit`, and the pane
 * puts the caret in the score fields.
 *
 * The key map is local to this component rather than registered in
 * `shell/shortcuts.ts`: these keys exist only while the Inbox scope is on
 * screen, and `a`/`r` are irreversible enough that they must not survive it.
 * Both guards from the old page come along — `overlayIsOpen()` (a modal sheet
 * parks focus on its Close BUTTON, which is not a typing target) and
 * `isTypingTarget` (the listener is on `window`, so it sees the search box, the
 * prompt dock and everything else).
 */
import { useCallback, useEffect, useMemo, useReducer, useState } from 'react'
import {
  isDecisionProposal, isTriageProposal, type InboxProposal, type TriageProposal,
} from '@/api/triage'
import {
  useAcceptBulk, useAcceptProposal, useRejectDecision, useRejectProposal, useTriageLive,
} from '@/api/hooks'
import { GroupedList } from '@/ui/list/GroupedList'
import { BulkActionBar } from '@/ui/list/BulkActionBar'
import { SessionListControls } from '@/features/sessions/list/SessionListControls'
import { Banner } from '@/ui/Banner'
import { Button } from '@/ui/Button'
import { EmptyState } from '@/ui/EmptyState'
import { Kbd } from '@/ui/Kbd'
import { Skeleton } from '@/ui/Skeleton'
import { useItemOrdering } from '@/session/useSessionOrdering'
import { overlayIsOpen } from '@/shell/useOverlayState'
import { PRODUCT, ROADMAP } from '@/features/products/vocab'
import { ScopeBody } from '@/features/sessions/ScopeBody'
import { proposalKey } from '../roadmapNavState'
import { inboxReducer, initialInboxState, isTypingTarget, resolveCursorIndex } from './inboxKeys'
import { clearInboxOrder, setInboxOrder } from './inboxOrderStore'
import { INBOX_ORDERING_SPEC, type InboxGroupBy, type InboxSortBy } from './inboxOrdering'
import { ProposalCard } from './ProposalCard'
import { isPending, pendingProposals } from './triageSelectors'

export interface InboxScopeBodyProps {
  proposals: readonly InboxProposal[]
  isLoading: boolean
  isError: boolean
  /** The `?product=` chip, or null for every product. */
  product: string | null
  /** Key of the open row (`keyForOpenItem`), or null. */
  selectedKey: string | null
  onOpen: (p: InboxProposal, opts?: { edit: boolean }) => void
  onClearProduct: () => void
}

const haystack = (p: InboxProposal) =>
  (isDecisionProposal(p)
    ? `${p.title ?? ''} ${p.ref ?? ''} ${p.options.map((o) => o.label).join(' ')} ${p.rationale ?? ''}`
    : `${p.title ?? ''} ${p.repo ?? ''}#${p.number ?? ''} ${p.proposedFeatureSlug ?? ''} ${p.noFitSuggestion ?? ''}`)
    .toLowerCase()

export function InboxScopeBody({
  proposals, isLoading, isError, product, selectedKey, onOpen, onClearProduct,
}: InboxScopeBodyProps) {
  useTriageLive()
  const [query, setQuery] = useState('')
  const q = query.trim().toLowerCase()
  const visible = useMemo(
    () => (q ? proposals.filter((p) => haystack(p).includes(q)) : proposals),
    [proposals, q],
  )
  const ordering = useItemOrdering(visible, INBOX_ORDERING_SPEC)
  const ordered = ordering.orderedSessions
  const ids = useMemo(() => ordered.map((p) => p.id), [ordered])

  // This list IS what "the next proposal" means to the detail pane, which is a
  // sibling component and cannot see this hook's state (see `inboxOrderStore`).
  // Published as rendered — grouping, sort, collapse and the search box all
  // already applied — so an accept in the pane advances to the row the user can
  // actually see below the one they just decided.
  useEffect(() => setInboxOrder(ids), [ids])
  // Unmounted (the column is gone, e.g. the phone layout while a proposal is
  // open): the pane must fall back rather than follow a frozen order.
  useEffect(() => clearInboxOrder, [])

  // The rows the bulk bar may act on, through `triageSelectors`' one definition
  // of "still pending" (the nav badge and the Inbox scope count read the same
  // one). Shared from here by the selection prune below and by `acceptSelected`
  // — two copies would be free to disagree about what the bulk bar counts and
  // what it sends.
  //
  // Pending AND classification: `/pm/triage/accept-bulk` accepts classification
  // proposals only, and a decision id reaching it would be a 404 inside a sweep
  // whose toast then under-reports. The kind filter is here rather than inside
  // the selector because it is not a second answer to "is this awaiting a
  // human?" — decisions ARE — but to "can this be decided without opening it?".
  const pendingIds = useMemo(
    () => pendingProposals(ordered).filter(isTriageProposal).map((p) => p.id),
    [ordered],
  )

  const [state, dispatch] = useReducer(inboxReducer, initialInboxState)
  // Re-anchor whenever the queue moves — an optimistic removal, a live prepend,
  // a re-group, or a search that hid the anchored row — and drop from the
  // selection any row that stopped being pending WITHOUT leaving: a fresh
  // auto-accept stays visible for its undo window, so a ticked row can go
  // `accepted` under the user's hands and `rowsChanged` (which sees ids, not
  // statuses) would keep counting it in the bulk bar.
  useEffect(() => {
    dispatch({ type: 'rowsChanged', ids })
    dispatch({ type: 'pruneSelected', pendingIds })
  }, [ids, pendingIds])
  const current: InboxProposal | undefined = ordered[resolveCursorIndex(ids, state)]

  const accept = useAcceptProposal(product ?? undefined)
  const reject = useRejectProposal(product ?? undefined)
  const rejectDecision = useRejectDecision()
  const bulk = useAcceptBulk(product ?? undefined)
  const acceptMutate = accept.mutate
  const rejectMutate = reject.mutate
  const rejectDecisionMutate = rejectDecision.mutate
  const bulkMutate = bulk.mutate

  // A single accept/reject takes its row out of the list, so it takes the row
  // out of the SELECTION too: a later `A` resending the id gets `not_pending`
  // back and the sweep's toast under-reports.
  const acceptOne = useCallback((p: TriageProposal) => {
    acceptMutate({ id: p.id })
    dispatch({ type: 'deselect', id: p.id })
  }, [acceptMutate])
  // One `r`, two endpoints — the reject a decision needs is its own route
  // (it writes the person's owner-action hold, not a triage state), so the row's
  // kind picks the mutation here rather than the key map growing a second case.
  const rejectOne = useCallback((p: InboxProposal) => {
    if (isDecisionProposal(p)) rejectDecisionMutate({ id: p.id })
    else rejectMutate({ id: p.id })
    dispatch({ type: 'deselect', id: p.id })
  }, [rejectMutate, rejectDecisionMutate])
  // Cleared on SUCCESS, not on dispatch: a failed sweep rolls the rows back,
  // and a selection cleared ahead of that leaves them restored but unselected.
  // The `pendingIds` filter is belt to the prune effect's braces: the prune
  // runs on a render, this runs on a keystroke, and only this one decides what
  // actually goes over the wire.
  const acceptSelected = useCallback(() => {
    const stillPending = new Set(pendingIds)
    const sel = [...state.selected].filter((id) => stillPending.has(id))
    if (sel.length === 0) return
    bulkMutate(sel, { onSuccess: () => dispatch({ type: 'clearSelection' }) })
  }, [bulkMutate, state.selected, pendingIds])

  useEffect(() => {
    function onKey(ev: KeyboardEvent) {
      // Somebody else already claimed this keydown; an overlay is up; or the
      // key is part of a chord that belongs to the browser (Ctrl+A, Cmd+R).
      // Shift is allowed through — it is how `A` is typed.
      if (ev.defaultPrevented || overlayIsOpen()) return
      if (ev.ctrlKey || ev.metaKey || ev.altKey) return
      if (isTypingTarget(ev.target)) return
      switch (ev.key) {
        case 'j': dispatch({ type: 'move', delta: 1, ids }); break
        case 'k': dispatch({ type: 'move', delta: -1, ids }); break
        // A row past `pending` (a fresh auto-accept, still shown for the undo
        // window) has already had its one decision — none of these keys have
        // anything left to act on, so the cursor sitting on it is inert.
        case 'x':
          // A decision cannot go into a bulk accept (see `pendingIds`).
          if (!isPending(current) || isDecisionProposal(current)) return
          dispatch({ type: 'toggleSelect', id: current.id })
          break
        case 'a':
          if (!isPending(current)) return
          // A decision's accept carries the option the person chose, and there
          // is no undo window to take a guessed one back with — so `a` opens the
          // pane where the options are, rather than silently accepting the
          // machine's own recommendation on one keystroke.
          if (isDecisionProposal(current)) onOpen(current)
          else acceptOne(current)
          break
        case 'r':
          if (!isPending(current)) return
          rejectOne(current)
          break
        case 'e':
          // Nothing to edit on a decision: the scores are the classifier's.
          if (!isPending(current) || isDecisionProposal(current)) return
          onOpen(current, { edit: true })
          break
        case 'A':
          if (state.selected.size === 0) return
          acceptSelected()
          break
        // Every other key is somebody else's; fall through WITHOUT
        // preventDefault so typing, tabbing and browser shortcuts still work.
        default: return
      }
      ev.preventDefault()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [ids, current, state.selected, acceptOne, rejectOne, acceptSelected, onOpen])

  const emptyTitle = q
    ? 'No matches'
    : product ? `Inbox zero for ${product}` : 'Inbox zero — nothing to triage.'

  return (
    <ScopeBody
      controls={
        <div className="flex flex-col gap-2">
          <SessionListControls<InboxGroupBy, InboxSortBy>
            groupBy={ordering.groupBy}
            sortBy={ordering.sortBy}
            onGroupByChange={ordering.setGroupBy}
            onSortByChange={ordering.setSortBy}
            searchQuery={query}
            onSearchChange={setQuery}
            searchPlaceholder="Search inbox…"
            searchAriaLabel="Search inbox"
            groupOptions={[
              { value: 'product', label: PRODUCT.singular },
              { value: 'capability', label: 'Capability' },
              { value: 'confidence', label: 'Confidence' },
              { value: 'none', label: 'None' },
            ]}
            sortOptions={[
              { value: 'newest', label: 'Newest' },
              { value: 'confidence', label: 'Confidence' },
              { value: 'title', label: 'Title' },
            ]}
          />
          {product && (
            <div className="flex items-center gap-2 text-xs text-ink-muted">
              <span>Scope: {product}</span>
              <Button type="button" variant="ghost" size="sm" onClick={onClearProduct}>
                Show every product
              </Button>
            </div>
          )}
          {/* Keyboard-FIRST, not keyboard-only — every key below also has a
              pointer affordance (click a row, the per-row tick, the bulk bar). */}
          <p className="flex flex-wrap items-center gap-1 text-xs text-ink-faint">
            <Kbd keys="j" />/<Kbd keys="k" /> move · <Kbd keys="a" /> accept ·
            {' '}<Kbd keys="e" /> edit · <Kbd keys="r" /> reject ·
            {' '}<Kbd keys="x" /> select · <Kbd keys="A" /> accept selected
          </p>
          {/* In the controls strip, not below the rows — the same place both
              other bulk-selecting lists put it (`SessionList`,
              `ArchivedSessionList`). Inside the scroll band it scrolls away
              from the selection that summoned it. */}
          {state.selected.size > 0 && (
            <BulkActionBar
              label={`${state.selected.size} selected`}
              onClear={() => dispatch({ type: 'clearSelection' })}
            >
              {/* `lg` is the only size that carries the 44px touch target,
                  which is what `BulkActionBar`'s own Clear button pins to. */}
              <Button type="button" variant="secondary" size="lg" onClick={acceptSelected} disabled={bulk.isPending}>
                Accept selected ({state.selected.size})
              </Button>
            </BulkActionBar>
          )}
        </div>
      }
    >
      {isError && <Banner tone="danger" title="Could not load the inbox." />}
      {isLoading && proposals.length === 0 && (
        <div role="status" aria-label="Loading inbox"><Skeleton lines={5} /></div>
      )}
      {!isLoading && visible.length === 0 && <EmptyState title={emptyTitle} />}
      <GroupedList<InboxProposal>
        groups={ordering.groups}
        collapsedKeys={ordering.collapsedKeys}
        onToggleCollapse={ordering.toggleCollapse}
        panelRole="list"
        ariaLabel={ROADMAP.inbox}
        renderRow={(p) => (
          <ProposalCard
            key={p.id}
            proposal={p}
            selected={selectedKey === proposalKey(p.id)}
            checked={state.selected.has(p.id)}
            cursor={current?.id === p.id}
            // Pointer parity for j/k: opening a row is also where the cursor
            // goes, so the next `a` acts on what was clicked.
            onOpen={() => {
              dispatch({ type: 'focusRow', id: p.id, index: ids.indexOf(p.id) })
              onOpen(p)
            }}
            onToggleSelect={() => dispatch({ type: 'toggleSelect', id: p.id })}
          />
        )}
      />
    </ScopeBody>
  )
}
