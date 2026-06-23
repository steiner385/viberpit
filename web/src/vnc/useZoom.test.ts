import { describe, it, expect } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { clampZoom, isZoomed, zoomLabel, useZoom, ZOOM_MIN, ZOOM_MAX } from './useZoom'

describe('zoom pure helpers', () => {
  it('clamps to [1,3]', () => {
    expect(clampZoom(0.2)).toBe(ZOOM_MIN)
    expect(clampZoom(9)).toBe(ZOOM_MAX)
    expect(clampZoom(1.4)).toBeCloseTo(1.4)
  })
  it('isZoomed uses the 1.02 epsilon', () => {
    expect(isZoomed(1.0)).toBe(false)
    expect(isZoomed(1.02)).toBe(false)
    expect(isZoomed(1.03)).toBe(true)
  })
  it('labels current state, never the action', () => {
    expect(zoomLabel(1.0)).toBe('Fit')
    expect(zoomLabel(1.4)).toBe('140%')
  })
})

describe('useZoom', () => {
  it('steps, clamps, and resets', () => {
    const { result } = renderHook(() => useZoom())
    expect(result.current.zoom).toBe(ZOOM_MIN)
    expect(result.current.zoomed).toBe(false)
    act(() => result.current.zoomIn())
    expect(result.current.zoom).toBeCloseTo(1.1)
    expect(result.current.label).toBe('110%')
    act(() => result.current.setZoom(99))
    expect(result.current.zoom).toBe(ZOOM_MAX)
    act(() => result.current.resetFit())
    expect(result.current.zoom).toBe(ZOOM_MIN)
    expect(result.current.zoomed).toBe(false)
  })
})
