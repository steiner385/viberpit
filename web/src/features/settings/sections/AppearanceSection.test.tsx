/**
 * Settings › Appearance on the chassis (spec §7.2, task 16).
 *
 * This is the IMMEDIATE-apply section: there is no draft, no SaveBar and no
 * mocked transport — every row writes its own browser-scoped store on change,
 * so the assertions are on `localStorage` and on `<html data-theme>` directly.
 */
import type { ReactNode } from 'react'
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { axe, toHaveNoViolations } from 'jest-axe'
import { ThemeProvider, THEME_STORAGE_KEY } from '@/shell/theme'
import { RAIL_EXPANDED_KEY } from '@/shell/railExpanded'
import { NOTIFY_PREF_KEY } from '@/pipeline/notifyPref'
import { SIDEBAR_STORAGE_KEY, SIDEBAR_DEFAULT } from '@/lib/sidebarWidth'
import { useSidebarWidth } from '@/lib/useSidebarWidth'
import { stubMatchMedia } from '@/test/matchMedia'
import { AppearanceSection } from './AppearanceSection'

expect.extend(toHaveNoViolations)

/**
 * Stands in for the live list column (`shell/regions/ContextList`), which holds
 * the same width store: R15 is that a Reset here moves THAT subscriber, not just
 * the stored key.
 */
function ListColumnProbe() {
  const [width] = useSidebarWidth()
  // A plain span, not an <output>: <output> carries an implicit role="status",
  // which would join the rows' live regions and shift `getAllByRole('status')`.
  return <span data-testid="live-list-width">{width}</span>
}

function mount(extra?: ReactNode) {
  stubMatchMedia([])
  return render(<ThemeProvider><AppearanceSection />{extra}</ThemeProvider>)
}

describe('AppearanceSection', () => {
  it('theme radio writes ccswitch.theme and reports Saved; system removes the key', async () => {
    const { container } = mount()
    expect(screen.getByRole('heading', { level: 2, name: 'Appearance' })).toBeInTheDocument()
    expect(screen.getByText('This browser')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('radio', { name: 'Dark' }))
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('dark')
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')
    expect(screen.getAllByRole('status')[0]).toHaveTextContent('Saved')
    await userEvent.click(screen.getByRole('radio', { name: 'Use system setting' }))
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBeNull()
    expect(await axe(container)).toHaveNoViolations()
  })

  it('switches apply immediately to their stores', async () => {
    mount()
    await userEvent.click(screen.getByRole('switch', { name: 'Show navigation labels' }))
    expect(localStorage.getItem(RAIL_EXPANDED_KEY)).toBe('true')
    await userEvent.click(screen.getByRole('switch', { name: 'Pipeline notifications' }))
    expect(localStorage.getItem(NOTIFY_PREF_KEY)).toBe('true')
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()
  })

  it('layout widths show the stored value and Reset restores the default for every subscriber (R15)', async () => {
    localStorage.setItem(SIDEBAR_STORAGE_KEY, '400')
    mount(<ListColumnProbe />)
    expect(screen.getByText('400 px')).toBeInTheDocument()
    expect(screen.getByTestId('live-list-width')).toHaveTextContent('400')
    await userEvent.click(screen.getByRole('button', { name: 'Reset session list width' }))
    expect(screen.getByText(`${SIDEBAR_DEFAULT} px`)).toBeInTheDocument()
    // The live column moved too — not just the stored key.
    expect(screen.getByTestId('live-list-width')).toHaveTextContent(String(SIDEBAR_DEFAULT))
    expect(localStorage.getItem(SIDEBAR_STORAGE_KEY)).toBe(String(SIDEBAR_DEFAULT))
  })
})
