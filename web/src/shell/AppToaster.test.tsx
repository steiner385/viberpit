import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { AppToaster } from './AppToaster'
import { TOAST_INSET_VARS } from './toastInsets'

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

type Offset = Record<'top' | 'left' | 'right' | 'bottom', string | undefined>

describe('AppToaster — placement', () => {
  it('sits top-right, at the top of the content band', () => {
    expect(toasterProps().position).toBe('top-right')
  })

  it('measures every edge from the band and header insets, on desktop and on a phone', () => {
    const props = toasterProps()
    for (const offset of [props.offset, props.mobileOffset] as Offset[]) {
      expect(offset.top).toContain(`var(${TOAST_INSET_VARS.top}`)
      expect(offset.left).toContain(`var(${TOAST_INSET_VARS.left}`)
      expect(offset.right).toContain(`var(${TOAST_INSET_VARS.right}`)
      // Never the bottom band: NavBar, dock and bottom-sheet dialogs live there.
      expect(offset.bottom).toBeUndefined()
    }
  })

  it('falls back to the safe-area top when the shell (and its insets) are gone', () => {
    const offset = toasterProps().mobileOffset as Offset
    expect(offset.top).toMatch(/max\(.*env\(safe-area-inset-top/)
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
