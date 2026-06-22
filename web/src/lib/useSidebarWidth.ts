import { useCallback, useEffect, useState } from 'react'
import { clampSidebarWidth, loadSidebarWidth, saveSidebarWidth } from './sidebarWidth'

/**
 * Sidebar width state, restored from and persisted to localStorage. Returned as
 * a [value, setter] pair; the setter always clamps to the allowed band.
 */
export function useSidebarWidth() {
  const [width, setWidthRaw] = useState(loadSidebarWidth)
  const setWidth = useCallback((px: number) => setWidthRaw(clampSidebarWidth(px)), [])
  useEffect(() => { saveSidebarWidth(width) }, [width])
  return [width, setWidth] as const
}
