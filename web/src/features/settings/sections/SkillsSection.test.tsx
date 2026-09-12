import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('@/api/client', () => ({
  ApiRequestError: class extends Error {},
  api: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn().mockResolvedValue({}),
    patch: vi.fn(),
    del: vi.fn().mockResolvedValue({}),
  },
}))

import { SkillsSection } from './SkillsSection'
import { api } from '@/api/client'

function renderSection() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <SkillsSection />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const skillsResponse = {
  skills: [
    { name: 'my-skill', description: 'My user skill', source: 'user', readonly: false },
    { name: 'plugin-skill', description: 'Plugin provided', source: 'alpha@m', readonly: true },
  ],
}

beforeEach(() => {
  vi.mocked(api.get).mockImplementation((path: string) => {
    if (path === '/library/skills') return Promise.resolve(skillsResponse)
    if (path === '/library/skills/my-skill') {
      return Promise.resolve({ name: 'my-skill', content: '---\nname: my-skill\n---\n\nContent here', readonly: false })
    }
    if (path === '/library/skills/plugin-skill?plugin=alpha%40m') {
      return Promise.resolve({ name: 'plugin-skill', content: '---\nname: plugin-skill\n---\n', readonly: true })
    }
    return Promise.resolve({})
  })
})

describe('SkillsSection', () => {
  // (a) user + plugin rows render in their groups
  it('renders user skills under "Your skills" and plugin skills under "From plugins"', async () => {
    renderSection()
    expect(await screen.findByText('Your skills')).toBeTruthy()
    expect(await screen.findByText('my-skill')).toBeTruthy()
    expect(await screen.findByText('From plugins')).toBeTruthy()
    expect(await screen.findByText('plugin-skill')).toBeTruthy()
    // plugin badge
    expect(await screen.findByText('alpha@m')).toBeTruthy()
  })

  // (b) New skill → type name + content → Save calls api.put
  it('new skill: type name + content then Save calls api.put', async () => {
    renderSection()
    const newBtn = await screen.findByRole('button', { name: /new skill/i })
    await userEvent.click(newBtn)

    // Dialog opens
    expect(await screen.findByRole('dialog')).toBeTruthy()

    // Type name
    const nameInput = screen.getByPlaceholderText('my-skill')
    await userEvent.clear(nameInput)
    await userEvent.type(nameInput, 'new-skill')

    // Edit the content textarea
    const contentArea = screen.getByRole('textbox', { name: /content/i })
    await userEvent.clear(contentArea)
    await userEvent.type(contentArea, '---\nname: new-skill\n---\n\nHello')

    // Click Save
    const saveBtn = screen.getByRole('button', { name: /^save$/i })
    await userEvent.click(saveBtn)

    await waitFor(() => {
      expect(api.put).toHaveBeenCalledWith(
        '/library/skills/new-skill',
        expect.objectContaining({ content: expect.any(String) }),
      )
    })
  })

  // (c) Opening existing user skill loads content + Delete calls api.del
  it('opening existing user skill loads content and Delete calls api.del', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderSection()

    const skillRow = await screen.findByRole('button', { name: /my-skill/i })
    await userEvent.click(skillRow)

    // Content loaded from api.get
    await waitFor(() => {
      expect(api.get).toHaveBeenCalledWith('/library/skills/my-skill')
    })

    // Dialog open, Delete button present
    const deleteBtn = await screen.findByRole('button', { name: /^delete$/i })
    await userEvent.click(deleteBtn)

    await waitFor(() => {
      expect(api.del).toHaveBeenCalledWith('/library/skills/my-skill')
    })
  })

  // (d) Plugin skill opens readonly — no Save button
  it('plugin skill opens read-only (no Save button)', async () => {
    renderSection()

    const pluginRow = await screen.findByRole('button', { name: /plugin-skill/i })
    await userEvent.click(pluginRow)

    // Dialog opens
    expect(await screen.findByRole('dialog')).toBeTruthy()

    // No Save button
    expect(screen.queryByRole('button', { name: /^save$/i })).toBeNull()
    // No Delete button
    expect(screen.queryByRole('button', { name: /^delete$/i })).toBeNull()
    // Close / Cancel present
    expect(screen.getByRole('button', { name: /cancel/i })).toBeTruthy()
  })

  // (e) Textarea disabled while content is loading
  it('textarea is disabled while content is loading', async () => {
    // Replace the content GET with a never-resolving promise
    vi.mocked(api.get).mockImplementation((path: string) => {
      if (path === '/library/skills') return Promise.resolve(skillsResponse)
      if (path === '/library/skills/my-skill') return new Promise(() => {}) // never resolves
      return Promise.resolve({})
    })

    renderSection()

    const skillRow = await screen.findByRole('button', { name: /my-skill/i })
    await userEvent.click(skillRow)

    // Dialog opens
    expect(await screen.findByRole('dialog')).toBeTruthy()

    // Textarea should be disabled while loading
    const textarea = screen.getByRole('textbox', { name: /content/i })
    expect(textarea).toHaveAttribute('disabled')
  })

  // (f) Save error is shown when mutation fails
  it('shows error message when save mutation fails', async () => {
    vi.mocked(api.put).mockRejectedValue(new Error('name too long'))

    renderSection()
    const newBtn = await screen.findByRole('button', { name: /new skill/i })
    await userEvent.click(newBtn)

    const nameInput = screen.getByPlaceholderText('my-skill')
    await userEvent.clear(nameInput)
    await userEvent.type(nameInput, 'valid-skill')

    const saveBtn = screen.getByRole('button', { name: /^save$/i })
    await userEvent.click(saveBtn)

    await waitFor(() => {
      expect(screen.getByRole('alert')).toBeTruthy()
      expect(screen.getByText('name too long')).toBeTruthy()
    })
  })

  // (g) Skill name validation: 65-char name is invalid
  it('rejects a 65-character skill name', async () => {
    renderSection()
    const newBtn = await screen.findByRole('button', { name: /new skill/i })
    await userEvent.click(newBtn)

    const nameInput = screen.getByPlaceholderText('my-skill')
    await userEvent.clear(nameInput)
    await userEvent.type(nameInput, 'a'.repeat(65))

    // Save button should be disabled (name too long)
    const saveBtn = screen.getByRole('button', { name: /^save$/i })
    expect(saveBtn).toBeDisabled()
  })

  // (h) Skill name validation: valid 64-char name is accepted
  it('accepts a 64-character skill name', async () => {
    renderSection()
    const newBtn = await screen.findByRole('button', { name: /new skill/i })
    await userEvent.click(newBtn)

    const nameInput = screen.getByPlaceholderText('my-skill')
    await userEvent.clear(nameInput)
    // 1 starting char + 63 chars = 64 total, which matches /^[a-z0-9][a-z0-9-]{0,63}$/
    await userEvent.type(nameInput, 'a' + 'b'.repeat(63))

    const saveBtn = screen.getByRole('button', { name: /^save$/i })
    expect(saveBtn).not.toBeDisabled()
  })
})
