// Originally ported from plotroom@5166b52 frontend/src/kiosk.ts; moved here from
// web/src/pipeline/kiosk.ts and extended with `pages` and `theme` (sub-spec B §5).
import { NAV_DEFS, type NavId } from './navDefs'
import type { ThemeChoice } from './theme'

export const DEFAULT_CYCLE_SECONDS = 30
export const MIN_CYCLE_SECONDS = 10
const DEFAULT_PAGES: readonly NavId[] = ['pipeline']

export interface KioskConfig {
  enabled: boolean
  /** Seconds each page stays on screen before the attract cycle advances. */
  cycleSeconds: number
  /** Pages the attract cycle rotates through; always kiosk-safe and non-empty. */
  pages: readonly NavId[]
  /** Pins the theme for a wall display. `system` is not accepted — a wall
   *  display has no user to follow. */
  theme?: ThemeChoice
}

const KIOSK_SAFE = new Set<string>(NAV_DEFS.filter((d) => d.showInKiosk).map((d) => d.id))

function parsePages(raw: string | null): readonly NavId[] {
  if (raw === null) return DEFAULT_PAGES
  const seen = new Set<string>()
  const out: NavId[] = []
  for (const part of raw.split(',')) {
    const id = part.trim()
    if (!KIOSK_SAFE.has(id) || seen.has(id)) continue
    seen.add(id)
    out.push(id as NavId)
  }
  return out.length > 0 ? out : DEFAULT_PAGES
}

function parseCycle(raw: string | null): number {
  if (raw === null || raw.trim() === '') return DEFAULT_CYCLE_SECONDS
  const n = Number(raw)
  if (!Number.isFinite(n)) return DEFAULT_CYCLE_SECONDS
  return Math.max(MIN_CYCLE_SECONDS, Math.round(n))
}

function parseTheme(raw: string | null): ThemeChoice | undefined {
  return raw === 'light' || raw === 'dark' ? raw : undefined
}

/**
 * Parse `?kiosk=1&pages=a,b&cycle=N&theme=light|dark`.
 *
 * Read once at AppShell mount — changing the URL requires a reload, which is
 * exactly what a wall display does anyway.
 */
export function readKioskConfig(search: string = window.location.search): KioskConfig {
  const p = new URLSearchParams(search)
  return {
    enabled: p.get('kiosk') === '1',
    cycleSeconds: parseCycle(p.get('cycle')),
    pages: parsePages(p.get('pages')),
    theme: parseTheme(p.get('theme')),
  }
}
