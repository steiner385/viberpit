import * as React from 'react'
import { useMediaQuery } from '@/lib/useMediaQuery'

export type LayoutMode = 'phone' | 'tablet' | 'desktop' | 'kiosk'
export type WidthTier = 'phone' | 'tablet' | 'desktop' | 'wide'

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
  const atWide = useMediaQuery('(min-width: 1440px)')
  const coarsePointer = useMediaQuery('(pointer: coarse)')

  const value = React.useMemo<Viewport>(() => {
    const width: WidthTier = atWide ? 'wide' : atDesktop ? 'desktop' : atTablet ? 'tablet' : 'phone'
    const natural: LayoutMode = atDesktop ? 'desktop' : atTablet ? 'tablet' : 'phone'
    return {
      mode: kiosk?.enabled ? 'kiosk' : natural,
      width,
      coarsePointer,
      kiosk: kiosk ?? null,
    }
  }, [atTablet, atDesktop, atWide, coarsePointer, kiosk])

  return <ViewportContext.Provider value={value}>{children}</ViewportContext.Provider>
}

// Colocating the hook with its provider is the conventional pattern (see shell/theme.tsx).
// eslint-disable-next-line react-refresh/only-export-components
export function useViewport(): Viewport {
  const v = React.useContext(ViewportContext)
  if (!v) throw new Error('useViewport must be used within a ViewportProvider')
  return v
}
