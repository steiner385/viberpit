/**
 * D-16a in the session composer: the `+` menu's Style › submenu (the session's own
 * style list, the current one checked, a pick posts), hidden for Codex and for an
 * empty list (Review Focus 5); `/plan` and `/output-style` in the slash palette as the
 * server's catalog lists them; and a refused app command — sent with more
 * text, acked `failed` — comes back into the box instead of being lost.
 */
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Session } from '@/api/types'
import type { Command } from '@/session/events'
import { Composer } from './Composer'
import { OUTPUT_STYLE_COPY, useOutputStyleEntry } from './outputStyle'

const mutate = vi.fn()
let sessions: Partial<Session>[] = []
vi.mock('@/api/hooks', () => ({
  useSessions: () => ({ data: sessions }),
  useSetOutputStyle: () => ({ mutate, isPending: false }),
}))

const STYLES = ['default', 'Proactive', 'Concise', 'Explanatory', 'Learning', 'plugin:Terse']

function claude(over: Partial<Session> = {}): Partial<Session> {
  return { id: 's1', engine: 'claude_code', outputStyle: 'Concise', availableOutputStyles: STYLES, ...over }
}

function StyleComposer({ commands = [] }: { commands?: Command[] }) {
  const entry = useOutputStyleEntry('s1')
  return (
    <Composer
      status={{ state: 'idle', count: 0 }} onPrompt={() => {}} onInterrupt={() => {}}
      sessionId="s1" commands={commands} extraPlusEntries={entry ? [entry] : []}
    />
  )
}

async function openAdd() {
  await userEvent.click(screen.getByRole('button', { name: 'Add' }))
  return screen.getByRole('menu', { name: 'Add' })
}

beforeEach(() => { mutate.mockClear(); localStorage.clear() })

describe('the + menu Style › submenu', () => {
  it('lists the session styles in CLI order with the current one checked, and the cache-miss tooltip', async () => {
    sessions = [claude()]
    render(<StyleComposer />)
    const menu = await openAdd()
    const style = within(menu).getByRole('menuitem', { name: /Style ›/ })
    expect(style).toHaveAttribute('title', OUTPUT_STYLE_COPY)
    expect(OUTPUT_STYLE_COPY).toBe('Changing style mid-session re-sends the system prompt on the next turn.')
    await userEvent.click(style)
    const sub = screen.getByRole('menu', { name: 'Style' })
    const items = within(sub).getAllByRole('menuitemradio')
    expect(items.map((i) => i.textContent?.replace(/\d$/, ''))).toEqual(STYLES)
    expect(within(sub).getByRole('menuitemradio', { name: /Concise/ })).toHaveAttribute('aria-checked', 'true')
    expect(within(sub).getByRole('menuitemradio', { name: /Learning/ })).toHaveAttribute('aria-checked', 'false')
    expect(within(sub).getByRole('menuitemradio', { name: /Learning/ })).toHaveAttribute('title', OUTPUT_STYLE_COPY)
  })

  it('choosing a style posts it for this session', async () => {
    sessions = [claude()]
    render(<StyleComposer />)
    await userEvent.click(within(await openAdd()).getByRole('menuitem', { name: /Style ›/ }))
    await userEvent.click(screen.getByRole('menuitemradio', { name: /plugin:Terse/ }))
    expect(mutate).toHaveBeenCalledWith({ id: 's1', style: 'plugin:Terse' })
  })

  it('choosing the current style posts nothing', async () => {
    sessions = [claude()]
    render(<StyleComposer />)
    await userEvent.click(within(await openAdd()).getByRole('menuitem', { name: /Style ›/ }))
    await userEvent.click(screen.getByRole('menuitemradio', { name: /Concise/ }))
    expect(mutate).not.toHaveBeenCalled()
  })

  it('checks none while the effective style is not known yet', async () => {
    sessions = [claude({ outputStyle: null })]
    render(<StyleComposer />)
    await userEvent.click(within(await openAdd()).getByRole('menuitem', { name: /Style ›/ }))
    for (const item of screen.getAllByRole('menuitemradio')) expect(item).toHaveAttribute('aria-checked', 'false')
  })

  it.each([
    ['a Codex session', { engine: 'codex' as const }],
    ['an empty style list', { availableOutputStyles: [] }],
    ['a session that reported no list', { availableOutputStyles: undefined }],
  ])('is hidden for %s', async (_case, over) => {
    sessions = [claude(over)]
    render(<StyleComposer />)
    const menu = await openAdd()
    expect(within(menu).queryByRole('menuitem', { name: /Style/ })).toBeNull()
    expect(within(menu).getByRole('menuitem', { name: /Attach file/ })).toBeInTheDocument()
  })
})

describe('/plan and /output-style in the slash palette', () => {
  const PLAN: Command = { name: 'plan', description: 'Turn plan mode on or off (bare: toggle)', argumentHint: '[on|off]', kind: 'control', source: 'app' }
  const STYLE: Command = { name: 'output-style', description: "Set this session's output style", argumentHint: '[style]', kind: 'control', source: 'app' }

  // Listing is the server catalog's call (gated on flagSettings, so absent for Codex);
  // the e2e Codex case and the server's catalog tests are the checks for that gate.
  it('lists both where the catalog does (Claude)', async () => {
    sessions = [claude()]
    render(<StyleComposer commands={[PLAN, STYLE]} />)
    await userEvent.type(screen.getByLabelText('Message'), '/')
    const list = screen.getByRole('listbox')
    expect(list).toHaveTextContent('plan')
    expect(list).toHaveTextContent('output-style')
  })

  it('/output-style <name> runs as a command, not a prompt', async () => {
    sessions = [claude()]
    const onCommand = vi.fn()
    const onPrompt = vi.fn()
    render(<Composer status={{ state: 'idle', count: 0 }} onPrompt={onPrompt} onInterrupt={() => {}}
      sessionId="s1" commands={[STYLE]} onCommand={onCommand} />)
    await userEvent.type(screen.getByLabelText('Message'), '/output-style Learning{Enter}')
    expect(onCommand).toHaveBeenCalledWith('output-style', 'Learning')
    expect(onPrompt).not.toHaveBeenCalled()
  })
})

describe('a refused app command keeps its text (D-16a)', () => {
  function Harness({ returned, onPrompt }: { returned: { text: string } | null; onPrompt: () => void }) {
    return (
      <Composer status={{ state: 'idle', count: 0 }} onPrompt={onPrompt} onInterrupt={() => {}}
        sessionId="s1" returnedDraft={returned} />
    )
  }

  it('a prompt acked failed comes back into the empty box', async () => {
    sessions = [claude()]
    const onPrompt = vi.fn()
    const { rerender } = render(<Harness returned={null} onPrompt={onPrompt} />)
    const box = screen.getByLabelText('Message')
    await userEvent.type(box, '/plan on{Shift>}{Enter}{/Shift}and more{Enter}')
    expect(onPrompt).toHaveBeenCalledWith('/plan on\nand more', [])
    expect(box).toHaveValue('')
    // The server refuses it (more text after an app command) and acks `failed`.
    rerender(<Harness returned={{ text: '/plan on\nand more' }} onPrompt={onPrompt} />)
    expect(box).toHaveValue('/plan on\nand more')
  })

  it('never overwrites a draft typed since the send: the returned text goes below it', async () => {
    sessions = [claude()]
    const { rerender } = render(<Harness returned={null} onPrompt={() => {}} />)
    const box = screen.getByLabelText('Message')
    await userEvent.type(box, 'something new')
    rerender(<Harness returned={{ text: '/fast on\nx' }} onPrompt={() => {}} />)
    expect(box).toHaveValue('something new\n\n/fast on\nx')
  })

  it('two failures in a row keep both texts', () => {
    sessions = [claude()]
    const { rerender } = render(<Harness returned={null} onPrompt={() => {}} />)
    const box = screen.getByLabelText('Message')
    rerender(<Harness returned={{ text: '/plan on\nfirst' }} onPrompt={() => {}} />)
    rerender(<Harness returned={{ text: '/fast on\nsecond' }} onPrompt={() => {}} />)
    expect(box).toHaveValue('/plan on\nfirst\n\n/fast on\nsecond')
  })

  it('ignores a prompt that was returned before this composer mounted', () => {
    sessions = [claude()]
    render(<Harness returned={{ text: 'old' }} onPrompt={() => {}} />)
    expect(screen.getByLabelText('Message')).toHaveValue('')
  })
})
