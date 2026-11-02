import { describe, it, expect, afterEach } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import { useContainerMinWidth } from './useContainerMinWidth'
import { setElementWidth } from '@/test/elementWidth'

function Probe({ minRem }: { minRem: number }) {
  const [ref, matches] = useContainerMinWidth<HTMLDivElement>(minRem)
  return <div ref={ref} data-testid="probe">{matches ? 'wide' : 'narrow'}</div>
}

describe('useContainerMinWidth', () => {
  let restore: (() => void) | null = null
  afterEach(() => { restore?.(); restore = null })

  it('matches when the element is at least minRem wide (16px root)', () => {
    restore = setElementWidth(1024)
    render(<Probe minRem={64} />)
    expect(screen.getByTestId('probe')).toHaveTextContent('wide')
  })

  it('does not match below the threshold', () => {
    restore = setElementWidth(1023)
    render(<Probe minRem={64} />)
    expect(screen.getByTestId('probe')).toHaveTextContent('narrow')
  })

  it('re-measures when the element resizes', () => {
    restore = setElementWidth(800)
    render(<Probe minRem={64} />)
    expect(screen.getByTestId('probe')).toHaveTextContent('narrow')
    act(() => { restore = setElementWidth(1440, { notify: true }) })
    expect(screen.getByTestId('probe')).toHaveTextContent('wide')
  })
})
