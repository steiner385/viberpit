/**
 * The dispatch bar's `+` menu (D-14): Attach image moved in from its own button,
 * Presets › (a removable chip, `presetId` on the next create) and Templates ›
 * (the server-rendered text inserted into the prompt).
 */
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { PromptTemplate, SessionPreset } from '@/api/types'
import type { GithubRepo } from '@/api/hooks'
import { githubRepo } from '@/test/factories'
import { DispatchBar, type DispatchBarProps } from './DispatchBar'

const preset = (over: Partial<SessionPreset> = {}): SessionPreset => ({
  id: 'p1', name: 'Bug bash', repo: null, account: null, model: null, permissionMode: null,
  initialPrompt: null, branchStrategy: null, permissionProfileId: null, createdAt: null, updatedAt: null,
  ...over,
})

const template = (over: Partial<PromptTemplate> = {}): PromptTemplate => ({
  id: 't1', name: 'Review', content: 'Review {{area}}', variables: [{ name: 'area', default: 'the parser' }],
  tags: [], createdAt: null, updatedAt: null, ...over,
})

const REPOS: GithubRepo[] = [githubRepo({ nameWithOwner: 'o/a' }), githubRepo({ nameWithOwner: 'o/b' })]

function mount(over: Partial<DispatchBarProps> = {}) {
  const onDispatch = vi.fn().mockResolvedValue(undefined)
  const props: DispatchBarProps = {
    repos: REPOS, reposLoading: false, reposError: false, onDispatch,
    onNavigate: vi.fn(), onOpenHighlighted: vi.fn(), onEnterList: vi.fn(), onShowHelp: vi.fn(),
    onEscape: vi.fn(), isPending: false,
    models: [{ id: 'opus', label: 'Opus' }, { id: 'sonnet', label: 'Sonnet' }],
    renderTemplate: vi.fn().mockResolvedValue(''),
    ...over,
  }
  const utils = render(<DispatchBar {...props} />)
  return { ...utils, onDispatch, props }
}

const openAdd = () => userEvent.click(screen.getByRole('button', { name: 'Add' }))
const itemLabels = () =>
  Array.from(screen.getByRole('menu').querySelectorAll('[role^="menuitem"]')).map((i) => i.textContent?.replace(/\d$/, ''))

beforeEach(() => window.localStorage.clear())

describe('DispatchBar + menu', () => {
  it('replaces the standalone Attach image button', () => {
    mount()
    expect(screen.queryByRole('button', { name: 'Attach image' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Add' })).toHaveAttribute('aria-haspopup', 'menu')
  })

  it('offers Attach image, Presets and Templates', async () => {
    mount()
    await openAdd()
    expect(itemLabels()).toEqual(['Attach image', 'Presets ›', 'Templates ›'])
  })

  it('Attach image still opens the file picker', async () => {
    const { container } = mount()
    const input = container.querySelector('input[type=file]') as HTMLInputElement
    const click = vi.spyOn(input, 'click')
    await openAdd()
    await userEvent.click(screen.getByRole('menuitem', { name: /Attach image/ }))
    expect(click).toHaveBeenCalledTimes(1)
  })

  it('a preset shows a removable chip and its id rides on the next create, once', async () => {
    const { onDispatch } = mount({ presets: [preset(), preset({ id: 'p2', name: 'Docs pass' })] })
    await openAdd()
    await userEvent.click(screen.getByRole('menuitem', { name: /Presets/ }))
    expect(screen.getByRole('menu', { name: 'Presets' })).toBeInTheDocument()
    expect(itemLabels()).toEqual(['Bug bash', 'Docs pass'])
    await userEvent.click(screen.getByRole('menuitem', { name: /Docs pass/ }))
    expect(screen.getByText('Preset: Docs pass')).toBeInTheDocument()

    await userEvent.type(screen.getByLabelText('New session prompt'), 'go{Enter}')
    await waitFor(() => expect(onDispatch).toHaveBeenCalledTimes(1))
    expect(onDispatch.mock.calls[0][0]).toMatchObject({ presetId: 'p2', initialPrompt: 'go' })
    // One create: the chip clears with the draft.
    expect(screen.queryByText('Preset: Docs pass')).toBeNull()
    await userEvent.type(screen.getByLabelText('New session prompt'), 'again{Enter}')
    await waitFor(() => expect(onDispatch).toHaveBeenCalledTimes(2))
    expect(onDispatch.mock.calls[1][0]).toMatchObject({ presetId: null })
  })

  it('removing the chip drops the preset', async () => {
    const { onDispatch } = mount({ presets: [preset()] })
    await openAdd()
    await userEvent.click(screen.getByRole('menuitem', { name: /Presets/ }))
    await userEvent.click(screen.getByRole('menuitem', { name: /Bug bash/ }))
    await userEvent.click(screen.getByRole('button', { name: 'Remove preset Bug bash' }))
    expect(screen.queryByText('Preset: Bug bash')).toBeNull()
    await userEvent.type(screen.getByLabelText('New session prompt'), 'go{Enter}')
    await waitFor(() => expect(onDispatch).toHaveBeenCalledTimes(1))
    expect(onDispatch.mock.calls[0][0]).toMatchObject({ presetId: null })
  })

  it("a preset fills the bar's visible picks, so what is shown is what is sent", async () => {
    const { onDispatch } = mount({
      presets: [preset({ repo: 'o/b', model: 'sonnet', initialPrompt: 'Triage the open bugs' })],
    })
    await openAdd()
    await userEvent.click(screen.getByRole('menuitem', { name: /Presets/ }))
    await userEvent.click(screen.getByRole('menuitem', { name: /Bug bash/ }))
    expect(screen.getByLabelText('Repository')).toHaveValue('o/b')
    expect(screen.getByLabelText('Model')).toHaveValue('sonnet')
    expect(screen.getByLabelText('New session prompt')).toHaveValue('Triage the open bugs')
    await userEvent.click(screen.getByRole('button', { name: 'Create session' }))
    await waitFor(() => expect(onDispatch).toHaveBeenCalledTimes(1))
    expect(onDispatch.mock.calls[0][0]).toMatchObject({
      presetId: 'p1', repo: 'o/b', model: 'sonnet', initialPrompt: 'Triage the open bugs',
    })
  })

  it('never overwrites a prompt already typed', async () => {
    mount({ presets: [preset({ initialPrompt: 'from the preset' })] })
    await userEvent.type(screen.getByLabelText('New session prompt'), 'mine')
    await openAdd()
    await userEvent.click(screen.getByRole('menuitem', { name: /Presets/ }))
    await userEvent.click(screen.getByRole('menuitem', { name: /Bug bash/ }))
    expect(screen.getByLabelText('New session prompt')).toHaveValue('mine')
  })

  it('says there are no presets yet, rather than an empty menu', async () => {
    mount({ presets: [] })
    await openAdd()
    await userEvent.click(screen.getByRole('menuitem', { name: /Presets/ }))
    const empty = screen.getByRole('menuitem', { name: /No presets yet/ })
    expect(empty).toHaveAttribute('aria-disabled', 'true')
  })

  it('a template inserts its server-rendered text with the variable defaults', async () => {
    const renderTemplate = vi.fn().mockResolvedValue('Review the parser')
    mount({ templates: [template()], renderTemplate })
    await openAdd()
    await userEvent.click(screen.getByRole('menuitem', { name: /Templates/ }))
    await userEvent.click(screen.getByRole('menuitem', { name: /Review/ }))
    expect(renderTemplate).toHaveBeenCalledWith('t1', { area: 'the parser' })
    await waitFor(() => expect(screen.getByLabelText('New session prompt')).toHaveValue('Review the parser'))
  })

  it('a template lands at the caret, keeping the draft around it', async () => {
    const renderTemplate = vi.fn().mockResolvedValue('X')
    mount({ templates: [template()], renderTemplate })
    const ta = screen.getByLabelText('New session prompt') as HTMLTextAreaElement
    await userEvent.type(ta, 'ab')
    ta.setSelectionRange(1, 1)
    await openAdd()
    await userEvent.click(screen.getByRole('menuitem', { name: /Templates/ }))
    await userEvent.click(screen.getByRole('menuitem', { name: /Review/ }))
    await waitFor(() => expect(ta).toHaveValue('aXb'))
  })

  it('a preset requested from outside (⌘K) sets the chip, and is acknowledged', async () => {
    const onPresetRequestHandled = vi.fn()
    mount({ presets: [preset()], presetRequest: { id: 'p1' }, onPresetRequestHandled })
    expect(await screen.findByText('Preset: Bug bash')).toBeInTheDocument()
    expect(onPresetRequestHandled).toHaveBeenCalledTimes(1)
  })
})
