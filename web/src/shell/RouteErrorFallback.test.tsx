// web/src/shell/RouteErrorFallback.test.tsx
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { createMemoryRouter, Outlet, RouterProvider, type RouteObject } from 'react-router-dom'
import { RouteErrorFallback } from './RouteErrorFallback'
import { buildRoutes } from './routes'
import { clientLogger } from '@/obs/logger'

function Layout() {
  return (
    <div>
      <nav data-testid="shell-nav">nav</nav>
      <Outlet />
    </div>
  )
}

function Boom(): never { throw new Error('render-crash SECRET_INTERNAL_DETAIL') }

function mount(routes: RouteObject[], path = '/') {
  const router = createMemoryRouter(routes, { initialEntries: [path] })
  render(<RouterProvider router={router} />)
}

describe('RouteErrorFallback (#1418)', () => {
  let logSpy: ReturnType<typeof vi.spyOn>
  beforeEach(() => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    logSpy = vi.spyOn(clientLogger, 'error').mockImplementation(() => {})
  })
  afterEach(() => vi.restoreAllMocks())

  it('contains a page render exception: no stack, shell kept, id logged', () => {
    mount([{ element: <Layout />, children: [{ errorElement: <RouteErrorFallback />, children: [{ path: '/', element: <Boom /> }] }] }])
    expect(screen.getByRole('alert')).toBeInTheDocument()
    expect(screen.getByTestId('shell-nav')).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/SECRET_INTERNAL_DETAIL|Unexpected Application Error|errorElement/)
    const id = screen.getByTestId('route-error-id').textContent!
    expect(id).toMatch(/^[0-9a-f]{8}$/i)
    expect(logSpy).toHaveBeenCalledWith(
      expect.stringContaining(`[${id}] render-crash`),
      expect.objectContaining({ stack: expect.any(String) }),
    )
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Go to Sessions' })).toBeInTheDocument()
  })

  it('treats a failed chunk import as a reload prompt', () => {
    const lazyFail = () => { throw new TypeError('Failed to fetch dynamically imported module: /assets/x.js') }
    function Page() { return lazyFail() }
    mount([{ errorElement: <RouteErrorFallback />, children: [{ path: '/', element: <Page /> }] }])
    expect(screen.getByText('This page could not be loaded.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reload' })).toBeInTheDocument()
  })

  it('handles a non-Error throw (invalid shape) without leaking it', () => {
    function Page(): never { throw { weird: 'SHAPE_LEAK' } }
    mount([{ errorElement: <RouteErrorFallback />, children: [{ path: '/', element: <Page /> }] }])
    expect(screen.getByRole('alert')).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/Unexpected Application Error/)
  })

  it('the real route table installs it above and below the layout', () => {
    const routes = buildRoutes(<Layout />)
    expect(routes[0].errorElement).toBeTruthy()
    expect(routes[0].children![0].children![0].errorElement).toBeTruthy()
  })
})
