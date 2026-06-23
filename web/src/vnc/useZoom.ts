import { useCallback, useState } from 'react'

export const ZOOM_MIN = 1.0
export const ZOOM_MAX = 3.0
export const ZOOM_STEP = 0.1
export const FIT_EPSILON = 1.02

export function clampZoom(z: number): number {
  if (Number.isNaN(z)) return ZOOM_MIN
  return Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, z))
}
export function isZoomed(z: number): boolean { return z > FIT_EPSILON }
export function zoomLabel(z: number): string {
  return isZoomed(z) ? `${Math.round(z * 100)}%` : 'Fit'
}

export function useZoom() {
  const [zoom, setZoomState] = useState(ZOOM_MIN)
  const setZoom = useCallback((z: number) => setZoomState(clampZoom(z)), [])
  const zoomIn = useCallback(() => setZoomState((z) => clampZoom(z + ZOOM_STEP)), [])
  const zoomOut = useCallback(() => setZoomState((z) => clampZoom(z - ZOOM_STEP)), [])
  const resetFit = useCallback(() => setZoomState(ZOOM_MIN), [])
  return { zoom, zoomIn, zoomOut, setZoom, resetFit, zoomed: isZoomed(zoom), label: zoomLabel(zoom) }
}
