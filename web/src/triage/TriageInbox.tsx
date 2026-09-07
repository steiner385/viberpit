/**
 * Triage Inbox — the daily driver (design §8). One flat queue across all
 * projects, newest first, keyboard-first: j/k move · a accept · e edit-then-
 * accept · r reject · x select · A accept-selected. Optimistic over the
 * proposal→overlay copy (rows vanish immediately, roll back on error — the
 * rollback and its toast live in the hooks, `api/hooks.ts`).
 *
 * Keyboard-FIRST, not keyboard-only: `/triage` is in the phone nav, so every
 * key also has a pointer affordance (click a row to move the cursor, per-row
 * select/Accept/Reject buttons). They are deliberately quiet — the keys stay the
 * primary flow.
 *
 * Replaces the phase-2 plain triage review list that lived under `projects/`,
 * deleted in this same change; the Products tab now links here instead of
 * embedding a list of its own.
 */
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import {
  useAcceptBulk, useAcceptProposal, useRejectProposal, useTriage, useTriageLive,
  useUpdateOverlay,
} from '@/api/hooks'
import type { OverlayEdits, TriageProposal } from '@/api/triage'
import { NAV_BY_ID } from '@/shell/navDefs'
import { overlayIsOpen } from '@/shell/useOverlayState'
import { PageHeader } from '@/ui/PageHeader'
import { PageContent } from '@/ui/PageContent'

import {
  inboxReducer, initialInboxState, isTypingTarget, resolveCursorIndex, scoreInputValue,
} from './keyboardState'
import './triage.css'

/** Per-project queue depth for the header, from the server's `projectSlug` —
 *  NOT derived from `repo`, which is one repo of a possibly multi-repo project. */
function queueDepthByProject(rows: TriageProposal[]): [string, number][] {
  const counts = new Map<string, number>()
  for (const r of rows) {
    const k = r.projectSlug ?? 'unknown'
    counts.set(k, (counts.get(k) ?? 0) + 1)
  }
  return [...counts.entries()]
}

function scoreOf(p: TriageProposal): OverlayEdits {
  return {
    featureId: p.proposedFeatureId ?? undefined,
    reach: p.reach ?? undefined,
    impact: p.impact ?? undefined,
    effort: p.effort ?? undefined,
    confidence: p.confidence ?? undefined,
  }
}

function InboxBody() {
  useTriageLive()
  const [params] = useSearchParams()
  const projectSlug = params.get('projectSlug') || undefined
  const { data: rows = [], isLoading } = useTriage(projectSlug)
  const accept = useAcceptProposal(projectSlug)
  const reject = useRejectProposal(projectSlug)
  const acceptBulk = useAcceptBulk(projectSlug)
  const updateOverlay = useUpdateOverlay()

  const [state, dispatch] = useReducer(inboxReducer, initialInboxState)
  const [editValues, setEditValues] = useState<OverlayEdits>({})
  const rowEls = useRef(new Map<string, HTMLDivElement>())

  const rowIds = useMemo(() => rows.map((r) => r.id), [rows])

  // Re-anchor the cursor whenever the queue moves (optimistic removals, and the
  // prepends `useTriageLive` pulls in). The cursor rides its ROW, not its index.
  useEffect(() => {
    dispatch({ type: 'rowsChanged', ids: rowIds })
  }, [rowIds])

  // Derived every render from `cursorId`, so a proposal arriving between the
  // read and the keystroke cannot slide a different row under the cursor.
  const cursorIndex = resolveCursorIndex(rowIds, state)
  // `undefined` whenever the queue is empty — every handler below tolerates it.
  const current: TriageProposal | undefined = rows[cursorIndex]

  const acceptMutate = accept.mutate
  const rejectMutate = reject.mutate
  const bulkMutate = acceptBulk.mutate

  // Accepting is irreversible, so a cursor the user cannot SEE must not be the
  // one a keystroke acts on: `.triage-inbox` is the `overflow-y:auto` box, and
  // after enough `j` the highlighted row is off-screen.
  useEffect(() => {
    if (state.cursorId === null) return
    const el = rowEls.current.get(state.cursorId)
    // jsdom (and any non-layout host) implements no scrolling at all.
    if (el && typeof el.scrollIntoView === 'function') el.scrollIntoView({ block: 'nearest' })
  }, [state.cursorId])

  const acceptOne = useCallback((p: TriageProposal, edits?: OverlayEdits) => {
    acceptMutate({ id: p.id, edits })
    // The row is leaving the list, so it leaves the selection with it — an `A`
    // that resends an accepted id gets `not_pending` back and the sweep's toast
    // under-reports ("Accepted 1" for a two-row selection).
    dispatch({ type: 'deselect', id: p.id })
  }, [acceptMutate])

  const rejectOne = useCallback((p: TriageProposal) => {
    rejectMutate({ id: p.id })
    dispatch({ type: 'deselect', id: p.id })
  }, [rejectMutate])

  const acceptSelected = useCallback((ids: string[]) => {
    // Cleared on SUCCESS, not on dispatch: a failed sweep rolls the rows back,
    // and a selection cleared ahead of that leaves them restored but unselected.
    bulkMutate(ids, { onSuccess: () => dispatch({ type: 'clearSelection' }) })
  }, [bulkMutate])

  const startEdit = useCallback((p: TriageProposal) => {
    setEditValues(scoreOf(p))
    dispatch({ type: 'startEdit', id: p.id })
  }, [])

  useEffect(() => {
    function onKey(ev: KeyboardEvent) {
      // Somebody else already claimed this keydown (a focused list row, an open
      // `ui/Menu`'s Escape, the dispatch bar's arrows) — the same rule the app's
      // shortcut dispatcher uses, `shell/useShortcuts.ts`.
      if (ev.defaultPrevented) return
      // `AppShell` renders `DrawerHost` BESIDE the route `Outlet`, so `/triage`
      // stays mounted — with this listener live — under an overlay. Below 1280
      // the Help/Assistant drawer is a modal `Sheet` whose focus trap parks
      // focus on its Close BUTTON, which `isTypingTarget` reports false for: a
      // user reading "a accept" in the help sheet and pressing it would
      // irreversibly accept the row underneath. `overlayIsOpen()` is the app's
      // ONE answer to "is an overlay open" (`shell/useOverlayState`) — modal
      // traps and open menus, docked push-mode drawers deliberately excluded.
      if (overlayIsOpen()) return
      // A chord is somebody else's shortcut (Ctrl+A "select all", Cmd+R
      // "reload"). Only the bare key — and Shift, which is how `A` is typed.
      if (ev.ctrlKey || ev.metaKey || ev.altKey) return

      // Escape is checked BEFORE the typing guard: it is the documented way out
      // of the inline editor, and the editor's own inputs are typing targets, so
      // guarding first would leave a keyboard-only user reaching for the mouse.
      if (state.editing !== null && ev.key === 'Escape') {
        dispatch({ type: 'cancelEdit' })
        ev.preventDefault()
        return
      }
      // Never hijack a keystroke aimed at a form control, in ANY mode: the
      // listener is on `window`, so it also sees the search box, the prompt
      // dock and this component's own score inputs.
      if (isTypingTarget(ev.target)) return

      // While the inline editor is open the only inbox key is Escape (above);
      // the rest belong to the form — its inputs are already excluded, but its
      // buttons and the form itself are not.
      if (state.editing !== null) return

      switch (ev.key) {
        case 'j': dispatch({ type: 'move', delta: 1, ids: rowIds }); break
        case 'k': dispatch({ type: 'move', delta: -1, ids: rowIds }); break
        case 'x':
          if (!current) return
          dispatch({ type: 'toggleSelect', id: current.id })
          break
        case 'a':
          if (!current) return
          acceptOne(current)
          break
        case 'r':
          if (!current) return
          rejectOne(current)
          break
        case 'e':
          if (!current) return
          startEdit(current)
          break
        case 'A':
          if (state.selected.size === 0) return
          acceptSelected([...state.selected])
          break
        // Every other key is somebody else's; fall through WITHOUT
        // preventDefault so typing, tabbing and browser shortcuts still work.
        default: return
      }
      ev.preventDefault()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [rowIds, current, state.editing, state.selected,
      acceptOne, rejectOne, acceptSelected, startEdit])

  const depths = useMemo(() => queueDepthByProject(rows), [rows])

  const overlayMutateAsync = updateOverlay.mutateAsync
  const commitEdit = useCallback(async (p: TriageProposal) => {
    // edit-then-accept: persist the overlay edit, then accept the proposal.
    // A proposal with no mirrored work item has no overlay to patch, so it goes
    // straight to accept, which carries the same edits in its body.
    if (p.workItemId) {
      try {
        await overlayMutateAsync({ itemId: p.workItemId, edit: editValues })
      } catch {
        // The hook already toasted. Swallow rather than reject: this is called
        // from an event handler as a floating promise, so an escaping rejection
        // would ship a spurious `unhandledrejection` to the server log
        // (`obs/install.ts`). The editor stays open, holding the edit.
        return
      }
    }
    acceptOne(p, editValues)
    dispatch({ type: 'cancelEdit' })
  }, [editValues, overlayMutateAsync, acceptOne])

  if (isLoading) return <div className="triage-empty">Loading…</div>
  if (!rows.length) {
    return (
      <div className="triage-empty">
        {projectSlug
          ? <>Inbox zero for {projectSlug}. <Link to={NAV_BY_ID.triage.path}>Show every product</Link></>
          : 'Inbox zero — nothing to triage.'}
      </div>
    )
  }

  return (
    <div className="triage-inbox">
      <header className="triage-header">
        {projectSlug && (
          <span className="triage-depth">
            Scope: {projectSlug} <Link to={NAV_BY_ID.triage.path}>Clear</Link>
          </span>
        )}
        {depths.map(([slug, n]) => (
          <span key={slug} className="triage-depth">{slug}: {n}</span>
        ))}
      </header>
      <div role="list" aria-label="Triage queue" className="triage-list">
        {rows.map((p, i) => (
          <div
            key={p.id}
            role="listitem"
            ref={(el) => {
              if (el) rowEls.current.set(p.id, el)
              else rowEls.current.delete(p.id)
            }}
            aria-current={i === cursorIndex ? 'true' : undefined}
            className={
              'triage-row' + (i === cursorIndex ? ' is-cursor' : '')
              + (state.selected.has(p.id) ? ' is-selected' : '')
            }
            // Pointer parity for `j`/`k`: clicking anywhere on a row moves the
            // cursor to it, so the per-row buttons act on what was clicked.
            onClick={() => dispatch({ type: 'focusRow', id: p.id, index: i })}
          >
            <span className="triage-title">
              {p.url
                ? <a href={p.url} target="_blank" rel="noreferrer">{p.title}</a>
                : p.title}
            </span>
            <span className="triage-ref">{p.repo}#{p.number}</span>
            {p.noFitSuggestion
              ? <span className="triage-nofit">no fit → new: {p.noFitSuggestion}</span>
              : <span className="triage-feature">{p.proposedFeatureSlug}</span>}
            <span className="triage-scores">
              R{p.reach ?? '—'} I{p.impact ?? '—'} E{p.effort ?? '—'} C{p.confidence ?? '—'}
            </span>
            <span className={`triage-conf conf-${p.confidenceLevel ?? 'low'}`}>
              {p.confidenceLevel ?? 'low'}
            </span>
            <span className="triage-actions">
              <button
                type="button" className="triage-act"
                aria-pressed={state.selected.has(p.id)}
                aria-label={`Select ${p.title ?? p.id}`}
                onClick={() => dispatch({ type: 'toggleSelect', id: p.id })}
              >
                {state.selected.has(p.id) ? '☑' : '☐'}
              </button>
              <button
                type="button" className="triage-act"
                aria-label={`Accept ${p.title ?? p.id}`}
                onClick={() => acceptOne(p)}
              >
                Accept
              </button>
              <button
                type="button" className="triage-act"
                aria-label={`Reject ${p.title ?? p.id}`}
                onClick={() => rejectOne(p)}
              >
                Reject
              </button>
              <button
                type="button" className="triage-act"
                aria-label={`Edit scores for ${p.title ?? p.id}`}
                onClick={() => startEdit(p)}
              >
                Edit
              </button>
            </span>
            {/* The classifier's stated reason (design §8) — the one input a
                human has before a one-keystroke irreversible accept. */}
            {p.rationale && <span className="triage-rationale">{p.rationale}</span>}
            {state.editing === p.id && (
              <form
                className="triage-editor"
                onSubmit={(e) => { e.preventDefault(); void commitEdit(p) }}
              >
                {/* Autofocused: `e` is a keyboard gesture, and without this the
                    caret stays on <body> and a keyboard-only user has to Tab
                    through the whole nav rail to reach the first field. */}
                <input aria-label="reach" type="number" autoFocus value={editValues.reach ?? ''}
                       onChange={(e) => setEditValues((v) => ({ ...v, reach: scoreInputValue(e.target.value) }))} />
                <input aria-label="impact" type="number" value={editValues.impact ?? ''}
                       onChange={(e) => setEditValues((v) => ({ ...v, impact: scoreInputValue(e.target.value) }))} />
                <input aria-label="effort" type="number" value={editValues.effort ?? ''}
                       onChange={(e) => setEditValues((v) => ({ ...v, effort: scoreInputValue(e.target.value) }))} />
                <input aria-label="confidence" type="number" value={editValues.confidence ?? ''}
                       onChange={(e) => setEditValues((v) => ({ ...v, confidence: scoreInputValue(e.target.value) }))} />
                <button type="submit">Save &amp; accept</button>
                <button type="button" onClick={() => dispatch({ type: 'cancelEdit' })}>Cancel</button>
              </form>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}

export function TriageInbox() {
  return (
    <div className="flex flex-col h-full min-h-0" data-page-variant="standard">
      <PageHeader
        title="Triage"
        meta={<span className="triage-hint">j/k move · a accept · e edit · r reject · x select · A accept selected</span>}
      />
      <PageContent>
        <InboxBody />
      </PageContent>
    </div>
  )
}
