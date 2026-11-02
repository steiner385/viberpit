import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { PermissionCard } from './PermissionCard'
import type { Block } from '@/session/reducer'

// Counts evaluations of the lazily loaded plan-lines module: the plan card is
// first-paint, so the gutter/editor must only load once comment mode opens.
const { planLinesLoaded } = vi.hoisted(() => ({ planLinesLoaded: vi.fn() }))
vi.mock('../review/PlanStepLines', async (importOriginal) => {
  planLinesLoaded()
  return importOriginal()
})

type PermBlock = Extract<Block, { kind: 'permission' }>

const PLAN = '1. Read the code\n2. Edit the parser\n3. Run the tests\n4. Ship it'

const planPerm: PermBlock = {
  kind: 'permission',
  id: 'perm-plan',
  permId: 'perm-plan',
  tool: 'ExitPlanMode',
  toolKind: 'unknown',
  input: { plan: PLAN },
  plan: true,
}

const overall = () => screen.getByRole('textbox', { name: /what should change/i })
const send = () => screen.getByRole('button', { name: /^Send & keep planning/i })

async function openComments(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('button', { name: /^Keep planning/i }))
}

async function commentOnLine(user: ReturnType<typeof userEvent.setup>, n: number, body: string) {
  await user.click(await screen.findByRole('button', { name: `Comment on step line ${n}` }))
  await user.type(screen.getByRole('textbox', { name: 'Comment' }), body)
  await user.click(screen.getByRole('button', { name: 'Save' }))
}

describe('PermissionCard — per-step plan comments (D-5)', () => {
  // First in the file: the module cache keeps the plan-lines module once loaded.
  it('does not load the step gutter/editor until comment mode opens', async () => {
    const user = userEvent.setup()
    render(<PermissionCard block={planPerm} onDecision={vi.fn()} />)
    expect(screen.queryByRole('button', { name: /Comment on step line/ })).toBeNull()
    expect(planLinesLoaded).not.toHaveBeenCalled()
    await openComments(user)
    expect(await screen.findByRole('button', { name: 'Comment on step line 1' })).toBeInTheDocument()
    expect(planLinesLoaded).toHaveBeenCalledTimes(1)
  })

  it('renders every plan line, numbered, with its own comment target', async () => {
    const user = userEvent.setup()
    render(<PermissionCard block={planPerm} onDecision={vi.fn()} />)
    await openComments(user)
    for (const n of [1, 2, 3, 4]) {
      expect(await screen.findByRole('button', { name: `Comment on step line ${n}` })).toBeInTheDocument()
    }
    expect(screen.getByText('3. Run the tests')).toBeInTheDocument()
    // The numbered lines replace the plain plan text — the plan is not shown twice.
    expect(screen.queryByTestId('permission-detail')).toBeNull()
  })

  it('a step comment plus overall text sends the composed message', async () => {
    const user = userEvent.setup()
    const onDecision = vi.fn()
    render(<PermissionCard block={planPerm} onDecision={onDecision} />)
    await openComments(user)
    await commentOnLine(user, 3, 'also run lint')
    await user.type(overall(), 'Mostly good')
    await user.click(send())
    expect(onDecision).toHaveBeenCalledTimes(1)
    expect(onDecision).toHaveBeenCalledWith(
      planPerm.permId,
      'deny',
      'Mostly good\n\nOn the plan:\n- line 3: "3. Run the tests" — also run lint',
    )
  })

  it('step comments with no overall text send the steps only, in line order', async () => {
    const user = userEvent.setup()
    const onDecision = vi.fn()
    render(<PermissionCard block={planPerm} onDecision={onDecision} />)
    await openComments(user)
    await commentOnLine(user, 4, 'not yet')
    await commentOnLine(user, 2, 'which parser?')
    await user.click(send())
    expect(onDecision).toHaveBeenCalledWith(
      planPerm.permId,
      'deny',
      'On the plan:\n- line 2: "2. Edit the parser" — which parser?\n- line 4: "4. Ship it" — not yet',
    )
  })

  it('a saved step comment can be deleted before sending', async () => {
    const user = userEvent.setup()
    const onDecision = vi.fn()
    render(<PermissionCard block={planPerm} onDecision={onDecision} />)
    await openComments(user)
    await commentOnLine(user, 1, 'drop me')
    await user.click(screen.getByRole('button', { name: 'Delete' }))
    await user.click(send())
    expect(onDecision.mock.calls[0]).toEqual([planPerm.permId, 'deny'])
  })

  it('over 4000 characters disables Send and shows the length', async () => {
    const user = userEvent.setup()
    const onDecision = vi.fn()
    render(<PermissionCard block={planPerm} onDecision={onDecision} />)
    await openComments(user)
    fireEvent.change(overall(), { target: { value: 'x'.repeat(4001) } })
    expect(send()).toBeDisabled()
    expect(screen.getByRole('status')).toHaveTextContent(/^Too long$/)
    expect(screen.getByText('— 4001/4000')).toBeInTheDocument()
    await user.click(send())
    expect(onDecision).not.toHaveBeenCalled()
  })

  it('the limit counts the composed message, step comments included', async () => {
    const user = userEvent.setup()
    render(<PermissionCard block={planPerm} onDecision={vi.fn()} />)
    await openComments(user)
    fireEvent.change(overall(), { target: { value: 'x'.repeat(3990) } })
    expect(send()).toBeEnabled()
    await commentOnLine(user, 1, 'ok')
    // 3990 + '\n\nOn the plan:\n' (15) + '- line 1: "1. Read the code" — ok' (33)
    expect(send()).toBeDisabled()
    expect(screen.getByText('— 4038/4000')).toBeInTheDocument()
  })

  it('Cancel sends nothing and drops the step comments', async () => {
    const user = userEvent.setup()
    const onDecision = vi.fn()
    render(<PermissionCard block={planPerm} onDecision={onDecision} />)
    await openComments(user)
    await commentOnLine(user, 2, 'hmm')
    await user.type(overall(), 'later')
    await user.click(screen.getByRole('button', { name: /^Cancel$/ }))
    expect(onDecision).not.toHaveBeenCalled()
    await openComments(user)
    await screen.findByRole('button', { name: 'Comment on step line 2' })
    expect(screen.queryByRole('article', { name: /Comment on line 2/ })).toBeNull()
    expect(overall()).toHaveValue('')
  })

  it('falls back to the detail text when the request carries no plan', async () => {
    const user = userEvent.setup()
    const onDecision = vi.fn()
    render(
      <PermissionCard
        block={{ ...planPerm, tool: 'Bash', toolKind: 'bash', input: { command: 'make\nmake test' } }}
        onDecision={onDecision}
      />,
    )
    await openComments(user)
    await commentOnLine(user, 2, 'skip')
    await user.click(send())
    expect(onDecision).toHaveBeenCalledWith(planPerm.permId, 'deny', 'On the plan:\n- line 2: "make test" — skip')
  })

  it('long plan lines wrap rather than overflow a phone-width card', async () => {
    const user = userEvent.setup()
    const long = 'x'.repeat(300)
    render(<PermissionCard block={{ ...planPerm, input: { plan: long } }} onDecision={vi.fn()} />)
    await openComments(user)
    const text = await screen.findByText(long)
    expect(text).toHaveClass('whitespace-pre-wrap', 'min-w-0')
    expect(text.className).toMatch(/overflow-wrap:anywhere|break-all/)
  })

  it('the "Too long" live region stays static while typing: announced once, the count outside it', async () => {
    const user = userEvent.setup()
    render(<PermissionCard block={planPerm} onDecision={vi.fn()} />)
    await openComments(user)
    const status = screen.getByRole('status')
    expect(status).toHaveTextContent('')
    fireEvent.change(overall(), { target: { value: 'x'.repeat(4001) } })
    expect(status).toHaveTextContent(/^Too long$/)
    fireEvent.change(overall(), { target: { value: 'x'.repeat(4002) } })
    // Same node, same text: no re-announcement per keystroke; the count updates beside it.
    expect(screen.getByRole('status')).toBe(status)
    expect(status).toHaveTextContent(/^Too long$/)
    expect(status).not.toContainElement(screen.getByText('— 4002/4000'))
  })

  it('closing a step editor (Save or Cancel) returns focus to that line\'s gutter', async () => {
    const user = userEvent.setup()
    render(<PermissionCard block={planPerm} onDecision={vi.fn()} />)
    await openComments(user)
    await commentOnLine(user, 2, 'save me')
    expect(screen.getByRole('button', { name: 'Comment on step line 2' })).toHaveFocus()
    await user.click(screen.getByRole('button', { name: 'Comment on step line 3' }))
    await user.click(within(screen.getByRole('group', { name: 'Plan' })).getByRole('button', { name: 'Cancel' }))
    expect(screen.getByRole('button', { name: 'Comment on step line 3' })).toHaveFocus()
  })

  it('editing a step comment returns focus to its Edit button; Delete to the next card, else the gutter', async () => {
    const user = userEvent.setup()
    render(<PermissionCard block={planPerm} onDecision={vi.fn()} />)
    await openComments(user)
    await commentOnLine(user, 1, 'one')
    await commentOnLine(user, 3, 'three')
    const [first, second] = screen.getAllByRole('article')
    await user.click(within(first).getByRole('button', { name: 'Edit' }))
    await user.click(within(first).getByRole('button', { name: 'Save' }))
    expect(within(first).getByRole('button', { name: 'Edit' })).toHaveFocus()
    await user.click(within(first).getByRole('button', { name: 'Delete' }))
    expect(within(second).getByRole('button', { name: 'Edit' })).toHaveFocus()
    await user.click(within(second).getByRole('button', { name: 'Delete' }))
    expect(screen.getByRole('button', { name: 'Comment on step line 3' })).toHaveFocus()
  })

  it('the numbered plan is height-capped and scrolls, like the plain text it replaces', async () => {
    const user = userEvent.setup()
    render(<PermissionCard block={planPerm} onDecision={vi.fn()} />)
    await openComments(user)
    const plan = await screen.findByRole('group', { name: 'Plan' })
    expect(plan).toHaveClass('max-h-48', 'overflow-y-auto')
  })
})
