/**
 * Settings › Appearance on the chassis (spec §7.2, task 16).
 *
 * This is the IMMEDIATE-apply section: there is no draft, no SaveBar and no
 * mocked transport — every row writes its own browser-scoped store on change,
 * so the assertions are on `localStorage` and on `<html data-theme>` directly.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ThemeProvider, THEME_STORAGE_KEY } from '@/shell/theme'
import { RAIL_EXPANDED_KEY } from '@/shell/railExpanded'
import { NOTIFY_PREF_KEY } from '@/pipeline/notifyPref'
import { SIDEBAR_STORAGE_KEY, SIDEBAR_DEFAULT } from '@/lib/sidebarWidth'
import { stubMatchMedia } from '@/test/matchMedia'
import { AppearanceSection } from './AppearanceSection'

function mount() {
  stubMatchMedia([])
  return render(<ThemeProvider><AppearanceSection /></ThemeProvider>)
}

describe('AppearanceSection', () => {
  it('theme radio writes ccswitch.theme and reports Saved; system removes the key', async () => {
    mount()
    expect(screen.getByRole('heading', { level: 2, name: 'Appearance' })).toBeInTheDocument()
    expect(screen.getByText('This browser')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('radio', { name: 'Dark' }))
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('dark')
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')
    expect(screen.getAllByRole('status')[0]).toHaveTextContent('Saved')
    await userEvent.click(screen.getByRole('radio', { name: 'Use system setting' }))
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBeNull()
  })

  it('switches apply immediately to their stores', async () => {
    mount()
    await userEvent.click(screen.getByRole('switch', { name: 'Show navigation labels' }))
    expect(localStorage.getItem(RAIL_EXPANDED_KEY)).toBe('true')
    await userEvent.click(screen.getByRole('switch', { name: 'Pipeline notifications' }))
    expect(localStorage.getItem(NOTIFY_PREF_KEY)).toBe('true')
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()
  })

  it('layout widths show the stored value and Reset restores the default', async () => {
    localStorage.setItem(SIDEBAR_STORAGE_KEY, '400')
    mount()
    expect(screen.getByText('400 px')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Reset session list width' }))
    expect(screen.getByText(`${SIDEBAR_DEFAULT} px`)).toBeInTheDocument()
    expect(localStorage.getItem(SIDEBAR_STORAGE_KEY)).toBe(String(SIDEBAR_DEFAULT))
  })
})
