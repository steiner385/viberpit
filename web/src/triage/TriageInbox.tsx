/**
 * Triage Inbox — the daily driver (design §8). One flat queue across all
 * projects, newest first, keyboard-first: j/k move · a accept · e edit-then-
 * accept · r reject · x select · A accept-selected. Optimistic over the
 * proposal→overlay copy (rows vanish immediately, roll back on error — the
 * rollback and its toast live in the hooks, `api/hooks.ts`).
 *
 * Replaces the phase-2 plain triage review list that lived under `projects/`,
 * deleted in this same change; the Products tab now links here instead of
 * embedding a list of its own.
 */
import { useCallback, useEffect, useMemo, useReducer, useState } from 'react'

import {
  useAcceptBulk, useAcceptProposal, useRejectProposal, useTriage, useTriageLive,
  useUpdateOverlay,
} from '@/api/hooks'
import type { OverlayEdits, TriageProposal } from '@/api/triage'

import { inboxReducer, initialInboxState, isTypingTarget } from './keyboardState'
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

export function TriageInbox() {
  useTriageLive()
  const { data: rows = [], isLoading } = useTriage()
  const accept = useAcceptProposal()
  const reject = useRejectProposal()
  const acceptBulk = useAcceptBulk()
  const updateOverlay = useUpdateOverlay()

  const [state, dispatch] = useReducer(inboxReducer, initialInboxState)
  const [editValues, setEditValues] = useState<OverlayEdits>({})

  // Clamp the cursor whenever the queue length changes (optimistic removals).
  useEffect(() => {
    dispatch({ type: 'rowsChanged', count: rows.length })
  }, [rows.length])

  // `undefined` whenever the queue is empty, or for the frame between an
  // optimistic removal and the clamp above — every handler below tolerates it.
  const current: TriageProposal | undefined = rows[state.cursor]

  const acceptMutate = accept.mutate
  const rejectMutate = reject.mutate
  const bulkMutate = acceptBulk.mutate

  useEffect(() => {
    function onKey(ev: KeyboardEvent) {
      // Never hijack a keystroke aimed at a form control, in ANY mode: the
      // listener is on `window`, so it also sees the search box, the prompt
      // dock and this component's own score inputs.
      if (isTypingTarget(ev.target)) return
      // A chord is somebody else's shortcut (Ctrl+A "select all", Cmd+R
      // "reload"). Only the bare key — and Shift, which is how `A` is typed.
      if (ev.ctrlKey || ev.metaKey || ev.altKey) return

      // While the inline editor is open the only inbox key is Escape; the rest
      // belong to the form (its inputs are already excluded above, but the
      // buttons and the form itself are not).
      if (state.editing !== null) {
        if (ev.key === 'Escape') {
          dispatch({ type: 'cancelEdit' })
          ev.preventDefault()
        }
        return
      }

      switch (ev.key) {
        case 'j': dispatch({ type: 'move', delta: 1, count: rows.length }); break
        case 'k': dispatch({ type: 'move', delta: -1, count: rows.length }); break
        case 'x':
          if (!current) return
          dispatch({ type: 'toggleSelect', id: current.id })
          break
        case 'a':
          if (!current) return
          acceptMutate({ id: current.id, edits: undefined })
          break
        case 'r':
          if (!current) return
          rejectMutate({ id: current.id })
          break
        case 'e':
          if (!current) return
          setEditValues(scoreOf(current))
          dispatch({ type: 'startEdit', id: current.id })
          break
        case 'A':
          if (state.selected.size === 0) return
          bulkMutate([...state.selected])
          dispatch({ type: 'clearSelection' })
          break
        // Every other key is somebody else's; fall through WITHOUT
        // preventDefault so typing, tabbing and browser shortcuts still work.
        default: return
      }
      ev.preventDefault()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [rows.length, current, state.editing, state.selected,
      acceptMutate, rejectMutate, bulkMutate])

  const depths = useMemo(() => queueDepthByProject(rows), [rows])

  const overlayMutateAsync = updateOverlay.mutateAsync
  const commitEdit = useCallback(async (p: TriageProposal) => {
    // edit-then-accept: persist the overlay edit, then accept the proposal.
    // A proposal with no mirrored work item has no overlay to patch, so it goes
    // straight to accept, which carries the same edits in its body.
    if (p.workItemId) await overlayMutateAsync({ itemId: p.workItemId, edit: editValues })
    acceptMutate({ id: p.id, edits: editValues })
    dispatch({ type: 'cancelEdit' })
  }, [editValues, overlayMutateAsync, acceptMutate])

  if (isLoading) return <div className="triage-empty">Loading…</div>
  if (!rows.length) return <div className="triage-empty">Inbox zero — nothing to triage.</div>

  return (
    <div className="triage-inbox">
      <header className="triage-header">
        <span className="triage-hint">j/k move · a accept · e edit · r reject · x select · A accept selected</span>
        {depths.map(([slug, n]) => (
          <span key={slug} className="triage-depth">{slug}: {n}</span>
        ))}
      </header>
      <div role="list" aria-label="Triage queue" className="triage-list">
        {rows.map((p, i) => (
          <div
            key={p.id}
            role="listitem"
            aria-current={i === state.cursor ? 'true' : undefined}
            className={
              'triage-row' + (i === state.cursor ? ' is-cursor' : '')
              + (state.selected.has(p.id) ? ' is-selected' : '')
            }
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
            {state.editing === p.id && (
              <form
                className="triage-editor"
                onSubmit={(e) => { e.preventDefault(); void commitEdit(p) }}
              >
                <input aria-label="reach" type="number" value={editValues.reach ?? ''}
                       onChange={(e) => setEditValues((v) => ({ ...v, reach: Number(e.target.value) }))} />
                <input aria-label="impact" type="number" value={editValues.impact ?? ''}
                       onChange={(e) => setEditValues((v) => ({ ...v, impact: Number(e.target.value) }))} />
                <input aria-label="effort" type="number" value={editValues.effort ?? ''}
                       onChange={(e) => setEditValues((v) => ({ ...v, effort: Number(e.target.value) }))} />
                <input aria-label="confidence" type="number" value={editValues.confidence ?? ''}
                       onChange={(e) => setEditValues((v) => ({ ...v, confidence: Number(e.target.value) }))} />
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
