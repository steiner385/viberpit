import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { PendingPermissionPanel } from '../PendingPermissionPanel'

const perm = (permId: string, tool = 'Write') => ({
  kind: 'permission' as const,
  id: permId,
  permId,
  tool,
  toolKind: 'write' as const,
  input: { file_path: '/tmp/x.md' },
  plan: false,
})
let _tid = 0
const turn = (...blocks: ReturnType<typeof perm>[]) => ({
  id: `t-${_tid++}`,
  role: 'assistant' as const,
  blocks,
})

describe('PendingPermissionPanel', () => {
  it('renders nothing when there is no open permission', () => {
    const { container } = render(
      <PendingPermissionPanel turns={[turn()]} permissionMode="interactive" onDecision={vi.fn()} />,
    )
    expect(container).toBeEmptyDOMElement()
  })

  it('pins the first open permission with actionable allow/deny controls', () => {
    render(
      <PendingPermissionPanel turns={[turn(perm('p1'))]} permissionMode="interactive" onDecision={vi.fn()} />,
    )
    // The actionable card — role="group" "Permission request" — is rendered in the panel.
    expect(screen.getByRole('group', { name: /permission request/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^allow/i })).toBeInTheDocument()
  })

  it('exposes role="region" with accessible name "Pending permission"', () => {
    render(
      <PendingPermissionPanel turns={[turn(perm('p1'))]} permissionMode="interactive" onDecision={vi.fn()} />,
    )
    expect(screen.getByRole('region', { name: /pending permission/i })).toBeInTheDocument()
  })

  it('caps its height and scrolls internally so long permission details keep the buttons reachable', () => {
    // Mirrors PendingQuestionPanel: this panel is flex-shrink-0, so a long tool
    // detail / diff preview would push the allow/deny buttons below the fold with
    // no reachable scroll. It must bound its own height and scroll within.
    render(
      <PendingPermissionPanel turns={[turn(perm('p1'))]} permissionMode="interactive" onDecision={vi.fn()} />,
    )
    const region = screen.getByRole('region', { name: /pending permission/i })
    expect(region).toHaveClass('overflow-y-auto', 'overscroll-contain', 'max-h-[50vh]')
  })

  it('clicking allow calls onDecision with the permId and decision', () => {
    const onDecision = vi.fn()
    render(
      <PendingPermissionPanel turns={[turn(perm('p1'))]} permissionMode="interactive" onDecision={onDecision} />,
    )
    fireEvent.click(screen.getByRole('button', { name: /^allow/i }))
    expect(onDecision).toHaveBeenCalledWith('p1', 'allow')
  })

  it('one at a time: shows only the first open permission', () => {
    render(
      <PendingPermissionPanel
        turns={[turn(perm('p1', 'Write'), perm('p2', 'Bash'))]}
        permissionMode="interactive"
        onDecision={vi.fn()}
      />,
    )
    const groups = screen.getAllByRole('group', { name: /permission request/i })
    expect(groups).toHaveLength(1)
  })
})
