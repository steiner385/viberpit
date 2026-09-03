import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MEDIA, stubMatchMedia } from '@/test/matchMedia'
import { ViewportProvider, useViewport } from './useViewport'

function Probe() {
  const v = useViewport()
  return <span data-testid="v">{`${v.mode}/${v.width}/${v.coarsePointer}`}</span>
}

function renderAt(matching: string[], kiosk: Parameters<typeof ViewportProvider>[0]['kiosk'] = null) {
  stubMatchMedia(matching)
  render(<ViewportProvider kiosk={kiosk}><Probe /></ViewportProvider>)
  return screen.getByTestId('v').textContent
}

const W768 = MEDIA.tablet
const W1024 = MEDIA.desktop
const W1280 = MEDIA.drawerPush
const W1440 = MEDIA.wide
const COARSE = MEDIA.coarsePointer

function DrawerProbe() {
  return <span data-testid="push">{String(useViewport().drawerPushes)}</span>
}

function drawerPushesAt(matching: string[]) {
  stubMatchMedia(matching)
  render(<ViewportProvider kiosk={null}><DrawerProbe /></ViewportProvider>)
  return screen.getByTestId('push').textContent
}

describe('useViewport', () => {
  beforeEach(() => { vi.unstubAllGlobals() })

  it('defaults to phone with no matchMedia (jsdom)', () => {
    vi.stubGlobal('matchMedia', undefined)
    render(<ViewportProvider kiosk={null}><Probe /></ViewportProvider>)
    expect(screen.getByTestId('v').textContent).toBe('phone/phone/false')
  })

  it('below 768 is phone', () => {
    expect(renderAt([])).toBe('phone/phone/false')
  })

  it('768–1023 is tablet', () => {
    expect(renderAt([W768])).toBe('tablet/tablet/false')
  })

  it('1024–1439 is desktop', () => {
    expect(renderAt([W768, W1024])).toBe('desktop/desktop/false')
  })

  it('1440+ is desktop at the wide tier', () => {
    expect(renderAt([W768, W1024, W1440])).toBe('desktop/wide/false')
  })

  it('reports a coarse pointer', () => {
    expect(renderAt([COARSE])).toBe('phone/phone/true')
  })

  it('kiosk overrides the mode at any width but keeps the width tier', () => {
    expect(renderAt([W768, W1024, W1440], { enabled: true, cycleSeconds: 30, pages: ['pipeline'] }))
      .toBe('kiosk/wide/false')
  })

  it('a disabled kiosk config does not override the mode', () => {
    expect(renderAt([W768, W1024], { enabled: false, cycleSeconds: 30, pages: ['pipeline'] }))
      .toBe('desktop/desktop/false')
  })

  // D13's drawer threshold sits BETWEEN the tier breakpoints: the drawer pushes
  // from 1280, while `width` only changes at 1024 and 1440. These pin the gap —
  // a consumer deriving push from `width === 'wide'` would be wrong for the
  // whole 1280–1439 band, which is where most laptops sit.
  it('the drawer overlays below 1280', () => {
    expect(drawerPushesAt([W768, W1024])).toBe('false')
  })

  it('the drawer pushes from 1280, below the wide tier', () => {
    expect(drawerPushesAt([W768, W1024, W1280])).toBe('true')
  })

  it('the drawer pushes at the wide tier', () => {
    expect(drawerPushesAt([W768, W1024, W1280, W1440])).toBe('true')
  })

  it('throws when used outside the provider', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    expect(() => render(<Probe />)).toThrow(/useViewport must be used within/)
    spy.mockRestore()
  })
})

import { readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

/**
 * useViewport is the only LAYOUT matchMedia reader. This walks the source and
 * fails if a new min-width/max-width query appears outside it — whether read
 * via a raw `matchMedia(...)` call or through the `useMediaQuery(...)` wrapper.
 * Non-layout queries (motion, pointer, colour-scheme) are listed as deliberate
 * exemptions — see DEC-8 in the plan. Adding a file here must be a decision,
 * not a reflex.
 *
 * The walk root is derived from this test file's own location (via
 * import.meta.url), NOT process.cwd(). process.cwd() differs by invocation:
 * `npm --prefix web test` runs with cwd `web/`, but
 * `npm --prefix web exec -- vitest run --root web ...` (as used to run this
 * file in isolation) runs with cwd at the repo root — which itself contains
 * an unrelated `src/` (the Python package, zero .ts/.tsx files). Keying off
 * process.cwd() would have made this guard pass vacuously — walking the wrong
 * tree and finding nothing to flag — under that second invocation.
 */
const LAYOUT_QUERY = /(?:matchMedia|useMediaQuery)\(\s*['"`]\((?:min|max)-width/
const EXEMPT = new Set<string>([
  'src/shell/useViewport.tsx',
  // Takes `query` as a parameter, not a literal, so this never matches the
  // regex above — kept here defensively as the generic subscription primitive.
  'src/lib/useMediaQuery.ts',
  // App.tsx's shell fork reads the same 1024px breakpoint useViewport now owns.
  // This exemption is deleted at Task 17, when AppShell replaces the fork.
  'src/App.tsx',
  // SessionCard's swipe-overflow clipping reads 1024px directly. Not part of
  // this PR's scope; outlives it until a later PR migrates it to useViewport.
  'src/components/SessionCard.tsx',
])

function walk(dir: string, out: string[] = []): string[] {
  for (const e of readdirSync(dir)) {
    const p = join(dir, e)
    if (statSync(p).isDirectory()) { walk(p, out); continue }
    if (/\.(ts|tsx)$/.test(e) && !/\.test\.tsx?$/.test(e)) out.push(p)
  }
  return out
}

it('no module outside useViewport reads a layout media query', () => {
  const here = dirname(fileURLToPath(import.meta.url)) // .../web/src/shell
  const srcRoot = dirname(here) // .../web/src
  const offenders = walk(srcRoot)
    .filter((p) => LAYOUT_QUERY.test(readFileSync(p, 'utf8')))
    .map((p) => p.slice(dirname(srcRoot).length + 1))
    .filter((rel) => !EXEMPT.has(rel))
  expect(offenders, offenders.join('\n')).toEqual([])
})
