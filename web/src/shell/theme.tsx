import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { useMediaQuery } from '@/lib/useMediaQuery'

export type ThemeChoice = 'system' | 'light' | 'dark'
export const THEME_STORAGE_KEY = 'ccswitch.theme'

// Must match the two <meta name="theme-color"> entries in index.html and the
// --t-canvas values in index.css.
const CANVAS = { light: '#ffffff', dark: '#16171d' } as const

function isChoice(v: unknown): v is ThemeChoice {
  return v === 'system' || v === 'light' || v === 'dark'
}

/** ?theme= (kiosk pins it per load) beats the stored choice; anything else → system. */
// Colocating these helpers with the provider is the conventional pattern.
// eslint-disable-next-line react-refresh/only-export-components
export function readStoredTheme(search: string = typeof location !== 'undefined' ? location.search : ''): ThemeChoice {
  const fromUrl = new URLSearchParams(search).get('theme')
  if (isChoice(fromUrl) && fromUrl !== 'system') return fromUrl
  try {
    const stored = localStorage.getItem(THEME_STORAGE_KEY)
    if (isChoice(stored)) return stored
  } catch { /* storage unavailable */ }
  return 'system'
}

/** Stamp <html data-theme> and pin the browser-chrome color to the chosen scheme. */
// eslint-disable-next-line react-refresh/only-export-components
export function applyTheme(choice: ThemeChoice): void {
  const root = document.documentElement
  if (choice === 'system') root.removeAttribute('data-theme')
  else root.setAttribute('data-theme', choice)
  const metas = document.querySelectorAll<HTMLMetaElement>('meta[name="theme-color"]')
  metas.forEach((m) => {
    if (choice === 'system') {
      // jsdom (and older WebKit) don't implement the `.media` IDL attribute on
      // HTMLMetaElement, so read the attribute directly rather than the property.
      m.content = (m.getAttribute('media') ?? '').includes('dark') ? CANVAS.dark : CANVAS.light
    } else {
      m.content = CANVAS[choice]
    }
  })
}

interface ThemeContextValue {
  choice: ThemeChoice
  resolved: 'light' | 'dark'
  setChoice: (c: ThemeChoice) => void
}
const ThemeContext = createContext<ThemeContextValue | null>(null)

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [choice, setChoiceState] = useState<ThemeChoice>(readStoredTheme)
  const systemDark = useMediaQuery('(prefers-color-scheme: dark)')

  useEffect(() => { applyTheme(choice) }, [choice])

  const setChoice = useCallback((c: ThemeChoice) => {
    setChoiceState(c)
    try {
      if (c === 'system') localStorage.removeItem(THEME_STORAGE_KEY)
      else localStorage.setItem(THEME_STORAGE_KEY, c)
    } catch { /* storage unavailable */ }
  }, [])

  const value = useMemo<ThemeContextValue>(() => ({
    choice,
    resolved: choice === 'system' ? (systemDark ? 'dark' : 'light') : choice,
    setChoice,
  }), [choice, systemDark, setChoice])

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

// eslint-disable-next-line react-refresh/only-export-components
export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext)
  if (!ctx) throw new Error('useTheme must be used inside <ThemeProvider>')
  return ctx
}
