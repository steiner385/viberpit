import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect } from 'vitest'
import { SessionCard } from '../SessionCard'
import type { Session, Account } from '@/api/types'

const baseSession: Session = {
  id: '1',
  name: 'Fix login bug',
  accountName: 'primary',
  status: 'in_progress',
  createdAt: '2026-06-22T11:00:00Z',
  updatedAt: '2026-06-22T11:58:00Z',
}

const baseAccount: Account = {
  name: 'primary',
  status: 'ok',
  usage: null,
  nickname: 'tony@kindash.com',
}

const noop = () => {}

function renderCard(session: Session, account: Account | undefined) {
  return render(
    <MemoryRouter>
      <SessionCard
        session={session}
        account={account}
        onOpen={noop}
        onRename={noop}
        onClose={noop}
        onDelete={noop}
      />
    </MemoryRouter>,
  )
}

describe('SessionCard account badge', () => {
  it('shows the account nickname instead of the internal slug', () => {
    renderCard(baseSession, baseAccount)
    expect(screen.getByText('tony@kindash.com')).toBeInTheDocument()
    // The internal slug must never leak into the badge once a nickname is chosen.
    expect(screen.queryByText('primary')).not.toBeInTheDocument()
  })

  it('falls back to the internal name when the account has no nickname', () => {
    renderCard(baseSession, { ...baseAccount, nickname: null })
    expect(screen.getByText('primary')).toBeInTheDocument()
  })

  it('falls back to the internal name when the account is unknown', () => {
    renderCard(baseSession, undefined)
    expect(screen.getByText('primary')).toBeInTheDocument()
  })
})
