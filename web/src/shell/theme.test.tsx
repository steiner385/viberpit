import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import { ThemeProvider, useTheme, readStoredTheme, applyTheme, THEME_STORAGE_KEY } from './theme'

function Probe() {
  const { choice, resolved, setChoice } = useTheme()
  return (
    <div>
      <span data-testid="choice">{choice}</span>
      <span data-testid="resolved">{resolved}</span>
      <button onClick={() => setChoice('dark')}>dark</button>
      <button onClick={() => setChoice('system')}>system</button>
    </div>
  )
}

beforeEach(() => {
  localStorage.clear()
  document.documentElement.removeAttribute('data-theme')
  document.head.innerHTML =
    '<meta name="theme-color" media="(prefers-color-scheme: light)" content="#ffffff">' +
    '<meta name="theme-color" media="(prefers-color-scheme: dark)" content="#16171d">'
})

describe('theme', () => {
  it('defaults to system and stamps nothing', () => {
    render(<ThemeProvider><Probe /></ThemeProvider>)
    expect(screen.getByTestId('choice').textContent).toBe('system')
    expect(document.documentElement.hasAttribute('data-theme')).toBe(false)
  })
  it('setChoice persists and stamps data-theme', () => {
    render(<ThemeProvider><Probe /></ThemeProvider>)
    act(() => screen.getByText('dark').click())
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('dark')
    expect(screen.getByTestId('resolved').textContent).toBe('dark')
    act(() => screen.getByText('system').click())
    expect(document.documentElement.hasAttribute('data-theme')).toBe(false)
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBeNull()
  })
  it('readStoredTheme: ?theme= overrides storage; garbage falls back to system', () => {
    localStorage.setItem(THEME_STORAGE_KEY, 'light')
    expect(readStoredTheme('')).toBe('light')
    expect(readStoredTheme('?theme=dark')).toBe('dark')
    localStorage.setItem(THEME_STORAGE_KEY, 'purple')
    expect(readStoredTheme('')).toBe('system')
  })
  it('applyTheme pins both theme-color metas to the chosen scheme and restores them for system', () => {
    applyTheme('dark')
    const metas = [...document.querySelectorAll<HTMLMetaElement>('meta[name="theme-color"]')]
    expect(metas.map((m) => m.content)).toEqual(['#16171d', '#16171d'])
    applyTheme('system')
    expect(metas.map((m) => m.content)).toEqual(['#ffffff', '#16171d'])
  })
  it('resolved follows matchMedia when choice is system', () => {
    vi.stubGlobal('matchMedia', (q: string) => ({
      matches: q.includes('dark'), media: q, addEventListener() {}, removeEventListener() {},
      addListener() {}, removeListener() {}, onchange: null, dispatchEvent: () => false,
    }))
    render(<ThemeProvider><Probe /></ThemeProvider>)
    expect(screen.getByTestId('resolved').textContent).toBe('dark')
    vi.unstubAllGlobals()
  })
})
