import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import type { RuleWire } from '@/api/types'

const { useRules, revokeAsync } = vi.hoisted(() => ({
  useRules: vi.fn(),
  revokeAsync: vi.fn(),
}))
vi.mock('@/api/hooks', () => ({
  usePermissionRules: useRules,
  useRevokePermissionRule: () => ({ mutateAsync: revokeAsync, isPending: false }),
}))

import { SessionRulesReadout } from './SessionRulesReadout'

const rule = (o: Partial<RuleWire>): RuleWire => ({
  id: 'r1', profileId: 'p', pattern: 'Bash(npm test:*)', scope: 'session', scopeKey: 's1', position: 0,
  createdBy: 'tony', createdVia: 'card', createdAt: '2026-10-11T00:00:00Z', hitCount: 3, lastHitAt: null,
  description: 'npm test', ...o,
})

describe('SessionRulesReadout', () => {
  beforeEach(() => { useRules.mockReset(); revokeAsync.mockReset() })

  it('renders nothing when no rules apply', () => {
    useRules.mockReturnValue({ data: [] })
    const { container } = render(<SessionRulesReadout sessionId="s1" />)
    expect(container).toBeEmptyDOMElement()
    expect(useRules).toHaveBeenCalledWith('s1')
  })

  it('lists pattern, scope chip and hits', () => {
    useRules.mockReturnValue({ data: [rule({}), rule({ id: 'r2', pattern: 'Read(*)', scope: 'global', scopeKey: null, hitCount: 1 })] })
    render(<SessionRulesReadout sessionId="s1" />)
    expect(screen.getByText('Bash(npm test:*)')).toBeInTheDocument()
    expect(screen.getByText('This session')).toBeInTheDocument()
    expect(screen.getByText('All sessions')).toBeInTheDocument()
    expect(screen.getByText(/3 hits/)).toBeInTheDocument()
  })

  it('singularizes one hit', () => {
    useRules.mockReturnValue({ data: [rule({ hitCount: 1 })] })
    render(<SessionRulesReadout sessionId="s1" />)
    expect(screen.getByText('1 hit')).toBeInTheDocument()
  })

  it('gives the revoke button the full touch target', () => {
    useRules.mockReturnValue({ data: [rule({})] })
    render(<SessionRulesReadout sessionId="s1" />)
    expect(screen.getByRole('button', { name: /Revoke rule/ })).toHaveClass('min-h-target', 'min-w-target')
  })

  it('revokes after confirmation', async () => {
    useRules.mockReturnValue({ data: [rule({})] })
    revokeAsync.mockResolvedValue(undefined)
    render(<SessionRulesReadout sessionId="s1" />)
    fireEvent.click(screen.getByRole('button', { name: /revoke rule Bash\(npm test:\*\)/i }))
    fireEvent.click(await screen.findByRole('button', { name: 'Revoke' }))
    await waitFor(() => expect(revokeAsync).toHaveBeenCalledWith('r1'))
  })

  it('shows a revoke failure inline', async () => {
    useRules.mockReturnValue({ data: [rule({})] })
    revokeAsync.mockRejectedValue(new Error('x'))
    render(<SessionRulesReadout sessionId="s1" />)
    fireEvent.click(screen.getByRole('button', { name: /revoke rule/i }))
    fireEvent.click(await screen.findByRole('button', { name: 'Revoke' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not revoke the rule.')
  })
})
