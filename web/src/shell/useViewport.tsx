/* eslint-disable react-refresh/only-export-components -- mixed export file: the
   provider component lives beside the hook that reads it and the mode/tier
   tables it is built from. Same convention as shell/theme.tsx and
   shell/InteractionContext.tsx. */
import * as React from 'react'
import { useMediaQuery } from '@/lib/useMediaQuery'

/**
 * Every layout mode, as a value — `AppShell`'s region-visibility matrix has to
 * enumerate them (7 regions × every mode), and a hand-written list in the test
 * would go stale the day a mode is added, leaving the new mode's whole column
 * unasserted while the suite stayed green. `LayoutMode` is derived from it, the
 * same way `NavId` is derived from `NAV_DEFS` (ruling R6), so the value and the
 * type cannot disagree.
 */
export const LAYOUT_MODES = ['phone', 'tablet', 'desktop', 'kiosk'] as const
export type LayoutMode = (typeof LAYOUT_MODES)[number]

export const WIDTH_TIERS = ['phone', 'tablet', 'desktop', 'wide'] as const
export type WidthTier = (typeof WIDTH_TIERS)[number]

/** The kiosk half of the viewport, supplied by AppShell (see shell/kiosk.ts). */
export interface ViewportKiosk {
  enabled: boolean
  cycleSeconds: number
  pages: readonly string[]
}

export interface Viewport {
  mode: LayoutMode
  /** Always the real width tier, even in kiosk — kiosk layouts use it for column counts. */
  width: WidthTier
  /**
   * `app.drawer` is a grid column (`Sheet mode="push"`) rather than an overlay.
   *
   * D13's threshold — `width === 'wide' || (desktop && ≥1280)` — falls BETWEEN
   * the tier breakpoints, so it cannot be derived from `width` by a consumer.
   * It is answered here rather than by `DrawerHost` because this module is the
   * only permitted layout `matchMedia` reader (see the guard in
   * `useViewport.test.tsx`), and because a threshold owned by its one consumer
   * is a threshold the next consumer copies slightly wrong.
   */
  drawerPushes: boolean
  coarsePointer: boolean
  kiosk: ViewportKiosk | null
}

const ViewportContext = React.createContext<Viewport | null>(null)

/**
 * The ONLY module that reads matchMedia for layout (umbrella §4.1).
 *
 * Deliberately exempt, because they are not layout queries and two of them are
 * plain functions that cannot consume a React context:
 *   - session/brailleSpinner.ts, pipeline/motion.ts  (prefers-reduced-motion)
 *   - components/BrowserTab.tsx                      (pointer: coarse, for touch UI)
 *   - shell/theme.tsx                                (prefers-color-scheme)
 */
export function ViewportProvider({
  kiosk,
  children,
}: {
  kiosk: ViewportKiosk | null
  children: React.ReactNode
}) {
  const atTablet = useMediaQuery('(min-width: 768px)')
  const atDesktop = useMediaQuery('(min-width: 1024px)')
  const atDrawerPush = useMediaQuery('(min-width: 1280px)')
  const atWide = useMediaQuery('(min-width: 1440px)')
  const coarsePointer = useMediaQuery('(pointer: coarse)')

  const value = React.useMemo<Viewport>(() => {
    const width: WidthTier = atWide ? 'wide' : atDesktop ? 'desktop' : atTablet ? 'tablet' : 'phone'
    const natural: LayoutMode = atDesktop ? 'desktop' : atTablet ? 'tablet' : 'phone'
    return {
      mode: kiosk?.enabled ? 'kiosk' : natural,
      width,
      // `wide` is ≥1440, so it is already inside ≥1280; the disjunction in D13
      // collapses to the one query rather than needing both.
      drawerPushes: atDrawerPush,
      coarsePointer,
      kiosk: kiosk ?? null,
    }
  }, [atTablet, atDesktop, atDrawerPush, atWide, coarsePointer, kiosk])

  return <ViewportContext.Provider value={value}>{children}</ViewportContext.Provider>
}

// Colocating the hook with its provider is the conventional pattern (see shell/theme.tsx).
export function useViewport(): Viewport {
  const v = React.useContext(ViewportContext)
  if (!v) throw new Error('useViewport must be used within a ViewportProvider')
  return v
}
