// D-7: a transcript path under the live session's cwd opens the Files tab; its
// secondary menu copies it or attaches it to the composer as `@path`. Without
// the provider (archived reader, sub-agent page, assistant drawer) or outside
// the cwd, a path keeps today's click-to-copy.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import userEvent from '@testing-library/user-event'
import { MarkdownDoc } from './MarkdownDoc'
import { FileRefContext, type FileRefActions } from './fileRefContext'
import { AppRouteContext } from '@/shell/appRouteContext'
import { isAppRoute } from '@/shell/routes'

vi.mock('./MermaidDiagram', () => ({ MermaidDiagram: () => null }))
// userEvent installs its own getter-only clipboard, so assert at the app's copy seam.
const { writeText } = vi.hoisted(() => ({ writeText: vi.fn() }))
vi.mock('@/lib/copyText', () => ({ copyText: writeText }))
const { useSessionFile } = vi.hoisted(() => ({ useSessionFile: vi.fn() }))
vi.mock('@/api/hooks', () => ({ useSessionFile }))

function renderWith(md: string, actions: Partial<FileRefActions> | null) {
  const value = actions ? { sessionId: 's1', cwd: '/repo', openFile: vi.fn(), ...actions } : null
  render(
    <AppRouteContext.Provider value={isAppRoute}>
      <FileRefContext.Provider value={value}>
        <MarkdownDoc>{md}</MarkdownDoc>
      </FileRefContext.Provider>
    </AppRouteContext.Provider>,
  )
  return value
}

beforeEach(() => {
  writeText.mockReset().mockResolvedValue(true)
  useSessionFile.mockReset().mockReturnValue({ isPending: true, data: undefined })
})

describe('transcript file paths (D-7)', () => {
  it('an absolute path under cwd opens the Files tab at its relative path and line', async () => {
    const user = userEvent.setup()
    const ctx = renderWith('see `/repo/src/a.ts:12`', {})!
    await user.click(screen.getByRole('button', { name: 'Open /repo/src/a.ts:12 in Files' }))
    expect(ctx.openFile).toHaveBeenCalledWith('src/a.ts', 12)
    expect(writeText).not.toHaveBeenCalled()
  })

  it('a relative path opens directly', async () => {
    const user = userEvent.setup()
    const ctx = renderWith('see `src/a.ts`', {})!
    await user.click(screen.getByRole('button', { name: 'Open src/a.ts in Files' }))
    expect(ctx.openFile).toHaveBeenCalledWith('src/a.ts', null)
  })

  it('a path outside cwd stays copy-only', async () => {
    const user = userEvent.setup()
    const ctx = renderWith('see `/etc/hosts`', {})!
    expect(screen.queryByRole('button', { name: /in Files/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /more actions/i })).toBeNull()
    await user.click(screen.getByRole('button', { name: '/etc/hosts' }))
    expect(writeText).toHaveBeenCalledWith('/etc/hosts')
    expect(ctx.openFile).not.toHaveBeenCalled()
  })

  it('under a subdirectory cwd, an absolute path opens cwd-relative — as the CLI and the server resolve it', async () => {
    const user = userEvent.setup()
    const ctx = renderWith('see `/repo/pkg/src/a.ts:3` and `/repo/top.ts`', { cwd: '/repo/pkg' })!
    await user.click(screen.getByRole('button', { name: 'Open /repo/pkg/src/a.ts:3 in Files' }))
    expect(ctx.openFile).toHaveBeenCalledWith('src/a.ts', 3)
    // Above the cwd: the server refuses the read, so it is never offered as a link.
    expect(screen.queryByRole('button', { name: 'Open /repo/top.ts in Files' })).toBeNull()
  })

  it('a sibling directory sharing the cwd prefix stays copy-only', () => {
    renderWith('see `/repo-evil/x.ts`', {})
    expect(screen.queryByRole('button', { name: /in Files/ })).toBeNull()
  })

  it.each([
    'app.state.sessions', 'asyncio.sleep', '1.2.3', 'v2.1', 'localhost:8080/api', '127.0.0.1:8787/api/x',
    'C:/repo/a.ts',
  ])('%j is copy-only, not a Files link', (token) => {
    renderWith(`see \`${token}\``, {})
    expect(screen.queryByRole('button', { name: /in Files/ })).toBeNull()
  })

  it.each(['src/a.ts', 'README.md', 'pyproject.toml'])('%j is a Files link', (token) => {
    renderWith(`see \`${token}\``, {})
    expect(screen.getByRole('button', { name: `Open ${token} in Files` })).toBeInTheDocument()
  })

  it('the menu copies the path', async () => {
    const user = userEvent.setup()
    renderWith('see `/repo/src/a.ts:12`', { attachMention: vi.fn() })
    await user.click(screen.getByRole('button', { name: 'More actions for /repo/src/a.ts:12' }))
    await user.click(screen.getByRole('menuitem', { name: 'Copy path' }))
    // The path only — the `:line` suffix is not part of it.
    expect(writeText).toHaveBeenCalledWith('/repo/src/a.ts')
  })

  it('Attach as @ hands the worktree-relative path to the composer', async () => {
    const user = userEvent.setup()
    const attachMention = vi.fn()
    const ctx = renderWith('see `/repo/src/a.ts:12`', { attachMention })!
    await user.click(screen.getByRole('button', { name: 'More actions for /repo/src/a.ts:12' }))
    await user.click(screen.getByRole('menuitem', { name: 'Attach as @' }))
    expect(attachMention).toHaveBeenCalledWith('src/a.ts')
    expect(ctx.openFile).not.toHaveBeenCalled()
  })

  it('without a composer to attach to, the menu offers only Copy path', async () => {
    const user = userEvent.setup()
    renderWith('see `src/a.ts`', {})
    await user.click(screen.getByRole('button', { name: 'More actions for src/a.ts' }))
    expect(screen.getByRole('menuitem', { name: 'Copy path' })).toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Attach as @' })).toBeNull()
  })

  it('the archived reader (no provider) has no Files link — copy-only', async () => {
    const user = userEvent.setup()
    renderWith('see `src/a.ts:3`', null)
    expect(screen.queryByRole('button', { name: /in Files/ })).toBeNull()
    await user.click(screen.getByRole('button', { name: 'src/a.ts:3' }))
    expect(writeText).toHaveBeenCalledWith('src/a.ts:3')
  })
})

describe('markdown links to local paths', () => {
  it('a relative link in the worktree opens Files, labelled, with no href', async () => {
    const user = userEvent.setup()
    const ctx = renderWith('see [notes](.rendertest/notes.md)', {})!
    expect(document.querySelector('a')).toBeNull()
    const link = screen.getByRole('button', { name: 'notes' })
    expect(link).toHaveAttribute('title', 'Open .rendertest/notes.md in Files')
    await user.click(link)
    expect(ctx.openFile).toHaveBeenCalledWith('.rendertest/notes.md', null)
  })

  it.each([
    ['[a](src/a.ts#L12)', 'src/a.ts', 12],
    ['[a](./src/a.ts:7)', 'src/a.ts', 7],
    ['[a](file:///repo/src/a.ts#L3-L9)', 'src/a.ts', 3],
    ['[a](/repo/docs/my%20notes.md)', 'docs/my notes.md', null],
  ])('%s opens %s at line %s', async (md, rel, line) => {
    const user = userEvent.setup()
    const ctx = renderWith(md, {})!
    expect(document.querySelector('a')).toBeNull()
    await user.click(screen.getByRole('button', { name: 'a' }))
    expect(ctx.openFile).toHaveBeenCalledWith(rel, line)
  })

  it.each([
    ['outside the worktree', '[x](file:///etc/hostname)', {}],
    ['outside the worktree (relative escape)', '[x](../other/a.md)', {}],
    ['without a provider', '[x](docs/x.md)', null],
  ])('%s it is plain text titled with the path — no anchor, no button', (_why, md, actions) => {
    renderWith(md, actions)
    expect(document.querySelector('a')).toBeNull()
    expect(screen.queryByRole('button')).toBeNull()
    expect(screen.getByText('x').tagName).toBe('SPAN')
    expect(screen.getByText('x').getAttribute('title')).toMatch(/hostname|a\.md|x\.md/)
  })

  it('a code label inside a path link is one click target — no copy chip nested in it', async () => {
    const user = userEvent.setup()
    const ctx = renderWith('[`docs/x.md`](docs/x.md)', {})!
    expect(screen.getAllByRole('button', { name: 'docs/x.md' })).toHaveLength(1)
    expect(document.querySelector('button [role="button"], button button')).toBeNull()
    await user.click(screen.getByText('docs/x.md'))
    expect(ctx.openFile).toHaveBeenCalledWith('docs/x.md', null)
    expect(writeText).not.toHaveBeenCalled()
  })

  it('a code label inside a web link is plain code, not a PathRef', () => {
    const { container } = render(<MarkdownDoc>{'[`docs/x.md`](https://example.com/x)'}</MarkdownDoc>)
    expect(screen.getByRole('link')).toHaveAttribute('href', 'https://example.com/x')
    expect(container.querySelector('a [role="button"], a button')).toBeNull()
  })

  it.each([
    ['in a live session', {}],
    ['in the assistant drawer (no provider)', null],
  ])('an in-app route stays a working same-tab link %s', (_where, actions) => {
    renderWith('open [the session](/sessions/abc?tab=files)', actions)
    const link = screen.getByRole('link', { name: 'the session' })
    expect(link).toHaveAttribute('href', '/sessions/abc?tab=files')
    expect(link).not.toHaveAttribute('target')
  })

  it('inside the router an in-app route navigates in place', async () => {
    const user = userEvent.setup()
    function Where() { return <span data-testid="where">{useLocation().pathname}</span> }
    render(
      <AppRouteContext.Provider value={isAppRoute}>
        <MemoryRouter initialEntries={['/sessions']}>
          <MarkdownDoc>{'[go](/sessions/abc)'}</MarkdownDoc>
          <Where />
        </MemoryRouter>
      </AppRouteContext.Provider>,
    )
    await user.click(screen.getByRole('link', { name: 'go' }))
    expect(screen.getByTestId('where')).toHaveTextContent('/sessions/abc')
  })

  it('an absolute path in the worktree that is not a page still opens Files', async () => {
    const user = userEvent.setup()
    const ctx = renderWith('[a](/repo/sessions/abc.md)', { cwd: '/repo' })!
    await user.click(screen.getByRole('button', { name: 'a' }))
    expect(ctx.openFile).toHaveBeenCalledWith('sessions/abc.md', null)
  })

  it.each(['file://otherhost/share/a.md', 'file://%zz/a.md', 'file:'])('%s (not a local path) is text, never a file: href', (href) => {
    renderWith(`[x](${href})`, {})
    expect(document.querySelector('a')).toBeNull()
    expect(screen.getByText('x').tagName).toBe('SPAN')
  })

  it('a path that is not a page of the app is still plain text', () => {
    renderWith('[x](/nope/abc)', null)
    expect(document.querySelector('a')).toBeNull()
  })

  it('a served attachment link stays a link', () => {
    renderWith('[shot](/api/attachments/abc)', {})
    expect(screen.getByRole('link', { name: 'shot' })).toHaveAttribute('href', '/api/attachments/abc')
  })

  it('web links, mailto and footnotes are unchanged', () => {
    renderWith('[w](https://example.com) [m](mailto:a@b.c) note[^1]\n\n[^1]: foot', {})
    expect(screen.getByRole('link', { name: 'w' })).toHaveAttribute('target', '_blank')
    expect(screen.getByRole('link', { name: 'm' })).toHaveAttribute('href', 'mailto:a@b.c')
    const ref = document.querySelector('sup a')!
    expect(ref.getAttribute('href')).toMatch(/^#markdown-.*fn-1$/)
  })

  it('untrusted text never routes a local path — it stays text', () => {
    render(
      <FileRefContext.Provider value={{ sessionId: 's1', cwd: '/repo', openFile: vi.fn() }}>
        <MarkdownDoc untrusted>{'[x](docs/x.md) [y](file:///repo/a.ts)'}</MarkdownDoc>
      </FileRefContext.Provider>,
    )
    expect(document.querySelector('a')).toBeNull()
    expect(screen.queryByRole('button')).toBeNull()
  })
})

describe('markdown images at local paths', () => {
  it('an in-worktree image loads through the Files read and opens Files on click', async () => {
    const user = userEvent.setup()
    useSessionFile.mockReturnValue({ isPending: false, data: { image: { mediaType: 'image/png', data: 'iVBORw0K' } } })
    const ctx = renderWith('![shot](.rendertest/shot.png)', {})!
    expect(useSessionFile).toHaveBeenCalledWith('s1', '.rendertest/shot.png')
    const img = screen.getByRole('img', { name: 'shot' })
    expect(img).toHaveAttribute('src', 'data:image/png;base64,iVBORw0K')
    await user.click(screen.getByRole('button', { name: 'Open .rendertest/shot.png in Files' }))
    expect(ctx.openFile).toHaveBeenCalledWith('.rendertest/shot.png', null)
  })

  it('an absolute in-worktree path is read cwd-relative; loading shows a notice', () => {
    renderWith('![shot](/repo/out/shot.png)', {})
    expect(useSessionFile).toHaveBeenCalledWith('s1', 'out/shot.png')
    expect(screen.getByText('Loading image…')).toBeInTheDocument()
  })

  it('a worktree file that is not an image names its path', () => {
    useSessionFile.mockReturnValue({ isPending: false, data: { binary: false, text: 'x' } })
    renderWith('![shot](out/shot.png)', {})
    expect(screen.queryByRole('img')).toBeNull()
    expect(screen.getByText(/Image unavailable/)).toHaveTextContent('out/shot.png')
  })

  it.each([
    ['outside the worktree', '![Settings](/tmp/shot.png)', {}, /^Settings is at \/tmp\/shot\.png on the session host, outside its worktree,.*share_artifact/],
    ['without a provider', '![Settings](out/shot.png)', null, /^Settings is a file on the session host \(out\/shot\.png\), so it can't be shown here.*share_artifact/],
  ])('%s it explains the image is on the session host and never fetches it', (_why, md, actions, text) => {
    renderWith(md, actions)
    expect(screen.queryByRole('img')).toBeNull()
    expect(useSessionFile).not.toHaveBeenCalled()
    expect(screen.getByText(/on the session host/)).toHaveTextContent(text)
  })

  it('an image inside a path link is the bare image — one click target', () => {
    useSessionFile.mockReturnValue({ isPending: false, data: { image: { mediaType: 'image/png', data: 'AAAA' } } })
    renderWith('[![shot](out/shot.png)](docs/x.md)', {})
    expect(screen.getAllByRole('button', { name: /in Files|^shot$/ })).toHaveLength(1)
    expect(document.querySelector('button button')).toBeNull()
  })
})
