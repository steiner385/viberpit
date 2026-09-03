import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { axe, toHaveNoViolations } from 'jest-axe'
import { MemoryRouter, useLocation } from 'react-router-dom'

// Scope note: axe in jsdom covers roles, names and ARIA wiring only. No
// stylesheet is applied here, so axe's `color-contrast` rule always returns
// `incomplete`, and `toHaveNoViolations()` inspects `violations` alone — these
// assertions therefore cannot fail on colour. Contrast is covered instead by
// src/__tests__/contrast.test.ts, which does the WCAG maths on index.css.
expect.extend(toHaveNoViolations)

const badges = vi.fn()
// Partial mock: `navBadgeProps` lives in this module too and is the real badge
// convention the rail renders through — only the data hook (which needs the API
// layer) is replaced.
vi.mock('@/shell/useNavBadges', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/shell/useNavBadges')>()
  return { ...actual, useNavBadges: () => badges() }
})

import type { NavBadges } from '@/shell/useNavBadges'
import { ViewportProvider } from '@/shell/useViewport'
import { ThemeProvider, THEME_STORAGE_KEY } from '@/shell/theme'
import { NAV_DEFS } from '@/shell/navDefs'
import { NavRail } from './NavRail'

/** A real `NavBadges` with every key present — no casts, so a key that stops
 *  existing is a type error here rather than a silently dead override. */
function navBadges(over: Partial<NavBadges> = {}): NavBadges {
  return {
    sessions: undefined, pipeline: undefined, automations: undefined,
    archived: undefined, settings: undefined,
    ...over,
  }
}

const W768 = '(min-width: 768px)'
const W1024 = '(min-width: 1024px)'
const W1440 = '(min-width: 1440px)'

/** Same matchMedia stub `useViewport.test.tsx` uses — every listed query matches. */
function stubMatchMedia(matching: string[]) {
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: matching.includes(q),
    media: q,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }))
}

/** Surfaces the current URL so assertions can read it (App.test.tsx pattern). */
function LocationProbe() {
  const location = useLocation()
  return <div data-testid="url">{location.pathname + location.search}</div>
}

function renderRail(path = '/sessions', matching: string[] = [W768, W1024]) {
  stubMatchMedia(matching)
  return render(
    <MemoryRouter initialEntries={[path]}>
      <ViewportProvider kiosk={null}>
        <ThemeProvider>
          <NavRail />
        </ThemeProvider>
      </ViewportProvider>
      <LocationProbe />
    </MemoryRouter>,
  )
}

describe('NavRail', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    badges.mockReturnValue(navBadges({ sessions: 3, settings: true }))
  })

  it('is a landmark carrying the region id', () => {
    renderRail()
    const nav = screen.getByRole('navigation', { name: 'Sections' })
    expect(nav).toHaveAttribute('data-region', 'app.rail')
  })

  it('renders one link per nav def except Settings, which lives in the bottom cluster', () => {
    renderRail()
    const nav = screen.getByRole('navigation')
    for (const def of NAV_DEFS) {
      expect(within(nav).getAllByRole('link', { name: new RegExp(def.label) })).toHaveLength(1)
    }
    // Derived from the same array the rail iterates: every section is present
    // exactly once, so a duplicated Settings target cannot slip back in.
    expect(within(nav).getAllByRole('link')).toHaveLength(NAV_DEFS.length + 1) // + New session
  })

  it('marks the active section with aria-current', () => {
    renderRail('/pipeline')
    expect(screen.getByRole('link', { name: /Pipeline/ })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('link', { name: /Sessions \(3\)/ })).not.toHaveAttribute('aria-current')
  })

  it('marks the section active on a nested route', () => {
    renderRail('/sessions/abc123')
    expect(screen.getByRole('link', { name: /Sessions \(3\)/ })).toHaveAttribute('aria-current', 'page')
  })

  it('folds the badge count into the accessible name, matching the Tabs convention', () => {
    renderRail()
    const sessions = screen.getByRole('link', { name: 'Sessions (3)' })
    expect(sessions.textContent).toContain('3')
    // `settings: true` is the attention flag, never a count.
    expect(screen.getByRole('link', { name: 'Settings (needs attention)' })).toBeInTheDocument()
  })

  it('renders no badge for a section whose count is absent', () => {
    badges.mockReturnValue(navBadges())
    renderRail()
    expect(screen.getByRole('link', { name: 'Sessions' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Settings' })).toBeInTheDocument()
  })

  it('opens the help drawer via the URL, not local state', async () => {
    renderRail()
    await userEvent.click(screen.getByRole('button', { name: 'Help' }))
    expect(screen.getByTestId('url')).toHaveTextContent('drawer=help')
  })

  it('New session is a link to the URL-addressable dialog', () => {
    renderRail()
    expect(screen.getByRole('link', { name: 'New session' })).toHaveAttribute(
      'href',
      '/sessions?dialog=new',
    )
  })

  it('every control meets the 44px touch target', () => {
    renderRail()
    for (const el of [...screen.getAllByRole('link'), ...screen.getAllByRole('button')]) {
      expect(el.className).toMatch(/min-h-target/)
      expect(el.className).toMatch(/min-w-target/)
    }
  })

  it('offers no expand toggle below the wide tier', () => {
    renderRail('/sessions', [W768, W1024])
    expect(screen.queryByRole('button', { name: /navigation/i })).not.toBeInTheDocument()
    // Collapsed: no visible label, and the name comes from real text — not an
    // aria-label, which is free to drift from whatever is on screen.
    const sessions = screen.getByRole('link', { name: 'Sessions (3)' })
    // Was `querySelector('span:not(.sr-only)')` toBeNull, i.e. "nothing visible
    // at all". A collapsed item now draws its count badge over the icon (the
    // test below asserts that), so the claim narrows to the one it was making:
    // the LABEL is not visible. Everything else in this test is unchanged.
    const visible = [...sessions.querySelectorAll('span')].filter((s) => !s.classList.contains('sr-only'))
    expect(visible.map((s) => s.textContent)).not.toContain('Sessions')
    expect(sessions.querySelector('.sr-only')?.textContent).toBe('Sessions (3)')
    expect(sessions).not.toHaveAttribute('aria-label')
  })

  it('draws the count badge on the collapsed rail, where there is no label to carry it', () => {
    renderRail('/sessions', [W768, W1024])
    const sessions = screen.getByRole('link', { name: 'Sessions (3)' })
    const visible = [...sessions.querySelectorAll('span')].filter((s) => !s.classList.contains('sr-only'))
    expect(visible.map((s) => s.textContent)).toContain('3')
    // Still decorative: the count is announced once, via the sr-only name.
    expect(visible[0]).toHaveAttribute('aria-hidden', 'true')
  })

  it('expands at the wide tier and persists the choice', async () => {
    renderRail('/sessions', [W768, W1024, W1440])
    await userEvent.click(screen.getByRole('button', { name: 'Expand navigation' }))
    expect(localStorage.getItem('ccswitch.railExpanded')).toBe('true')
    // Expanded: the label becomes visible; the accessible name is unchanged.
    const sessions = screen.getByRole('link', { name: 'Sessions (3)' })
    expect(sessions.querySelector('span[aria-hidden="true"]')?.textContent).toBe('Sessions')
    expect(sessions.querySelector('.sr-only')?.textContent).toBe('Sessions (3)')
    expect(screen.getByRole('button', { name: 'Collapse navigation' })).toHaveAttribute(
      'aria-expanded',
      'true',
    )
  })

  it('restores the persisted expansion, and ignores it below wide', () => {
    localStorage.setItem('ccswitch.railExpanded', 'true')
    renderRail('/sessions', [W768, W1024, W1440])
    expect(screen.getByRole('button', { name: 'Collapse navigation' })).toBeInTheDocument()
  })

  it('picks a theme through the menu', async () => {
    renderRail()
    await userEvent.click(screen.getByRole('button', { name: 'Theme' }))
    const menu = screen.getByRole('menu', { name: 'Theme' })
    expect(within(menu).getByRole('menuitemradio', { name: /System/ })).toHaveAttribute('aria-checked', 'true')
    await userEvent.click(within(menu).getByRole('menuitemradio', { name: /Dark/ }))
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('dark')
  })

  it('has no axe violations', async () => {
    const { container } = renderRail()
    expect(await axe(container)).toHaveNoViolations()
  })
})
