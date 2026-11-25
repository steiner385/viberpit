import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { UserFacingToolCard } from './UserFacingToolCard'
import { FileRefContext, TranscriptSessionContext, type FileRefActions } from './fileRefContext'
import type { ToolBlock } from '@/session/artifacts'
import type { UserFacingToolName } from '@/session/userFacingTools'

const { useSessionFile, useSentFiles } = vi.hoisted(() => ({ useSessionFile: vi.fn(), useSentFiles: vi.fn() }))
vi.mock('@/api/hooks', () => ({ useSessionFile }))
vi.mock('@/api/sentFilesHooks', () => ({ useSentFiles }))
vi.mock('./MermaidDiagram', () => ({ MermaidDiagram: () => null }))

const PNG = 'iVBORw0KGgo='

function call(name: UserFacingToolName, input: Record<string, unknown>, result?: ToolBlock['result']): ToolBlock {
  return { kind: 'tool', id: 'u1', toolId: 'u1', name, toolKind: 'unknown', input, result }
}

const delivered = (text: string): ToolBlock['result'] => ({ ok: true, payload: { content: [{ type: 'text', text }] } })

function renderCard(block: ToolBlock, actions: Partial<FileRefActions> | null = {}, transcriptSessionId: string | null = null) {
  const value = actions ? { sessionId: 's1', cwd: '/home/x/repo', openFile: vi.fn(), ...actions } : null
  render(
    <TranscriptSessionContext.Provider value={transcriptSessionId}>
      <FileRefContext.Provider value={value}>
        <UserFacingToolCard block={block} name={block.name as UserFacingToolName} />
      </FileRefContext.Provider>
    </TranscriptSessionContext.Provider>,
  )
  return value
}

const published = (...files: Array<{ path: string; url: string | null; reason?: string }>) => ({
  isPending: false,
  data: { files: files.map((f) => ({ mediaType: f.url ? 'image/png' : null, reason: null, ...f })) },
})

// A real-shaped call, as the CLI emitted it in a ViberPit session.
const SHOT = { files: ['/home/x/repo/.rendertest/shot.png'], caption: 'Settings page', status: 'normal' }

beforeEach(() => {
  useSessionFile.mockReset().mockReturnValue({ isPending: false, data: { image: { mediaType: 'image/png', data: PNG } } })
  useSentFiles.mockReset().mockReturnValue({ isPending: false, data: undefined })
})

describe('SendUserFile card', () => {
  it('shows the caption and a worktree image inline, read through the Files API', async () => {
    const user = userEvent.setup()
    const ctx = renderCard(call('SendUserFile', SHOT, delivered('1 files delivered to user. /home/x/repo/.rendertest/shot.png → file_uuid: abc')))!
    const card = await screen.findByTestId('send-user-file-card')
    expect(within(card).getByText('Settings page')).toBeInTheDocument()
    const img = within(card).getByRole('img', { name: 'shot.png' })
    expect(img).toHaveAttribute('src', `data:image/png;base64,${PNG}`)
    expect(useSessionFile).toHaveBeenCalledWith('s1', '.rendertest/shot.png')
    await user.click(screen.getByRole('button', { name: /Open .*shot\.png in Files/ }))
    expect(ctx.openFile).toHaveBeenCalledWith('.rendertest/shot.png', null)
  })

  it('says plainly a file outside the worktree the daemon could not publish is on the session host', async () => {
    useSentFiles.mockReturnValue(published({ path: '/tmp/report.pdf', url: null, reason: 'not_image' }))
    renderCard(call('SendUserFile', { files: ['/tmp/report.pdf'], status: 'normal' }, delivered('1 files delivered to user.')))
    const card = await screen.findByTestId('send-user-file-card')
    expect(within(card).getByText(/on the session host, outside its worktree/)).toBeInTheDocument()
    expect(within(card).getByRole('button', { name: '/tmp/report.pdf' })).toBeInTheDocument()
    expect(useSessionFile).not.toHaveBeenCalled()
    expect(useSentFiles).toHaveBeenCalledWith('s1', 'u1', { enabled: true })
  })

  it('renders an image outside the worktree from the attachment the daemon published', async () => {
    useSentFiles.mockReturnValue(published({ path: '/tmp/jobs/chart.png', url: '/api/attachments/a1' }))
    renderCard(call('SendUserFile', { files: ['/tmp/jobs/chart.png'], status: 'normal' }, delivered('ok')))
    const card = await screen.findByTestId('send-user-file-card')
    expect(within(card).getByRole('img', { name: 'chart.png' })).toHaveAttribute('src', '/api/attachments/a1')
    expect(within(card).queryByText(/on the session host/)).toBeNull()
    expect(useSessionFile).not.toHaveBeenCalled()
  })

  it('offers a published attach-mode file as a download, not inline', async () => {
    useSentFiles.mockReturnValue(published({ path: '/tmp/chart.png', url: '/api/attachments/a1' }))
    renderCard(call('SendUserFile', { files: ['/tmp/chart.png'], display: 'attach', status: 'normal' }, delivered('ok')))
    const card = await screen.findByTestId('send-user-file-card')
    expect(within(card).getByRole('link', { name: 'chart.png' })).toHaveAttribute('href', '/api/attachments/a1')
    expect(within(card).queryByRole('img')).toBeNull()
  })

  it('shows a loading state while an outside image is being published', async () => {
    useSentFiles.mockReturnValue({ isPending: true, data: undefined })
    renderCard(call('SendUserFile', { files: ['/tmp/chart.png'], status: 'normal' }, delivered('ok')))
    expect(await screen.findByText('Loading image…')).toBeInTheDocument()
  })

  it('does not ask the daemon while the call is still pending (its line may not be on disk)', async () => {
    renderCard(call('SendUserFile', { files: ['/tmp/chart.png'], status: 'normal' }))
    expect(await screen.findByText('Sending…')).toBeInTheDocument()
    expect(useSentFiles).toHaveBeenCalledWith('s1', 'u1', { enabled: false })
    expect(screen.queryByText('Loading image…')).toBeNull()
  })

  it('does not ask the daemon when every file is in the worktree', async () => {
    renderCard(call('SendUserFile', SHOT, delivered('ok')))
    await screen.findByTestId('send-user-file-card')
    expect(useSentFiles).toHaveBeenCalledWith('s1', 'u1', { enabled: false })
  })

  it('in the archived reader (no worktree) publishes through the transcript session id', async () => {
    useSentFiles.mockReturnValue(published({ path: SHOT.files[0], url: '/api/attachments/a2' }))
    renderCard(call('SendUserFile', SHOT, delivered('ok')), null, 'archived-1')
    const card = await screen.findByTestId('send-user-file-card')
    expect(useSentFiles).toHaveBeenCalledWith('archived-1', 'u1', { enabled: true })
    expect(within(card).getByRole('img', { name: 'shot.png' })).toHaveAttribute('src', '/api/attachments/a2')
  })

  it('links a non-image worktree file to Files (cwd-relative too)', async () => {
    renderCard(call('SendUserFile', { files: ['docs/notes.md'], status: 'normal' }, delivered('ok')))
    expect(await screen.findByRole('button', { name: 'Open docs/notes.md in Files' })).toBeInTheDocument()
  })

  it('says "No files" for a call that names none', async () => {
    renderCard(call('SendUserFile', { files: [], status: 'normal' }, delivered('0 files delivered to user.')))
    const card = await screen.findByTestId('send-user-file-card')
    expect(card).toHaveTextContent('No files')
    expect(card).not.toHaveTextContent('Shared 0 files')
  })

  it('shows a pending state until the result lands', async () => {
    renderCard(call('SendUserFile', SHOT))
    expect(await screen.findByText('Sending…')).toBeInTheDocument()
  })

  it('shows the error when delivery failed', async () => {
    renderCard(call('SendUserFile', SHOT, { ok: false, payload: { content: [{ type: 'text', text: 'File not found' }] } }))
    expect(await screen.findByText('Not delivered: File not found')).toBeInTheDocument()
  })

  it('without a worktree provider or a session id names the file as host-only', async () => {
    renderCard(call('SendUserFile', SHOT, delivered('ok')), null)
    const card = await screen.findByTestId('send-user-file-card')
    expect(within(card).getByText(/a file on the session host/)).toBeInTheDocument()
    expect(within(card).queryByRole('img')).toBeNull()
    expect(useSentFiles).toHaveBeenCalledWith(null, 'u1', { enabled: true })
  })
})

describe('SendUserMessage card', () => {
  it('renders the message as prose plus its attachments', async () => {
    renderCard(call('SendUserMessage', { message: 'All **done**.', attachments: ['/tmp/out.log'], extra: 1 }, delivered('ok')))
    const card = await screen.findByTestId('send-user-message-card')
    expect(await within(card).findByText('done')).toBeInTheDocument()
    expect(within(card).getByRole('button', { name: '/tmp/out.log' })).toBeInTheDocument()
  })

  it('degrades on a malformed input instead of throwing', async () => {
    renderCard(call('SendUserMessage', { message: { nested: true }, attachments: 'nope' }))
    expect(await screen.findByText('(empty message)')).toBeInTheDocument()
  })
})

describe('ReportFindings card', () => {
  it('lists findings in order with severity, location, verdict and outcome', async () => {
    const findings = [
      { file: 'src/a.ts', line: 12, summary: 'Null deref', failure_scenario: 'x is null on replay', severity: 'important', verdict: 'CONFIRMED', outcome: 'fixed' },
      { file: 'src/b.ts', line: 3, summary: 'Naming', failure_scenario: 'y'.repeat(300), severity: 'nit' },
    ]
    renderCard(call('ReportFindings', { findings, level: 'high' }, delivered('ok')))
    const card = await screen.findByTestId('report-findings-card')
    expect(within(card).getByText('· 2')).toBeInTheDocument()
    expect(within(card).getByText('high')).toBeInTheDocument()
    const items = within(card).getAllByRole('listitem')
    expect(items[0]).toHaveTextContent('Important')
    expect(items[0]).toHaveTextContent('Null deref')
    expect(within(items[0]).getByRole('button', { name: 'Open src/a.ts:12 in Files' })).toBeInTheDocument()
    expect(items[0]).toHaveTextContent('CONFIRMED')
    expect(items[0]).toHaveTextContent('fixed')
    expect(items[1]).toHaveTextContent('Nit')
    expect(within(items[1]).getByText('Failure scenario')).toBeInTheDocument()
  })

  it('says so when there are no findings', async () => {
    renderCard(call('ReportFindings', { findings: [] }, delivered('ok')))
    expect(await screen.findByText('No findings reported.')).toBeInTheDocument()
  })
})

describe('PushNotification card', () => {
  it('is a one-line notice', async () => {
    renderCard(call('PushNotification', { message: 'Build finished', status: 'proactive' }, delivered('ok')))
    expect(await screen.findByTestId('push-notification-card')).toHaveTextContent('Notification: Build finished')
  })
})
