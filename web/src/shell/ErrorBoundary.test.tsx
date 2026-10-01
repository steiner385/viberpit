// web/src/shell/ErrorBoundary.test.tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ErrorBoundary } from './ErrorBoundary'
import { ClientLogger } from '@/obs/logger'

function Boom(): never { throw new Error('render-crash') }

describe('ErrorBoundary', () => {
  it('renders fallback and reports the error', () => {
    const logger = new ClientLogger()
    const spy = vi.spyOn(logger, 'error')
    // Silence React's error console for this expected throw.
    vi.spyOn(console, 'error').mockImplementation(() => {})
    render(<ErrorBoundary logger={logger}><Boom /></ErrorBoundary>)
    expect(screen.getByText(/something went wrong/i)).toBeInTheDocument()
    expect(spy).toHaveBeenCalledWith(expect.stringContaining('render-crash'), expect.any(Object))
    // On the primitives and tokens like every other surface (2026-10-01 shell
    // critique, minor): the one action is the lg Button, the box is on the canvas.
    const reload = screen.getByRole('button', { name: 'Reload' })
    expect(reload.className).toMatch(/\bmin-h-target\b/)
    expect(screen.getByRole('alert').className).toMatch(/\bbg-canvas\b/)
    expect(screen.getByRole('alert').className).not.toMatch(/min-h-screen/)
    expect(screen.getByText(/has been logged/i)).toBeInTheDocument()
  })

  it('renders children when no error', () => {
    render(<ErrorBoundary><div>ok</div></ErrorBoundary>)
    expect(screen.getByText('ok')).toBeInTheDocument()
  })
})
