/**
 * ⌘K "New session on repo@branch" (D-19): the shell's request selects the repo and
 * shows a removable branch chip; the branch rides on the next create, once, and only
 * while its repo is the one chosen.
 */
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { GithubRepo } from '@/api/hooks'
import { githubRepo } from '@/test/factories'
import { DispatchBar, type DispatchBarProps, type DispatchRequest } from './DispatchBar'

const REPOS: GithubRepo[] = [githubRepo({ nameWithOwner: 'o/a' }), githubRepo({ nameWithOwner: 'o/b' })]
const REQUEST: DispatchRequest = { kind: 'branch', repo: 'o/b', branch: 'feat/x' }

function mount(over: Partial<DispatchBarProps> = {}) {
  const onDispatch = vi.fn().mockResolvedValue(undefined)
  const onDispatchRequestHandled = vi.fn()
  render(
    <DispatchBar
      repos={REPOS} reposLoading={false} reposError={false} onDispatch={onDispatch}
      onNavigate={vi.fn()} onOpenHighlighted={vi.fn()} onEnterList={vi.fn()} onShowHelp={vi.fn()}
      onEscape={vi.fn()} isPending={false}
      dispatchRequest={REQUEST} onDispatchRequestHandled={onDispatchRequestHandled}
      {...over}
    />,
  )
  return { onDispatch, onDispatchRequestHandled }
}

const send = async (text: string) => {
  await userEvent.type(screen.getByLabelText('New session prompt'), `${text}{Enter}`)
}

beforeEach(() => window.localStorage.clear())

describe('DispatchBar — start on a branch (⌘K)', () => {
  it('selects the repo, shows the branch chip, and acknowledges the request once', async () => {
    const { onDispatchRequestHandled } = mount()
    expect(screen.getByLabelText('Repository')).toHaveValue('o/b')
    expect(screen.getByText('Branch: feat/x')).toBeInTheDocument()
    await waitFor(() => expect(onDispatchRequestHandled).toHaveBeenCalledTimes(1))
  })

  it('sends the chosen branch with its repo, then clears the chip', async () => {
    const { onDispatch } = mount()
    await send('go')
    await waitFor(() => expect(onDispatch).toHaveBeenCalledTimes(1))
    expect(onDispatch.mock.calls[0][0]).toMatchObject({ repo: 'o/b', branch: 'feat/x', initialPrompt: 'go' })
    await waitFor(() => expect(screen.queryByText('Branch: feat/x')).toBeNull())
  })

  it('removing the chip sends no branch', async () => {
    const { onDispatch } = mount()
    await userEvent.click(screen.getByRole('button', { name: 'Remove branch feat/x' }))
    expect(screen.queryByText('Branch: feat/x')).toBeNull()
    await send('go')
    await waitFor(() => expect(onDispatch).toHaveBeenCalledTimes(1))
    expect(onDispatch.mock.calls[0][0]).toMatchObject({ repo: 'o/b', branch: null })
  })

  it('choosing another repo drops the branch — it belongs to the repo it came from', async () => {
    const { onDispatch } = mount()
    await userEvent.selectOptions(screen.getByLabelText('Repository'), 'o/a')
    expect(screen.queryByText('Branch: feat/x')).toBeNull()
    await send('go')
    await waitFor(() => expect(onDispatch).toHaveBeenCalledTimes(1))
    expect(onDispatch.mock.calls[0][0]).toMatchObject({ repo: 'o/a', branch: null })
  })
})
