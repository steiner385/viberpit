// web/src/session/useListNavigation.ts
import { useCallback, useState } from 'react'
import type { Session } from '@/api/types'
import type { NavItem } from './ordering'

export interface ListNavigation {
  highlightedIndex: number
  setHighlightedIndex: (i: number) => void
  highlighted: NavItem | undefined
  highlightedSession: Session | undefined
  highlightedGroupKey: string | undefined
  navigate: (dir: 'up' | 'down') => void
  highlightSession: (id: string) => void
}

/**
 * Owns the keyboard cursor over a list of NavItems (group headers + sessions).
 * Shared by both layouts (DesktopShell, Dashboard) so the header-aware logic
 * lives in one place. The index clamps to the list during render when the list
 * shrinks (mirrors the prior per-layout clamp).
 */
export function useListNavigation(navItems: readonly NavItem[]): ListNavigation {
  const [highlightedIndex, setHighlightedIndex] = useState(-1)

  // Clamp during render when the list shrinks, then derive from the clamped value.
  const clamped = navItems.length === 0 ? -1 : Math.min(highlightedIndex, navItems.length - 1)
  if (clamped !== highlightedIndex) setHighlightedIndex(clamped)

  const highlighted = clamped >= 0 ? navItems[clamped] : undefined
  const highlightedSession = highlighted?.kind === 'session' ? highlighted.session : undefined
  const highlightedGroupKey = highlighted?.kind === 'header' ? highlighted.key : undefined

  const navigate = useCallback((dir: 'up' | 'down') => {
    if (navItems.length === 0) return
    setHighlightedIndex((i) => {
      if (dir === 'up') return i <= 0 ? 0 : i - 1
      return Math.min(navItems.length - 1, i + 1)
    })
  }, [navItems.length])

  const highlightSession = useCallback((id: string) => {
    const idx = navItems.findIndex((it) => it.kind === 'session' && it.session.id === id)
    if (idx >= 0) setHighlightedIndex(idx)
  }, [navItems])

  return {
    highlightedIndex: clamped,
    setHighlightedIndex,
    highlighted,
    highlightedSession,
    highlightedGroupKey,
    navigate,
    highlightSession,
  }
}
