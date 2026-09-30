/**
 * The Inbox scope of the Roadmap list column (finish-line §4.3, roadmap spec
 * §12): the pending proposals, grouped by product, with the j/k a r e x A R key
 * map and the bulk bar re-homed from the old standalone Triage page.
 *
 * Since spec 2026-09-18 §6 the queue carries TWO kinds — classification
 * proposals and decision proposals — through one ordering spec and one key map.
 * A decision is not bulk-acceptable (the accept carries the option the person
 * chose) and has no scores to edit, so `x`, `e`, `A` and `R` do not reach one;
 * `a` OPENS it rather than accepting, because there is no chosen option in a
 * list row and a decision has no undo window to take a guess back with. `r`
 * rejects either kind: refusing needs no choice, and it parks the item for its
 * owner.
 *
 * The SELECTION is the Inbox's own reducer rather than `useBulkSelection`
 * (the Sessions lists' hook): its prune is status-aware (`pruneSelected` — a
 * ticked row can stop being decidable without leaving the list), which that hook
 * cannot express. The header tick, the shift-click range and the `A`/`R` sweeps
 * all act on ONE set, `pendingIds` — the pending classification rows in rendered
 * order — so what "Select all" ticks, what a range can span, and what a sweep
 * sends are the same rows by construction.
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
 * The LISTENER is local to this component rather than installed by the shell:
 * these keys exist only while the Inbox scope is on screen, and `a`/`r` are
 * irreversible enough that they must not survive it. Both guards from the old
 * page come along — `overlayIsOpen()` (a modal sheet parks focus on its Close
 * BUTTON, which is not a typing target) and `isTypingTarget` (the listener is on
 * `window`, so it sees the search box, the prompt dock and everything else).
 *
 * The KEYS, though, come from `shell/shortcuts.ts`' `roadmap-inbox` group, read
 * here through `matchShortcut(ev, inGroup('roadmap-inbox'))`. A scope-local
 * switch on `ev.key` was invisible to the Help sheet (`?`), which renders from
 * that registry and nothing else, so the whole map was undiscoverable (#1494) —
 * and the Kbd legend below was a second, hand-kept copy of it. Registering the
 * rows does NOT hand them to the shell's dispatcher: that can only fire a row
 * whose id has a handler in `AppShellInner`, and none of these do.
 */
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react'
import {
  isDecisionProposal, isTriageProposal, type InboxProposal, type TriageProposal,
} from '@/api/triage'
import {
  useAcceptBulk, useAcceptProposal, useRejectBulk, useRejectDecision, useRejectProposal,
  useTriageLive,
} from '@/api/hooks'
import { GroupedList } from '@/ui/list/GroupedList'
import { BulkActionBar } from '@/ui/list/BulkActionBar'
import { SELECT_CHECKBOX_CLS } from '@/ui/list/SelectableRow'
import { SessionListControls } from '@/features/sessions/list/SessionListControls'
import { Banner } from '@/ui/Banner'
import { Button } from '@/ui/Button'
import { EmptyState } from '@/ui/EmptyState'
import { Kbd } from '@/ui/Kbd'
import { Skeleton } from '@/ui/Skeleton'
import { useItemOrdering } from '@/session/useSessionOrdering'
import { overlayIsOpen } from '@/shell/useOverlayState'
import { inGroup, matchShortcut } from '@/shell/shortcuts'
import { PRODUCT, ROADMAP } from '@/features/products/vocab'
import { ScopeBody } from '@/features/sessions/ScopeBody'
import { proposalKey } from '../roadmapNavState'
import { INBOX_LOAD_ERROR } from './inboxErrors'
import { inboxReducer, initialInboxState, isTypingTarget, resolveCursorIndex } from './inboxKeys'
import { clearInboxOrder, setInboxOrder } from './inboxOrderStore'
import { INBOX_ORDERING_SPEC, type InboxGroupBy, type InboxSortBy } from './inboxOrdering'
import { InboxStarvationNotice } from './InboxStarvationNotice'
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

  // The header tick's three states, over the same set the sweeps send. The
  // selection is pruned to `pendingIds`, so "some" is simply "any".
  const allSelected = pendingIds.length > 0 && pendingIds.every((id) => state.selected.has(id))
  const someSelected = state.selected.size > 0
  // `indeterminate` is a DOM property, not an attribute — same idiom as
  // `ArchivedSessionList`'s header.
  const selectAllRef = useRef<HTMLInputElement>(null)
  useEffect(() => {
    if (selectAllRef.current) selectAllRef.current.indeterminate = someSelected && !allSelected
  }, [someSelected, allSelected])

  const accept = useAcceptProposal(product ?? undefined)
  const reject = useRejectProposal(product ?? undefined)
  const rejectDecision = useRejectDecision()
  const bulk = useAcceptBulk(product ?? undefined)
  const bulkReject = useRejectBulk(product ?? undefined)
  const acceptMutate = accept.mutate
  const rejectMutate = reject.mutate
  const rejectDecisionMutate = rejectDecision.mutate
  const bulkMutate = bulk.mutate
  const bulkRejectMutate = bulkReject.mutate
  const sweeping = bulk.isPending || bulkReject.isPending

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
  // The reject twin — same filter, same clear-on-success, its own endpoint.
  const rejectSelected = useCallback(() => {
    const stillPending = new Set(pendingIds)
    const sel = [...state.selected].filter((id) => stillPending.has(id))
    if (sel.length === 0) return
    bulkRejectMutate(sel, { onSuccess: () => dispatch({ type: 'clearSelection' }) })
  }, [bulkRejectMutate, state.selected, pendingIds])
  // "Select all" ticks every selectable row; ticked again with all of them
  // already in, it clears — the `toggleAll` rule the Sessions lists follow.
  const toggleAll = useCallback(() => {
    if (allSelected) dispatch({ type: 'clearSelection' })
    else dispatch({ type: 'selectAll', ids: pendingIds })
  }, [allSelected, pendingIds])

  useEffect(() => {
    function onKey(ev: KeyboardEvent) {
      // Somebody else already claimed this keydown; an overlay is up; or the
      // key is part of a chord that belongs to the browser (Ctrl+A, Cmd+R —
      // every `roadmap-inbox` row uses `plain()`, which bails on those anyway).
      // Shift is allowed through — it is how `A` is typed.
      if (ev.defaultPrevented || overlayIsOpen()) return
      if (isTypingTarget(ev.target)) return
      // No context argument: a `roadmap-inbox` row carries no `when` (the guards
      // above are this scope's, not the registry's), and passing a fabricated
      // context is how a `when` comes to be evaluated against a lie.
      const row = matchShortcut(ev, inGroup('roadmap-inbox'))
      switch (row?.id) {
        case 'inbox.next': dispatch({ type: 'move', delta: 1, ids }); break
        case 'inbox.prev': dispatch({ type: 'move', delta: -1, ids }); break
        // A row past `pending` (a fresh auto-accept, still shown for the undo
        // window) has already had its one decision — none of these keys have
        // anything left to act on, so the cursor sitting on it is inert.
        case 'inbox.select':
          // A decision cannot go into a bulk accept (see `pendingIds`).
          if (!isPending(current) || isDecisionProposal(current)) return
          dispatch({ type: 'toggleSelect', id: current.id })
          break
        // Neither key DECIDES a decision from the list — both open the pane.
        // Accept carries the option the person chose, and reject is terminal
        // (`_require_pending`: a rejected proposal can never be accepted, the
        // drafted options are gone and only a new session can re-propose). With
        // no undo window behind either, a one-keystroke accept of the machine's
        // own recommendation and a one-keystroke discard are the same hazard, so
        // both land in the pane, where the options and the consequence copy are
        // and the real Accept/Reject buttons live.
        case 'inbox.accept':
          if (!isPending(current)) return
          if (isDecisionProposal(current)) onOpen(current)
          else acceptOne(current)
          break
        case 'inbox.reject':
          if (!isPending(current)) return
          if (isDecisionProposal(current)) onOpen(current)
          else rejectOne(current)
          break
        case 'inbox.edit':
          // Nothing to edit on a decision: the scores are the classifier's.
          if (!isPending(current) || isDecisionProposal(current)) return
          onOpen(current, { edit: true })
          break
        case 'inbox.acceptSelected':
          if (state.selected.size === 0) return
          acceptSelected()
          break
        case 'inbox.rejectSelected':
          if (state.selected.size === 0) return
          rejectSelected()
          break
        // Every other key is somebody else's; fall through WITHOUT
        // preventDefault so typing, tabbing and browser shortcuts still work.
        default: return
      }
      ev.preventDefault()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [ids, current, state.selected, acceptOne, rejectOne, acceptSelected, rejectSelected, onOpen])

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
          {/* And it follows the CURSOR: the two row kinds answer the same keys
              differently, so a fixed strip promising "a accept · r reject" is
              false half the time. `A` and `R` stay on both — they act on the
              selection, which a decision can never join. */}
          {/* `ink-muted`, not `ink-faint`: this is read, and `ink-faint` on the
              surface is 3.1:1 — the non-text floor, under the 4.5:1 text floor
              (2026-09-30 Roadmap critique, P3). */}
          <p className="flex flex-wrap items-center gap-1 text-xs text-ink-muted">
            <Kbd keys="j" />/<Kbd keys="k" /> move ·
            {current !== undefined && isDecisionProposal(current)
              ? <>{' '}<Kbd keys="a" /> / <Kbd keys="r" /> open ·</>
              : (
                <>
                  {' '}<Kbd keys="a" /> accept ·
                  {' '}<Kbd keys="e" /> edit · <Kbd keys="r" /> reject ·
                  {' '}<Kbd keys="x" /> select ·
                </>
              )}
            {' '}<Kbd keys="A" /> accept selected · <Kbd keys="R" /> reject selected
          </p>
          {/* The header tick, over the rows a sweep can act on — hidden when
              there are none, since a tick that can select nothing is a lie. */}
          {pendingIds.length > 0 && (
            <label className="flex items-center gap-2 text-xs text-ink-muted">
              <input
                ref={selectAllRef}
                type="checkbox"
                checked={allSelected}
                onChange={toggleAll}
                aria-label="Select all"
                className={SELECT_CHECKBOX_CLS}
              />
              <span>Select all ({pendingIds.length})</span>
            </label>
          )}
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
              <Button type="button" variant="secondary" size="lg" onClick={acceptSelected} disabled={sweeping}>
                Accept selected ({state.selected.size})
              </Button>
              {/* Not styled as destructive: a reject parks the item under an
                  owner-action hold and the sweep's toast can undo it, unlike
                  the archive list's Delete. */}
              <Button type="button" variant="secondary" size="lg" onClick={rejectSelected} disabled={sweeping}>
                Reject selected ({state.selected.size})
              </Button>
            </BulkActionBar>
          )}
        </div>
      }
    >
      {/* The column ORs the two queries into one flag: it lists whatever loaded,
          and an undifferentiated banner is right for a surface that shows both
          kinds interleaved. The PANE, which renders one row, names the half that
          broke (`inboxErrorTitle`). */}
      <InboxStarvationNotice product={product ?? null} />
      {isError && <Banner tone="danger" title={INBOX_LOAD_ERROR} />}
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
            // Shift extends a range from the last plain tick, over the
            // selectable rows in rendered order (`pendingIds`).
            onToggleSelect={(shift) => dispatch(shift
              ? { type: 'selectRange', id: p.id, ids: pendingIds }
              : { type: 'toggleSelect', id: p.id })}
          />
        )}
      />
    </ScopeBody>
  )
}
