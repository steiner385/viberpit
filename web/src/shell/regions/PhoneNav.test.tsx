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
// convention under test — only the data hook (which needs the API layer) is
// replaced.
vi.mock('@/shell/useNavBadges', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/shell/useNavBadges')>()
  return { ...actual, useNavBadges: () => badges() }
})

import type { NavBadges } from '@/shell/useNavBadges'
import { ThemeProvider } from '@/shell/theme'
import { NAV_DEFS } from '@/shell/navDefs'
import { NavBar } from './NavBar'
import { TopBar } from './TopBar'

/** A real `NavBadges` with every key present — no casts, so a key that stops
 *  existing is a type error here rather than a silently dead override. */
function navBadges(over: Partial<NavBadges> = {}): NavBadges {
  return {
    sessions: undefined, pipeline: undefined, automations: undefined,
    archived: undefined, settings: undefined,
    ...over,
  }
}

/** Surfaces the current URL so assertions can read it (App.test.tsx pattern). */
function LocationProbe() {
  const location = useLocation()
  return <div data-testid="url">{location.pathname + location.search}</div>
}

/** Both phone rails together — the arrangement the shell mounts on phone. */
function renderPhoneNav(path = '/sessions') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <ThemeProvider>
        <TopBar />
        <NavBar />
      </ThemeProvider>
      <LocationProbe />
    </MemoryRouter>,
  )
}

const PRIMARY = NAV_DEFS.slice(0, 4)
const OVERFLOW = NAV_DEFS.slice(4)

const bar = () => screen.getByRole('navigation', { name: 'Sections' })
const topBar = () => screen.getByRole('banner')

/** Text a sighted user actually sees inside a control — `sr-only` nodes carry
 *  the accessible name and must not be mistaken for a visible badge. */
const visibleText = (el: HTMLElement) =>
  [...el.querySelectorAll('span')].filter((s) => !s.classList.contains('sr-only')).map((s) => s.textContent)

/** The rendered badge nodes: visible spans that are decorative (`aria-hidden`). */
const visibleBadges = (el: HTMLElement) =>
  [...el.querySelectorAll<HTMLElement>('span[aria-hidden="true"]')].filter(
    (s) => !s.classList.contains('sr-only'),
  )

async function openMore() {
  await userEvent.click(within(bar()).getByRole('button', { name: /^More/ }))
  return screen.getByRole('dialog', { name: 'More' })
}

describe('NavBar', () => {
  beforeEach(() => {
    badges.mockReturnValue(navBadges({ sessions: 3, settings: true }))
  })

  it('renders exactly the first four sections as links, in order', () => {
    renderPhoneNav()
    const links = within(bar()).getAllByRole('link')
    expect(links.map((el) => el.getAttribute('href'))).toEqual(PRIMARY.map((d) => d.path))
  })

  it('offers More as a button, not a link', () => {
    renderPhoneNav()
    expect(within(bar()).getByRole('button', { name: /^More/ })).toBeInTheDocument()
    expect(within(bar()).queryByRole('link', { name: /More/ })).toBeNull()
  })

  it('folds the badges of the sections it hides into More', () => {
    // archived (2) is behind More; settings' attention flag is too. Sessions (3)
    // is on the bar, so it must NOT be counted twice.
    badges.mockReturnValue(navBadges({ sessions: 3, archived: 2, settings: true }))
    renderPhoneNav()
    expect(within(bar()).getByRole('button', { name: 'More (2, needs attention)' })).toBeInTheDocument()
  })

  it('shows a plain More when nothing behind it is badged', () => {
    badges.mockReturnValue(navBadges({ sessions: 3 }))
    renderPhoneNav()
    expect(within(bar()).getByRole('button', { name: 'More' })).toBeInTheDocument()
  })

  // The bar is label-less, so the badge is the ONLY pixel a sighted phone user
  // gets. `ui/Tabs.tsx` (the strip this shell replaces, still rendered on mobile
  // by Dashboard) draws the count unconditionally — losing it here would be a
  // visible regression for every phone user, not a cosmetic one.
  it('draws a visible count badge on the phone bar, not only in the accessible name', () => {
    badges.mockReturnValue(navBadges({ sessions: 3 }))
    renderPhoneNav()
    const sessions = within(bar()).getByRole('link', { name: 'Sessions (3)' })
    expect(visibleText(sessions)).toContain('3')
  })

  it('draws the attention dot on the phone bar', () => {
    // `pipeline: true` is the attention shape (`navBadgeProps` maps any non-number
    // that is `true`), on a section that sits on the bar rather than behind More.
    badges.mockReturnValue(navBadges({ pipeline: true }))
    renderPhoneNav()
    const pipeline = within(bar()).getByRole('link', { name: 'Pipeline (needs attention)' })
    expect(pipeline.querySelector('span.bg-warning')).not.toBeNull()
  })

  it('draws one amber count badge when a bar control is both counted and needing attention', () => {
    // More hides Archived (2) and Settings (attention): one indicator carrying
    // the count in the attention tone, not a count and a dot fighting for the
    // same corner of the icon.
    badges.mockReturnValue(navBadges({ archived: 2, settings: true }))
    renderPhoneNav()
    const more = within(bar()).getByRole('button', { name: 'More (2, needs attention)' })
    const indicators = visibleBadges(more)
    expect(indicators).toHaveLength(1)
    expect(indicators[0].textContent).toBe('2')
    expect(indicators[0].className).toContain('text-warning')
  })

  it('carries Settings’ attention flag onto More, and shows it inline in the sheet', async () => {
    // `settings` is the boolean-badge path (never a count) and it lives behind
    // More, so both the label-less overlay form and the labelled inline form of
    // the same flag are exercised here.
    badges.mockReturnValue(navBadges({ settings: true }))
    renderPhoneNav()
    const more = within(bar()).getByRole('button', { name: 'More (needs attention)' })
    expect(more.querySelector('span.bg-warning')?.className).toContain('absolute')

    const sheet = await openMore()
    const settings = within(sheet).getByRole('link', { name: 'Settings (needs attention)' })
    expect(settings.querySelector('span.bg-warning')?.className).not.toContain('absolute')
  })

  it('lists every remaining section, Settings and the global actions in the More sheet', async () => {
    renderPhoneNav()
    const sheet = await openMore()
    for (const def of OVERFLOW) {
      expect(within(sheet).getAllByRole('link', { name: new RegExp(def.label) })).toHaveLength(1)
    }
    // Derived from the same array NavBar slices: bar links + sheet links cover
    // every section exactly once, so a section can never become unreachable.
    expect(within(sheet).getAllByRole('link')).toHaveLength(OVERFLOW.length + 1) // + New session
    for (const name of ['New session', 'Assistant', 'Help', 'Theme']) {
      expect(within(sheet).getByRole(name === 'New session' ? 'link' : 'button', { name })).toBeInTheDocument()
    }
  })

  it('marks the active section with aria-current, including a nested route', () => {
    renderPhoneNav('/sessions/abc123')
    expect(within(bar()).getByRole('link', { name: 'Sessions (3)' })).toHaveAttribute('aria-current', 'page')
    expect(within(bar()).getByRole('link', { name: 'Pipeline' })).not.toHaveAttribute('aria-current')
  })

  it('marks a section active inside the More sheet when the route is behind More', async () => {
    renderPhoneNav('/insights')
    for (const el of within(bar()).getAllByRole('link')) {
      expect(el).not.toHaveAttribute('aria-current')
    }
    const sheet = await openMore()
    expect(within(sheet).getByRole('link', { name: 'Insights' })).toHaveAttribute('aria-current', 'page')
  })

  it('pads its bottom by the safe-area inset so the bar clears the gesture bar', () => {
    renderPhoneNav()
    // The mechanism the rest of the mobile chrome uses (App.tsx header,
    // Composer, DispatchBar): an inline env() padding, not a class — an
    // arbitrary-value class is banned in shell/** by ccswitch/no-raw-palette.
    expect(bar().style.paddingBottom).toContain('env(safe-area-inset-bottom)')
  })

  it('every control meets the 44px touch target', () => {
    renderPhoneNav()
    for (const el of [...within(bar()).getAllByRole('link'), ...within(bar()).getAllByRole('button')]) {
      expect(el.className).toMatch(/min-h-target/)
      expect(el.className).toMatch(/min-w-target/)
    }
  })

  it('has no axe violations', async () => {
    const { container } = renderPhoneNav()
    expect(await axe(container)).toHaveNoViolations()
  })
})

describe('TopBar', () => {
  beforeEach(() => {
    badges.mockReturnValue(navBadges())
  })

  it('renders the brand and the Assistant and Help controls', () => {
    renderPhoneNav()
    expect(within(topBar()).getByText('ccswitch')).toBeInTheDocument()
    expect(within(topBar()).getByRole('button', { name: 'Assistant' })).toBeInTheDocument()
    expect(within(topBar()).getByRole('button', { name: 'Help' })).toBeInTheDocument()
  })

  it('renders no h1 — the page title is the page’s own heading', () => {
    renderPhoneNav()
    expect(within(topBar()).queryByRole('heading', { level: 1 })).toBeNull()
  })

  it('opens a drawer via the URL, not local state', async () => {
    renderPhoneNav()
    await userEvent.click(within(topBar()).getByRole('button', { name: 'Help' }))
    expect(screen.getByTestId('url')).toHaveTextContent('drawer=help')
  })

  it('every control meets the 44px touch target', () => {
    renderPhoneNav()
    for (const el of within(topBar()).getAllByRole('button')) {
      expect(el.className).toMatch(/min-h-target/)
      expect(el.className).toMatch(/min-w-target/)
    }
  })
})

describe('the phone rails together', () => {
  beforeEach(() => {
    badges.mockReturnValue(navBadges())
  })

  it('both carry data-region="app.rail" with distinct data-rail values', () => {
    const { container } = renderPhoneNav()
    const rails = [...container.querySelectorAll('[data-region="app.rail"]')]
    expect(rails).toHaveLength(2)
    expect(rails.map((el) => el.getAttribute('data-rail')).sort()).toEqual(['bottom', 'top'])
  })
})
