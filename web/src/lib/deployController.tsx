// web/src/lib/deployController.tsx
import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { useDeployStatus, useStartDeploy } from '@/api/hooks'
import { deployOutcome } from './deployPhases'

export type DeployIntent = 'idle' | 'starting' | 'held-success' | 'held-failure'

export interface DeployControllerValue {
  intent: DeployIntent
  visible: boolean
  starting: boolean
  startError: string | null
  /** Start a deploy. Pass a sha to deploy the green frontier (the newest CI-green
   *  pending commit) instead of origin/master's tip; omit for the tip. */
  start: (targetSha?: string) => void
  dismiss: () => void
}

const Ctx = createContext<DeployControllerValue | null>(null)

export function DeployControllerProvider({ children }: { children: ReactNode }) {
  const { data: deploy } = useDeployStatus()
  const start = useStartDeploy()
  const [intent, setIntent] = useState<DeployIntent>('idle')
  const running = deploy?.running ?? false
  const prevRunning = useRef(false)

  // Classify terminal outcome on the running true→false edge.
  // While running is true, we derive idle from the value below (no setState in effect body).
  useEffect(() => {
    const was = prevRunning.current
    prevRunning.current = running
    if (was && !running) {
      // 'idle' means the status was wiped rather than concluded (a controller process
      // replaced mid-poll reports an empty DeployStatus, #865): there is no outcome to
      // hold, so dismiss instead of flashing a spurious "Upgrade failed".
      const outcome = deployOutcome(deploy)
      setIntent(outcome === 'success' ? 'held-success'
        : outcome === 'failure' ? 'held-failure' : 'idle')
    }
  }, [running, deploy])

  // While the server reports running, any stale pre-run intent is superseded.
  // Reset it so the post-run edge starts from a clean state.
  useEffect(() => {
    if (running && intent !== 'idle') {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- reset stale pre-run intent in sync with running flag
      setIntent('idle')
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [running])

  // Auto-dismiss a clean success after a brief confirmation window.
  useEffect(() => {
    if (intent !== 'held-success') return
    const t = setTimeout(() => setIntent('idle'), 2000)
    return () => clearTimeout(t)
  }, [intent])

  const doStart = useCallback((targetSha?: string) => {
    setIntent('starting')
    start.mutate(targetSha, { onError: () => setIntent('held-failure') })
  }, [start])

  const dismiss = useCallback(() => setIntent('idle'), [])

  const value: DeployControllerValue = {
    intent,
    visible: intent === 'starting' || running
      || intent === 'held-success' || intent === 'held-failure',
    starting: start.isPending,
    startError: start.isError ? (start.error?.message ?? 'Could not start deploy') : null,
    start: doStart,
    dismiss,
  }
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

/**
 * Assert that a `DeployControllerProvider` is above this point in the tree.
 *
 * `useDeployController()` below deliberately degrades to an inert controller
 * (`visible: false`) when there is no provider, so that leaf components render
 * in unit tests. For a LEAF that is the right trade: it draws nothing and
 * nothing is lost. For the component whose whole job is to host `DeployOverlay`
 * it is the worst possible failure — mounted in the wrong place it would render
 * silently forever, and the symptom would be "a deploy ran and the user saw no
 * progress overlay at all", with nothing in the console to say why.
 *
 * `shell/regions/Overlays` therefore asserts the provider rather than reading
 * through the lenient hook. Same idiom as `useViewport()` and
 * `useSessionRuntimeContext()`: a region whose parent must supply a provider
 * says so by throwing at mount, where a test or the first dev load catches it.
 */
// eslint-disable-next-line react-refresh/only-export-components
export function useRequireDeployController(): void {
  const v = useContext(Ctx)
  if (!v) {
    throw new Error(
      'useRequireDeployController: no DeployControllerProvider above this component. ' +
        'app.overlays hosts DeployOverlay and must render inside DeployControllerProvider.',
    )
  }
}

// eslint-disable-next-line react-refresh/only-export-components
export function useDeployController(): DeployControllerValue {
  const v = useContext(Ctx)
  // Deploy-aware components (DeployPanel) render in many shell unit tests without
  // the provider; degrade to an inert controller rather than throwing. The real app
  // always wraps the tree in DeployControllerProvider at the root (App.tsx).
  return v ?? { intent: 'idle', visible: false, starting: false, startError: null, start: () => {}, dismiss: () => {} }
}
