// web/src/components/__tests__/SessionCardPr.test.tsx
import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import { SessionCard } from '../SessionCard'
import type { Session } from '@/api/types'

const session: Session = {
  id: 's1', name: 'My session', accountName: 'acct', repo: 'o/r',
  status: 'in_progress', createdAt: new Date().toISOString(),
  updatedAt: new Date().toISOString(),
}

const noop = () => {}
const props = { account: undefined, onOpen: noop, onRename: noop, onClose: noop, onDelete: noop }

describe('SessionCard PR pill', () => {
  it('shows the PR pill when the session has a PR', () => {
    render(<SessionCard session={{ ...session, pr: {
      number: 7, url: 'https://github.com/o/r/pull/7', state: 'open',
      checks: 'passing', title: 't', deployed: false,
    } }} {...props} />)
    expect(screen.getByRole('link', { name: /#7/ })).toBeInTheDocument()
  })

  it('shows no PR pill when the session has none', () => {
    render(<SessionCard session={session} {...props} />)
    expect(screen.queryByRole('link', { name: /#/ })).not.toBeInTheDocument()
  })
})
