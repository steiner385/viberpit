/* eslint-disable react-refresh/only-export-components -- trackState is a pure
   mapper colocated with (and unit-tested against) InitiativeTrack, mirroring the
   MetroTrack idiom it reuses; a separate module for one helper over-splits. */
// Horizontal artifact/stage track for one Initiative — reuses the pipeline
// "metro" idiom and its `.track`/`.node`/`.seg` design tokens (plotroom.css,
// scoped to .prdash-root) rather than re-styling (constitution VI). Unlike
// MetroTrack it maps the SDLC lifecycle stages, not PR StageResult substates.
import { Fragment } from 'react'
import { INITIATIVE_STAGES, type InitiativeEpic, type InitiativeStage } from '@/api/sdlc'
import '../pipeline/plotroom.css'

export type NodeStatus = 'done' | 'active' | 'fail' | 'pending' | 'parked'
export interface TrackNode { label: string; status: NodeStatus }

/** Friendly per-stage label; the first three surface the artifact they commit
 *  (intent/spec/plan → Intent/Spec/Plan) so the chain reads as the playbook's
 *  intent→spec→plan→build progression. */
const STAGE_LABEL: Record<InitiativeStage, string> = {
  intake: 'Intent',
  design: 'Spec',
  plan: 'Plan',
  build: 'Build',
  deploy: 'Deploy',
  maintain: 'Maintain',
  done: 'Done',
}

const GLYPH: Partial<Record<NodeStatus, string>> = { done: '✓', fail: '✗', parked: '!' }

/**
 * Pure mapper: an initiative's (stage, status) → per-node track state. Stages
 * strictly before the current one are done; the current one is active (or
 * `parked` when the initiative is blocked, or `done` at the terminal stage);
 * everything after is pending. An unknown stage yields an all-pending track.
 */
export function trackState(stage: string | null, status: string | null): TrackNode[] {
  const s = INITIATIVE_STAGES.indexOf(stage as InitiativeStage)
  return INITIATIVE_STAGES.map((st, i) => {
    const label = STAGE_LABEL[st]
    if (s < 0) return { label, status: 'pending' as NodeStatus }
    if (i < s) return { label, status: 'done' as NodeStatus }
    if (i > s) return { label, status: 'pending' as NodeStatus }
    // i === s (the current stage)
    if (status === 'blocked') return { label, status: 'parked' as NodeStatus }
    if (st === 'done') return { label, status: 'done' as NodeStatus }
    return { label, status: 'active' as NodeStatus }
  })
}

function segment(prev: TrackNode, key: string) {
  return <i key={key} className={`seg${prev.status === 'done' ? ' done' : ''}`} />
}

function ariaLabel(nodes: TrackNode[]): string {
  const word: Record<NodeStatus, string> = {
    done: 'complete', active: 'in progress', pending: 'pending',
    fail: 'failed', parked: 'blocked',
  }
  return nodes.map((n) => `${n.label}: ${word[n.status]}`).join(', ')
}

/** Applied at render time only (never inside `trackState`, which stays a pure
 *  (stage, status) mapper — see #991): the Build node's label picks up the
 *  epic's sub-issue count ("Build 3/10") and becomes a deep link to the GitHub
 *  epic, per spec §7c ("ccswitch deep-links to the GitHub Project, ccswitch
 *  does not rebuild a board"). `tabIndex={-1}` keeps the link out of the tab
 *  order since it lives inside an `aria-hidden` node (the container's
 *  `aria-label` is already the accessible summary of the whole track). */
export function InitiativeTrack(
  { stage, status, epic }: { stage: string | null; status: string | null; epic?: InitiativeEpic | null },
) {
  const nodes = trackState(stage, status)
  return (
    <div className="track" aria-label={ariaLabel(nodes)}>
      {nodes.map((n, i) => {
        const isBuild = epic != null && INITIATIVE_STAGES[i] === 'build'
        const label = isBuild ? `${n.label} ${epic.done}/${epic.total}` : n.label
        const title = isBuild
          ? `${label} — ${ariaLabelWord(n.status)} (${epic.blocked} blocked, ${epic.needsHuman} needs human)`
          : `${n.label} — ${ariaLabelWord(n.status)}`
        return (
          <Fragment key={`${n.label}-${i}`}>
            {i > 0 && segment(nodes[i - 1]!, `seg-${i}`)}
            <span className={`node ${n.status}`} title={title} aria-hidden="true">
              <span className="c" aria-hidden="true">{GLYPH[n.status] ?? i + 1}</span>
              {isBuild
                ? (
                  <a className="node-label" href={epic.url} target="_blank" rel="noreferrer" tabIndex={-1}>
                    {label}
                  </a>
                )
                : <span className="node-label">{label}</span>}
            </span>
          </Fragment>
        )
      })}
    </div>
  )
}

function ariaLabelWord(status: NodeStatus): string {
  switch (status) {
    case 'done': return 'complete'
    case 'active': return 'in progress'
    case 'pending': return 'not reached yet'
    case 'fail': return 'failed'
    case 'parked': return 'blocked'
  }
}
