// web/src/components/ErrorBoundary.test.tsx
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
  })

  it('renders children when no error', () => {
    render(<ErrorBoundary><div>ok</div></ErrorBoundary>)
    expect(screen.getByText('ok')).toBeInTheDocument()
  })
})
