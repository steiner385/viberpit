// Overlays — the `app.overlays` region.
//
// The properties this file exists to pin:
//
//   • The region IS the portal root, not a box near it. Every assertion below
//     resolves `[data-region="app.overlays"]` from the document and compares it
//     to `getOverlayRoot()` BY NODE IDENTITY, and separately to
//     `document.getElementById('overlays')`. Both are needed: identity alone
//     still passes if the region marked `<body>` and `getOverlayRoot()` returned
//     `<body>` too, which is precisely what the old `?? document.body` fallback
//     in `ui/portal.ts` did in a document with no `#overlays` node — i.e. in
//     every test. This file deliberately never creates `#overlays` itself, so
//     the lazy creation in `ui/portal.ts` is under test rather than papered over.
//
//   • Overlays land INSIDE the region. Asserted through `Dialog`'s real portal
//     for both an app-level overlay (`DeployOverlay`) and a session-scoped one
//     (`CommandResultOverlay`), because "the region contains the overlays" is a
//     DOM claim about the portal target, not about React parentage — see the
//     `CommandResultOverlay` section of `Overlays.tsx`.
//
//   • One root, however many regions. Two `Overlays` mounted at once yield one
//     `#overlays` and one marked node; unmounting one of the two leaves the
//     survivor's mark intact (an unconditional cleanup would strip it).
//
//   • `DeployControllerProvider` is required, loudly. Outside it the region
//     throws at mount rather than rendering an invisible overlay forever. The
//     assertion matches the message, not merely "it threw" — any crash would
//     satisfy a bare `toThrow()`.
//
// Scope note on axe: jsdom applies no stylesheet, so the axe pass here covers
// roles, names and ARIA wiring ONLY. Its `color-contrast` rule always returns
// `incomplete` and `toHaveNoViolations()` inspects `violations` alone — a green
// assertion here is NOT evidence of adequate contrast. Contrast is checked
// separately by src/__tests__/contrast.test.ts against the tokens in index.css.
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { axe, toHaveNoViolations } from 'jest-axe'
import type { DeployProgress, PendingDeploy } from '@/api/types'
import type { Command } from '@/session/events'
import { DeployControllerProvider } from '@/lib/deployController'
import { getOverlayRoot } from '@/ui/portal'
import { CommandResultOverlay } from '@/components/session/CommandResultOverlay'
import { Overlays, OVERLAY_REGION } from './Overlays'

expect.extend(toHaveNoViolations)

// Real, fully-typed API payloads — a deploy mid-flight, which is the state that
// makes `DeployOverlay` visible through the genuine `DeployControllerProvider`
// logic (`visible` is derived from `running`). Hoisted because `vi.mock` runs
// its factory before the module body.
const api = vi.hoisted(() => {
  const deploy: DeployProgress = {
    running: true,
    phases: { preflight: 'done', save: 'running' },
    error: null,
    warnings: [],
    targetSha: 'deadbee1234',
  }
  const pending: PendingDeploy = {
    behind: true,
    liveSha: 'abcdef0',
    targetSha: 'deadbee',
    commits: [{ sha: 'deadbee', subject: 'ship it', author: 'tony', iso: '2026-09-03T12:00:00Z' }],
    ci: 'green',
  }
  return { deploy, pending }
})

vi.mock('@/api/hooks', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/hooks')>()),
  useDeployStatus: () => ({ data: api.deploy, isError: false, failureCount: 0 }),
  usePendingDeploy: () => ({ data: api.pending }),
  useStartDeploy: () => ({ mutate: () => {}, isPending: false, isError: false, error: null }),
}))

const COMMANDS: Command[] = [
  { name: 'help', description: 'List the available commands', argumentHint: null, kind: 'client', source: 'app' },
]

/** The region node, read from the document: everything here portals out of the
 *  React container, so a container-scoped query would find none of it. */
const region = () => document.querySelector(`[data-region="${OVERLAY_REGION}"]`)

function Shell({ children }: { children?: React.ReactNode }) {
  return (
    <DeployControllerProvider>
      <Overlays />
      {children}
    </DeployControllerProvider>
  )
}

describe('Overlays — app.overlays', () => {
  it('marks the portal root itself, and there is exactly one of it', () => {
    render(<Shell />)

    // Identity, not "a node with the attribute exists": the region must be the
    // very node `ui/portal.ts` hands to every Dialog/Sheet/Menu.
    expect(region()).toBe(getOverlayRoot())
    // …and that node must be the real `#overlays`, not `<body>` reached through
    // a fallback (the two are indistinguishable by the identity check alone).
    expect(region()).toBe(document.getElementById('overlays'))
    expect(region()).not.toBe(document.body)
    expect(document.querySelectorAll(`[data-region="${OVERLAY_REGION}"]`)).toHaveLength(1)
  })

  it('mounts DeployOverlay, and it renders inside the region', () => {
    render(<Shell />)

    const dialog = screen.getByRole('dialog', { name: 'Deploying' })
    expect(dialog).toHaveTextContent(/Upgrading ccswitch/)
    // The overlay is not merely on the page — it is in this region's subtree.
    expect(region()!.contains(dialog)).toBe(true)
  })

  it('a session-scoped CommandResultOverlay also lands inside the region', () => {
    // Mounted as a SIBLING of `Overlays`, the way `PageHost`'s page mounts it
    // from inside `SessionRuntimeProvider` — React parentage elsewhere, DOM
    // parentage here, because `Dialog` portals to the same root.
    render(
      <Shell>
        <CommandResultOverlay
          result={{ render: 'help', data: null }}
          commands={COMMANDS}
          onClose={() => {}}
        />
      </Shell>,
    )

    const dialog = screen.getByRole('dialog', { name: 'Command result' })
    expect(dialog).toHaveTextContent(/List the available commands/)
    expect(region()!.contains(dialog)).toBe(true)
  })

  it('rendering the region twice yields ONE overlay root', () => {
    render(
      <DeployControllerProvider>
        <Overlays />
        <Overlays />
      </DeployControllerProvider>,
    )

    expect(document.querySelectorAll('#overlays')).toHaveLength(1)
    expect(document.querySelectorAll(`[data-region="${OVERLAY_REGION}"]`)).toHaveLength(1)
  })

  it('unmounting one of two instances leaves the survivor marked', () => {
    function Two({ second }: { second: boolean }) {
      return (
        <DeployControllerProvider>
          <Overlays />
          {second && <Overlays />}
        </DeployControllerProvider>
      )
    }
    const { rerender } = render(<Two second />)
    expect(region()).toBe(getOverlayRoot())

    rerender(<Two second={false} />)
    expect(region()).toBe(getOverlayRoot())
  })

  it('releases the mark when the last instance unmounts', () => {
    const { unmount } = render(<Shell />)
    expect(region()).not.toBeNull()

    unmount()
    expect(region()).toBeNull()
    // The node itself is `ui/portal.ts`'s to keep — only the mark is ours.
    expect(document.getElementById('overlays')).not.toBeNull()
  })

  it('throws at mount outside DeployControllerProvider', () => {
    // React logs the thrown render error; silence it so the expected failure
    // does not read as a broken suite.
    const err = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      expect(() => render(<Overlays />)).toThrow(/no DeployControllerProvider above this component/)
    } finally {
      err.mockRestore()
    }
  })

  it('has no axe role/name violations with an overlay open (see scope note above)', async () => {
    render(<Shell />)
    expect(await axe(document.body)).toHaveNoViolations()
  })
})
