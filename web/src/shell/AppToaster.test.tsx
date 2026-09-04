import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { AppToaster } from './AppToaster'

// Capture the props handed to sonner instead of rendering its DOM: the
// contract under test is the configuration, not sonner's internals.
vi.mock('sonner', () => ({
  Toaster: (props: Record<string, unknown>) => (
    <div data-testid="toaster" data-props={JSON.stringify(props)} />
  ),
}))

function toasterProps(): Record<string, unknown> {
  render(<AppToaster />)
  return JSON.parse(screen.getByTestId('toaster').dataset.props ?? '{}')
}

describe('AppToaster — placement', () => {
  it('sits top-right on desktop, off the page title', () => {
    expect(toasterProps().position).toBe('top-right')
  })

  it('pins the phone strip to the safe-area top, over the TopBar band', () => {
    const offset = toasterProps().mobileOffset as Record<string, string>
    expect(offset.top).toContain('env(safe-area-inset-top)')
    // Side insets keep the strip off the screen edge; bottom is sonner's default.
    expect(offset.left).toBeTruthy()
    expect(offset.right).toBeTruthy()
    expect(offset.bottom).toBeUndefined()
  })

  it('offers a close button so a toast never has to be waited out', () => {
    expect(toasterProps().closeButton).toBe(true)
  })
})

describe('AppToaster — the single mount', () => {
  const here = dirname(fileURLToPath(import.meta.url)) // .../web/src/shell
  const main = readFileSync(join(here, '..', 'main.tsx'), 'utf8')

  it('main.tsx mounts AppToaster after (outside) the ErrorBoundary', () => {
    const boundaryClose = main.indexOf('</ErrorBoundary>')
    const mount = main.indexOf('<AppToaster />')
    expect(boundaryClose).toBeGreaterThan(-1)
    expect(mount).toBeGreaterThan(boundaryClose)
  })

  it('main.tsx does not also import sonner itself (one Toaster, configured here)', () => {
    expect(main).not.toMatch(/from ['"]sonner['"]/)
  })
})
