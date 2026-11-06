/**
 * PromptRow — the compact control row under the session composer (sub-spec D §3):
 * Mode ▾ · Model ▾ · Effort ▾ · usage ring · ⚡. It reads the live
 * runtime directly; the session record, engine capabilities and model catalog are
 * passed in so capability-gated controls hide (never disable) themselves.
 */

import { useImperativeHandle, useState, type Ref } from 'react'
import type { AgentCapabilities, ModelOption, Session } from '@/api/types'
import type { SessionRuntime } from '@/session/useSessionRuntime'
import { SESSION_MODE_OPTIONS } from '@/session/permissionModes'
import { statusTier } from '@/session/statusTier'
import { ModePicker } from './ModePicker'
import { ModelPicker } from './ModelPicker'
import { EffortPicker } from './EffortPicker'
import { effortChoices } from './effortChoices'
import { UsageRing } from '@/ui/UsageRing'
import { contextSeverity, contextUsedLabel } from '@/lib/contextWindow'
import FastToggle from './FastToggle'

/** Imperative openers for the row's menus (the Alt+Shift+M / I / E shortcuts). */
export interface PromptRowMenus {
  openMode(): void
  openModel(): void
  openEffort(): void
}

type OpenMenu = 'mode' | 'model' | 'effort' | null

export interface PromptRowProps {
  rt: Pick<SessionRuntime, 'mode' | 'pendingMode' | 'onSetMode' | 'model' | 'liveModel' | 'modelPending' | 'onSetModel' | 'contextPct'> & {
    state: { status: { state: string } }
  }
  /** The open session's record (undefined until the list query answers). */
  session?: Pick<Session, 'engine'> & Partial<Session>
  /** The session engine's capabilities; undefined means "unknown" — treat as present. */
  caps?: AgentCapabilities
  /** The session engine's model catalog (`GET /api/models`). */
  models?: readonly ModelOption[]
  menuRef?: Ref<PromptRowMenus>
  /** Turn fast mode on/off (D-14, `POST /sessions/{id}/fast`). Without it no ⚡. */
  onToggleFast?: (enabled: boolean) => void
  /** The fast-mode POST is in flight: ⚡ is disabled until it answers. */
  fastPending?: boolean
}

const NO_MODELS: readonly ModelOption[] = []

// `caps` gates nothing for the mode picker: no engine restricts permission modes
// (AgentCapabilities has no mode field and Codex maps all seven).
export function PromptRow({ rt, session, caps, models = NO_MODELS, menuRef, onToggleFast, fastPending = false }: PromptRowProps) {
  const [openMenu, setOpenMenu] = useState<OpenMenu>(null)
  const menuOpener = (menu: OpenMenu) => (next: boolean) => setOpenMenu(next ? menu : null)

  // The one effort reading, shared with the error card's "Lower effort" (D-19).
  const { efforts, effort } = effortChoices(rt.model, session, caps, models)
  const showEffort = efforts.length > 0
  const busy = statusTier(rt.state.status.state) === 'busy'
  const fastOn = session?.fastMode === true
  // Also while the stored choice is ON, so a model switch that dropped availability
  // never strands fast mode with no visible way to turn it off.
  const showFast = (session?.fastModeAvailable === true || fastOn) && onToggleFast !== undefined

  useImperativeHandle(menuRef, () => ({
    openMode: () => setOpenMenu('mode'),
    openModel: () => setOpenMenu('model'),
    // Hidden, not disabled: with no effort picker the shortcut does nothing.
    openEffort: () => { if (showEffort) setOpenMenu('effort') },
  }), [showEffort])

  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 px-2 pb-1 text-xs" data-testid="prompt-row">
      <ModePicker
        mode={rt.mode}
        pendingMode={rt.pendingMode}
        options={SESSION_MODE_OPTIONS}
        onSelect={rt.onSetMode}
        open={openMenu === 'mode'}
        onOpenChange={menuOpener('mode')}
      />
      <ModelPicker
        chosen={rt.model}
        live={rt.liveModel}
        pending={rt.modelPending}
        models={models}
        engine={session?.engine}
        busy={busy}
        canSteer={caps?.steer === true}
        onSelect={(id, when) => rt.onSetModel(id, when)}
        open={openMenu === 'model'}
        onOpenChange={menuOpener('model')}
      />
      {showEffort && (
        <EffortPicker
          effort={effort}
          efforts={efforts}
          // A busy pick lands after the turn: an effort change never stops and
          // replays the running turn (no dialog — there is nothing to choose).
          onSelect={(next) => rt.onSetModel(rt.model ?? 'default', busy ? 'after_turn' : 'now', next)}
          open={openMenu === 'effort'}
          onOpenChange={menuOpener('effort')}
        />
      )}
      {rt.contextPct != null && <ContextRing remainingPct={rt.contextPct} />}
      {showFast && (
        <FastToggle
          on={fastOn}
          onToggle={onToggleFast}
          pending={fastPending}
          liveState={session?.fastModeState}
          disabledReason={session?.fastModeDisabledReason}
        />
      )}
    </div>
  )
}

/** The context-window ring. `remainingPct` is the share still FREE (`rt.contextPct`). */
function ContextRing({ remainingPct }: { remainingPct: number }) {
  const usedPct = 100 - remainingPct
  const label = `${contextUsedLabel(remainingPct)}${contextSeverity(remainingPct) === 'danger' ? ' · auto-compacts soon' : ''}`
  // `title` on the wrapper: the ring is icon-sized, so the hover tooltip is how a
  // sighted user reads the number the aria-label gives a screen reader.
  return (
    <span title={label} className="ml-auto inline-flex">
      <UsageRing value={usedPct / 100} label={label} />
    </span>
  )
}
