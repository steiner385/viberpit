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
    expect(screen.getByText('tony@example.com handed this to you: needs the keyring. The runner will not touch it again until you act.')).toBeInTheDocument()
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

  const evidenceGuidance = { evidence: {
    available: true, unavailableReason: null, immediate: [],
    then: [{ text: 'The next run investigates this report instead of implementing it: it closes '
      + 'noise, consolidates duplicates, converts it into a fix, or hands it to you with what is '
      + 'needed. No pull request.', spawnsSession: false, writesGithub: false }],
    when: 'next_tick', reversal: null, gate: null },
    investigate: {
      available: true, unavailableReason: null,
      immediate: [{ text: 'Dispatches a coding session that investigates this report now. '
        + 'No pull request.', spawnsSession: true, writesGithub: false }],
      then: [], when: 'now', reversal: 'Archive the session; the item stays held pending investigation.',
      gate: null } }

  it('renders the machine consequence copy for an evidence hold through ConsequenceList, with no Retry or Implement now', async () => {
    fetchMock.mockResolvedValueOnce(reply({
      ...detail, holdKind: 'evidence', holdReason: 'auto-filed error report: investigate before implementing',
      holdBy: 'automation:error-triage', holdSetAt: '2026-09-18T00:00:00+00:00', guidance: evidenceGuidance }))
    mount()
    expect(await screen.findByRole('region', { name: 'Hold' })).toHaveTextContent('Held for evidence')
    expect(screen.getByText(/investigates this report instead of implementing it/)).toBeInTheDocument()
    expect(screen.getAllByText(/No pull request\./).length).toBeGreaterThan(0)
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Implement now' })).toBeNull()
  })

  it('#1963: dispatches a session on Investigate now for an evidence hold', async () => {
    fetchMock.mockResolvedValueOnce(reply({
      ...detail, holdKind: 'evidence', holdReason: 'auto-filed error report: investigate before implementing',
      holdBy: 'automation:error-triage', holdSetAt: '2026-09-18T00:00:00+00:00', guidance: evidenceGuidance }))
      .mockResolvedValueOnce(reply({ status: 'spawned', session_id: 's9', session_name: 'sess-9' }))
    mount()
    await screen.findByRole('region', { name: 'Hold' })
    click('Investigate now')
    expect(await screen.findByRole('link', { name: 'Open session' })).toHaveAttribute('href', '/sessions/s9')
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ refs: ['o/r#1'] })
  })

  it('disables Investigate now when the guidance says it is unavailable', async () => {
    fetchMock.mockResolvedValueOnce(reply({
      ...detail, holdKind: 'evidence', holdReason: 'auto-filed error report: investigate before implementing',
      holdBy: 'automation:error-triage', holdSetAt: '2026-09-18T00:00:00+00:00',
      guidance: { ...evidenceGuidance, investigate: {
        ...evidenceGuidance.investigate, available: false,
        unavailableReason: "This product's dispatch is off; turn it on in the product's automation policy first." } } }))
    mount()
    await screen.findByRole('region', { name: 'Hold' })
    expect(await screen.findByRole('button', { name: 'Investigate now' })).toBeDisabled()
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
    fetchMock.mockResolvedValueOnce(reply([]))
    mount()
    const region = await screen.findByRole('region', { name: 'Hold' })
    expect(region).toHaveTextContent('Awaiting your decision')
    expect(region).toHaveTextContent('durable queue or in-process stream?')
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
    expect(screen.getByRole('link', { name: /Open the inbox/ }))
      .toHaveAttribute('href', '/roadmap?scope=inbox')
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Implement now' })).toBeNull()
  })

  it('deep-links a decision hold to its pending proposal', async () => {
    fetchMock.mockResolvedValueOnce(reply({
      ...detail, holdKind: 'decision', holdReason: 'which queue?',
      holdBy: 'session:s1', holdSetAt: '2026-09-18T00:00:00+00:00', guidance: null }))
    fetchMock.mockResolvedValueOnce(reply([
      { id: 'dp-old', workItemId: 'w1', status: 'accepted', proposalKind: 'decision' },
      { id: 'dp-other', workItemId: 'w2', status: 'pending', proposalKind: 'decision' },
      { id: 'dp-9', workItemId: 'w1', status: 'pending', proposalKind: 'decision' },
    ]))
    mount()
    const link = await screen.findByRole('link', { name: /Open the inbox/ })
    await waitFor(() => expect(link).toHaveAttribute('href', '/roadmap?scope=inbox&proposal=dp-9'))
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

describe('IssueDispositionDialog — approval-gated hand-off', () => {
  const escalated = { ...detail, holdKind: 'owner_action', holdReason: 'bounced at opus; fable needs approval', holdBy: 'runner',
    holdSetAt: '2026-09-20T12:29:56+00:00', escalation: { bouncedAt: 'opus', needsApproval: 'fable' },
    guidance: { retry: { available: true, unavailableReason: null, immediate: [{ text: 'Releases the hold', spawnsSession: false, writesGithub: false }], then: [], when: 'never', reversal: null, gate: null },
                implement: { available: true, unavailableReason: null, immediate: [{ text: 'Dispatches a coding session', spawnsSession: true, writesGithub: false }], then: [], when: 'now', reversal: null, gate: null } } }

  it('explains the escalation in plain words and approves it at the gated tier, linking the session', async () => {
    fetchMock.mockResolvedValueOnce(reply(escalated))
      .mockResolvedValueOnce(reply({ status: 'spawned', session_id: 's-28', session_name: 'backlog #28', approval: 'approved', model: 'fable', item: {} }))
      .mockResolvedValueOnce(reply({ ...detail, holdKind: null, guidance: null, escalation: null }))
    const { onClose } = mount()
    const region = await screen.findByRole('region', { name: 'Hold' })
    expect(region).toHaveTextContent(/The Backlog Runner could not get a pull request merged at Opus and stopped: the next step up is Fable/)
    expect(region).toHaveTextContent(/Approve escalation to run it once at Fable now, or Retry/)
    expect(region).not.toHaveTextContent('bounced at opus; fable needs approval')
    click('Approve escalation to Fable')
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3))
    expect(fetchMock.mock.calls[1][0]).toMatch(/\/api\/pm\/work-items\/w1\/hold\/approve-escalation$/)
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ model: 'fable' })
    expect(await screen.findByRole('link', { name: 'Open session' })).toHaveAttribute('href', expect.stringContaining('s-28'))
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Hold' })).toBeNull())
    expect(onClose).not.toHaveBeenCalled()
  })

  it('offers no approve button on a plain owner hold', async () => {
    fetchMock.mockResolvedValueOnce(reply({ ...escalated, holdReason: 'needs the keyring', holdBy: 'tony@example.com', escalation: null }))
    mount()
    await screen.findByRole('region', { name: 'Hold' })
    expect(screen.queryByRole('button', { name: /Approve escalation/ })).toBeNull()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
  })
})
