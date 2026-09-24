import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { IssueDispositionDialog } from './IssueDispositionDialog'

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

const detail = {
  id: 'w1', repo: 'o/r', number: 1, title: 'Account warning', body: '**Check credentials** <script>alert(1)</script>', state: 'open', url: 'https://github.com/o/r/issues/1', version: 'v1',
  ref: 'o/r#1', holdKind: null, holdReason: null, holdBy: null, holdSetAt: null,
}
const fetchMock = vi.fn()
beforeEach(() => { vi.stubGlobal('fetch', fetchMock); fetchMock.mockReset() })
function reply(data: unknown, status = 200) { return { ok: status < 400, status, json: async () => data } }
function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const onClose = vi.fn()
  render(<MemoryRouter><QueryClientProvider client={qc}><IssueDispositionDialog itemId="w1" onClose={onClose} /></QueryClientProvider></MemoryRouter>)
  return { onClose, qc }
}
const click = (name: string) => fireEvent.click(screen.getByRole('button', { name }))
describe('IssueDispositionDialog', () => {
  it('reads the issue safely without sending a decision', async () => {
    fetchMock.mockResolvedValue(reply(detail))
    mount()
    expect(await screen.findByText('Account warning')).toBeInTheDocument()
    // The body renders through `<Markdown>`, whose renderer is code-split
    // (#1411): until the chunk lands the body is its own source text, so the
    // sanitiser assertion below has to wait for the rendered form.
    expect(await screen.findByText('Check credentials')).toBeInTheDocument()
    expect(document.querySelector('script')).toBeNull()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0][1].method).toBe('GET')
  })
  it.each([['Mark resolved', 'Confirm resolved', 'resolve'], ['Won’t do', 'Confirm won’t do', 'dismiss']])('confirms %s before writing and invalidates affected views', async (label, confirm, action) => {
    fetchMock.mockResolvedValueOnce(reply(detail)).mockResolvedValueOnce(reply({ ok: true, item: { ...detail, state: 'closed', version: 'v2' } }))
    const { onClose, qc } = mount()
    const invalidate = vi.spyOn(qc, 'invalidateQueries')
    await screen.findByText('Account warning')
    click(label)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(screen.getByText(/does not repair/)).toBeInTheDocument()
    click(confirm)
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce())
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ action, version: 'v1' })
    for (const queryKey of [['pm', 'leaves'], ['pm', 'map'], ['pm', 'queue']]) expect(invalidate).toHaveBeenCalledWith({ queryKey })
  })
  it('saves title and description in app and keeps the saved issue open for the next decision', async () => {
    fetchMock.mockResolvedValueOnce(reply(detail)).mockResolvedValueOnce(reply({ ok: true, item: { ...detail, title: 'Clear requirements', body: 'Acceptance criteria', version: 'v2' } }))
    const { onClose } = mount()
    await screen.findByText('Account warning')
    click('Edit details')
    fireEvent.change(screen.getByLabelText(/Title/), { target: { value: 'Clear requirements' } })
    fireEvent.change(screen.getByLabelText('Description'), { target: { value: 'Acceptance criteria' } })
    click('Save changes')
    await screen.findByRole('button', { name: 'Mark resolved' })
    expect(screen.getByText('Acceptance criteria')).toBeInTheDocument()
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Mark resolved' })).toBeInTheDocument()
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ action: 'edit', version: 'v1', title: 'Clear requirements', body: 'Acceptance criteria' })
  })
  it('keeps an edit and error visible after a failed save', async () => {
    fetchMock.mockResolvedValueOnce(reply(detail)).mockResolvedValueOnce(reply({ detail: 'GitHub unavailable' }, 502))
    const { onClose } = mount()
    await screen.findByText('Account warning')
    click('Edit details')
    fireEvent.change(screen.getByLabelText('Description'), { target: { value: 'New requirements' } })
    click('Save changes')
    expect(await screen.findByRole('alert')).toHaveTextContent('GitHub unavailable')
    expect(screen.getByLabelText('Description')).toHaveValue('New requirements')
    fireEvent.click(screen.getByTestId('dialog-backdrop'))
    expect(onClose).not.toHaveBeenCalled()
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ action: 'edit', version: 'v1', title: detail.title, body: 'New requirements' })
  })
  it('reloads after a conflict only when explicitly requested', async () => {
    fetchMock.mockResolvedValueOnce(reply(detail)).mockResolvedValueOnce(reply({ detail: 'Issue changed' }, 409)).mockResolvedValueOnce(reply({ ...detail, title: 'Changed upstream', version: 'v2' }))
    mount()
    await screen.findByText('Account warning')
    click('Mark resolved'); click('Confirm resolved')
    await screen.findByRole('alert')
    click('Reload issue')
    expect(await screen.findByText('Changed upstream')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).toBeNull()
  })
  it('prevents dismissing or double submitting while a write is pending', async () => {
    fetchMock.mockResolvedValueOnce(reply(detail)).mockImplementationOnce(() => new Promise(() => {}))
    const { onClose } = mount()
    await screen.findByText('Account warning')
    click('Mark resolved'); click('Confirm resolved')
    expect(screen.getByRole('button', { name: 'Confirm resolved' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Close' })).toBeDisabled()
    fireEvent.click(screen.getByTestId('dialog-backdrop'))
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).not.toHaveBeenCalled()
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
  })
  it('does not offer decisions for closed issues', async () => {
    fetchMock.mockResolvedValue(reply({ ...detail, state: 'closed' }))
    mount()
    await screen.findByText('Account warning')
    expect(screen.queryByRole('button', { name: 'Mark resolved' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Edit details' })).toBeNull()
  })

  const held = { ...detail, holdKind: 'owner_action', holdReason: 'needs the keyring', holdBy: 'tony@example.com', holdSetAt: '2026-09-18T00:00:00+00:00',
    guidance: { retry: { available: true, unavailableReason: null, immediate: [{ text: 'Releases the hold', spawnsSession: false, writesGithub: false }], then: [], when: 'never', reversal: null, gate: null },
                implement: { available: true, unavailableReason: null, immediate: [{ text: 'Dispatches a coding session', spawnsSession: true, writesGithub: false }], then: [], when: 'now', reversal: null, gate: null } } }

  it('shows the hold panel with consequences and releases the hold on Retry without closing', async () => {
    fetchMock.mockResolvedValueOnce(reply(held))
      .mockResolvedValueOnce(reply({ ok: true, released: 'owner_action', item: {} }))
      .mockResolvedValueOnce(reply({ ...detail, holdKind: null, guidance: null }))
    const { onClose } = mount()
    expect(await screen.findByRole('region', { name: 'Hold' })).toHaveTextContent('Held for the owner')
    expect(screen.getByText('needs the keyring')).toBeInTheDocument()
    expect(screen.getByText('Releases the hold')).toBeInTheDocument()
    click('Retry')
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3))
    expect(fetchMock.mock.calls[1][0]).toMatch(/\/api\/pm\/work-items\/w1\/hold\/release$/)
    expect(fetchMock.mock.calls[1][1].method).toBe('POST')
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Hold' })).toBeNull())
    expect(onClose).not.toHaveBeenCalled()
  })

  it('dispatches a session on Implement now, links to it, and stops asserting the released hold', async () => {
    fetchMock.mockResolvedValueOnce(reply(held))
      .mockResolvedValueOnce(reply({ status: 'spawned', session_id: 's-9', session_name: 'Fix keyring' }))
      // the dispatch released the hold server-side, so the invalidated issue query
      // refetches and comes back unheld
      .mockResolvedValueOnce(reply({ ...detail, holdKind: null, guidance: null }))
    mount()
    await screen.findByRole('region', { name: 'Hold' })
    click('Implement now')
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3))
    expect(fetchMock.mock.calls[1][0]).toMatch(/\/api\/automations\/backlog-runner\/dispatch$/)
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ refs: ['o/r#1'] })
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Hold' })).toBeNull())
    expect(await screen.findByRole('link', { name: 'Open session' })).toHaveAttribute('href', expect.stringMatching(/\/s-9$/))
  })

  it('shows a refusal inline and renders no buttons for an evidence hold', async () => {
    fetchMock.mockResolvedValueOnce(reply(held))
      .mockResolvedValueOnce(reply({ status: 'no_account', session_id: null, session_name: null }))
      .mockResolvedValueOnce(reply({ ...detail, holdKind: null, guidance: null }))
    mount()
    await screen.findByRole('region', { name: 'Hold' })
    click('Implement now')
    expect(await screen.findByText(/Dispatch failed: no_account/)).toBeInTheDocument()
    cleanup()
    fetchMock.mockReset()
    fetchMock.mockResolvedValueOnce(reply({ ...held, holdKind: 'evidence', guidance: null }))
    mount()
    expect(await screen.findByRole('region', { name: 'Hold' })).toHaveTextContent('Held for evidence')
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Implement now' })).toBeNull()
  })

  it('renders the machine consequence copy for an evidence hold through ConsequenceList, with no buttons', async () => {
    const evidenceGuidance = { evidence: {
      available: true, unavailableReason: null, immediate: [],
      then: [{ text: 'The next run investigates this report instead of implementing it: it closes '
        + 'noise, consolidates duplicates, converts it into a fix, or hands it to you with what is '
        + 'needed. No pull request.', spawnsSession: false, writesGithub: false }],
      when: 'next_tick', reversal: null, gate: null } }
    fetchMock.mockResolvedValueOnce(reply({
      ...detail, holdKind: 'evidence', holdReason: 'auto-filed error report: investigate before implementing',
      holdBy: 'automation:error-triage', holdSetAt: '2026-09-18T00:00:00+00:00', guidance: evidenceGuidance }))
    mount()
    expect(await screen.findByRole('region', { name: 'Hold' })).toHaveTextContent('Held for evidence')
    expect(screen.getByText(/investigates this report instead of implementing it/)).toBeInTheDocument()
    expect(screen.getByText(/No pull request\./)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Implement now' })).toBeNull()
  })

  // Spec 2026-09-18 §7: "Decision: links to the proposal card." The options,
  // the recommendation and the two consequence columns live on that card, so
  // this panel never grows a second copy of the choice — and no Retry, because
  // accepting the proposal is the only thing that clears a decision hold.
  it('points a decision hold at the inbox, and offers no buttons of its own', async () => {
    fetchMock.mockResolvedValueOnce(reply({
      ...detail, holdKind: 'decision',
      holdReason: 'durable queue or in-process stream?',
      holdBy: 'session:s1', holdSetAt: '2026-09-18T00:00:00+00:00', guidance: null }))
    mount()
    const region = await screen.findByRole('region', { name: 'Hold' })
    expect(region).toHaveTextContent('Awaiting your decision')
    expect(region).toHaveTextContent('durable queue or in-process stream?')
    expect(screen.getByRole('link', { name: /Open the inbox/ }))
      .toHaveAttribute('href', '/roadmap?scope=inbox')
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Implement now' })).toBeNull()
  })

  it('clears the last dispatch outcome when an edit starts', async () => {
    fetchMock.mockResolvedValueOnce(reply(held))
      .mockResolvedValueOnce(reply({ status: 'no_account', session_id: null, session_name: null }))
      .mockResolvedValueOnce(reply({ ...detail, holdKind: null, guidance: null }))
    mount()
    await screen.findByRole('region', { name: 'Hold' })
    click('Implement now')
    expect(await screen.findByText(/Dispatch failed: no_account/)).toBeInTheDocument()
    click('Edit details')
    expect(screen.queryByText(/Dispatch failed: no_account/)).toBeNull()
  })
})
