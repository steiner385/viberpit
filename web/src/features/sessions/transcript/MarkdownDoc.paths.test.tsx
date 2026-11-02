// D-7: a transcript path under the live session's cwd opens the Files tab; its
// secondary menu copies it or attaches it to the composer as `@path`. Without
// the provider (archived reader, sub-agent page, assistant drawer) or outside
// the cwd, a path keeps today's click-to-copy.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MarkdownDoc } from './MarkdownDoc'
import { FileRefContext, type FileRefActions } from './fileRefContext'

vi.mock('./MermaidDiagram', () => ({ MermaidDiagram: () => null }))
// userEvent installs its own getter-only clipboard, so assert at the app's copy seam.
const { writeText } = vi.hoisted(() => ({ writeText: vi.fn() }))
vi.mock('@/lib/copyText', () => ({ copyText: writeText }))

function renderWith(md: string, actions: Partial<FileRefActions> | null) {
  const value = actions ? { cwd: '/repo', openFile: vi.fn(), ...actions } : null
  render(
    <FileRefContext.Provider value={value}>
      <MarkdownDoc>{md}</MarkdownDoc>
    </FileRefContext.Provider>,
  )
  return value
}

beforeEach(() => {
  writeText.mockReset().mockResolvedValue(true)
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
