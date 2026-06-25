// web/src/components/__tests__/SessionCardPr.test.tsx
import { render, screen } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { SessionCard } from '../SessionCard'
import type { Session } from '@/api/types'

const session: Session = {
  id: 's1', name: 'My session', accountName: 'acct', repo: 'o/r',
  status: 'in_progress', createdAt: new Date().toISOString(),
  updatedAt: new Date().toISOString(),
  prs: [],
}

const noop = () => {}
const props = { account: undefined, onOpen: noop, onRename: noop, onClose: noop, onDelete: noop, onQuickResume: noop }

const failingPrSession: Session = {
  ...session,
  prs: [{ number: 7, url: 'https://github.com/o/r/pull/7', state: 'open', checks: 'failing', title: 't', deployed: false }],
}

describe('SessionCard PR pill', () => {
  it('shows the PR pill when the session has a PR', () => {
    render(<SessionCard session={{ ...session, prs: [{
      number: 7, url: 'https://github.com/o/r/pull/7', state: 'open',
      checks: 'passing', title: 't', deployed: false,
    }] }} {...props} />)
    expect(screen.getByRole('link', { name: /#7/ })).toBeInTheDocument()
  })

  it('shows no PR pill when the session has none', () => {
    render(<SessionCard session={session} {...props} />)
    expect(screen.queryByRole('link', { name: /#/ })).not.toBeInTheDocument()
  })
})

describe('SessionCard Fix PR button', () => {
  it('shows Fix PR #7 button in propose mode', () => {
    const onFixPr = vi.fn()
    render(<SessionCard {...props} session={failingPrSession} prAutofixMode="propose" onFixPr={onFixPr} />)
    expect(screen.getByRole('button', { name: /fix pr #7/i })).toBeInTheDocument()
  })

  it('clicking Fix PR button calls onFixPr with correct args', () => {
    const onFixPr = vi.fn()
    render(<SessionCard {...props} session={failingPrSession} prAutofixMode="propose" onFixPr={onFixPr} />)
    screen.getByRole('button', { name: /fix pr #7/i }).click()
    expect(onFixPr).toHaveBeenCalledWith('s1', 7)
  })

  it('hides Fix PR button in off mode', () => {
    render(<SessionCard {...props} session={failingPrSession} prAutofixMode="off" />)
    expect(screen.queryByRole('button', { name: /fix pr/i })).toBeNull()
  })

  it('hides Fix PR button in auto mode', () => {
    render(<SessionCard {...props} session={failingPrSession} prAutofixMode="auto" />)
    expect(screen.queryByRole('button', { name: /fix pr/i })).toBeNull()
  })

  it('hides Fix PR button when PR is passing', () => {
    const passingPrSession = { ...failingPrSession, prs: [{ ...failingPrSession.prs![0], checks: 'passing' as const }] }
    render(<SessionCard {...props} session={passingPrSession} prAutofixMode="propose" />)
    expect(screen.queryByRole('button', { name: /fix pr/i })).toBeNull()
  })

  it('hides Fix PR button when PR is merged (terminal)', () => {
    const mergedPrSession = { ...failingPrSession, prs: [{ ...failingPrSession.prs![0], state: 'merged' as const }] }
    render(<SessionCard {...props} session={mergedPrSession} prAutofixMode="propose" />)
    expect(screen.queryByRole('button', { name: /fix pr/i })).toBeNull()
  })
})
