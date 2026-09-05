import { describe, it, expect, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { axe, toHaveNoViolations } from 'jest-axe'
import { useState } from 'react'
import type { Session } from '@/api/types'
import {
  NO_FILTERS, deriveFilterOptions,
  type FilterOptions, type ListFilters,
} from '@/session/listFilters'
import { SessionListControls } from '../SessionListControls'

expect.extend(toHaveNoViolations)

describe('SessionListControls', () => {
  it('renders current selection and emits changes', async () => {
    const onGroupByChange = vi.fn()
    const onSortByChange = vi.fn()
    render(
      <SessionListControls
        groupBy="none"
        sortBy="status"
        onGroupByChange={onGroupByChange}
        onSortByChange={onSortByChange}
        searchQuery=""
        onSearchChange={() => {}}
      />,
    )
    await userEvent.selectOptions(screen.getByLabelText('Group by'), 'repo')
    expect(onGroupByChange).toHaveBeenCalledWith('repo')
    await userEvent.selectOptions(screen.getByLabelText('Sort by'), 'lastActive')
    expect(onSortByChange).toHaveBeenCalledWith('lastActive')
  })
})

function Harness() {
  const [q, setQ] = useState('')
  return (
    <SessionListControls
      groupBy="none" sortBy="status"
      onGroupByChange={() => {}} onSortByChange={() => {}}
      searchQuery={q} onSearchChange={setQ}
    />
  )
}

describe('SessionListControls injected options', () => {
  it('renders injected archived options', () => {
    render(
      <SessionListControls
        groupBy="account"
        sortBy="archived"
        onGroupByChange={() => {}}
        onSortByChange={() => {}}
        searchQuery=""
        onSearchChange={() => {}}
        groupOptions={[{ value: 'account', label: 'Account' }, { value: 'repo', label: 'Repo' }]}
        sortOptions={[{ value: 'archived', label: 'Archived date' }]}
      />,
    )
    expect(screen.getByRole('option', { name: 'Account' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Archived date' })).toBeInTheDocument()
  })
})

describe('SessionListControls search', () => {
  it('types into the search box and clears it', async () => {
    render(<Harness />)
    const box = screen.getByLabelText('Search sessions')
    await userEvent.type(box, 'auth')
    expect(box).toHaveValue('auth')
    await userEvent.click(screen.getByLabelText('Clear search'))
    expect(box).toHaveValue('')
  })

  it('hides the clear button when empty', () => {
    render(
      <SessionListControls
        groupBy="none" sortBy="status"
        onGroupByChange={() => {}} onSortByChange={() => {}}
        searchQuery="" onSearchChange={() => {}}
      />,
    )
    expect(screen.queryByLabelText('Clear search')).toBeNull()
  })
})

// ── the Status / Project / Environment filters (DEC-8) ───────────────────────
//
// Scope note on axe: jsdom applies no stylesheet, so axe below covers roles,
// accessible names and ARIA wiring ONLY. Its `color-contrast` rule always
// returns `incomplete` and `toHaveNoViolations()` inspects `violations` alone —
// a green assertion here is NOT evidence of adequate contrast. Contrast is
// checked separately by src/__tests__/contrast.test.ts, which does the WCAG
// maths on the tokens in index.css. (A previous PR shipped a contrast
// regression straight past seven green axe assertions.)

// ── typed factories (R10: real values, never `as unknown as T`) ──────────────

function session(over: Partial<Session> = {}): Session {
  return {
    id: 'sess-1', name: 'First', accountName: 'alice', engine: 'claude_code',
    engineSessionId: null, status: 'idle',
    createdAt: '2026-06-19T10:00:00Z', prs: [],
    ...over,
  }
}

/** Built by the REAL derivation, so the option domains under test are the ones
 *  `useSessionListState` hands the control — not a hand-written stand-in that
 *  could agree with a broken component. */
const OPTIONS: FilterOptions = deriveFilterOptions(
  [
    session({ id: 's1', repo: 'o/a', host: 'dobby' }),
    session({ id: 's2', repo: 'o/b', host: 'rocky' }),
  ],
  ['dobby', 'rocky'],
)
const EMPTY_OPTIONS: FilterOptions = deriveFilterOptions([], [])

function FilterHarness({
  initial = NO_FILTERS,
  options = OPTIONS,
  onChange,
}: {
  initial?: ListFilters
  options?: FilterOptions
  onChange?: (next: ListFilters) => void
}) {
  const [value, setValue] = useState(initial)
  return (
    <SessionListControls
      groupBy="none" sortBy="status"
      onGroupByChange={() => {}} onSortByChange={() => {}}
      searchQuery="" onSearchChange={() => {}}
      filters={{
        value,
        options,
        onChange: (next) => { onChange?.(next); setValue(next) },
      }}
    />
  )
}

describe('SessionListControls — DEC-7 relabel', () => {
  it('the Group-by option reads "Project" but its value is still `repo`', () => {
    render(
      <SessionListControls
        groupBy="none" sortBy="status"
        onGroupByChange={() => {}} onSortByChange={() => {}}
        searchQuery="" onSearchChange={() => {}}
      />,
    )
    const groupBy = screen.getByLabelText('Group by')
    const project = within(groupBy).getByRole('option', { name: 'Project' }) as HTMLOptionElement
    // `LIVE_SPEC.groupByValues` is persisted: a `project` value here would strand
    // every saved preference. The relabel is a LABEL change and nothing else.
    expect(project.value).toBe('repo')
    expect(within(groupBy).queryByRole('option', { name: 'Repo' })).toBeNull()
    const values = within(groupBy).getAllByRole('option').map((o) => (o as HTMLOptionElement).value)
    expect(values).toEqual(['none', 'repo', 'status'])
  })

  it('renders no filter selects at all when `filters` is omitted (ArchivedManager)', () => {
    render(
      <SessionListControls
        groupBy="none" sortBy="status"
        onGroupByChange={() => {}} onSortByChange={() => {}}
        searchQuery="" onSearchChange={() => {}}
      />,
    )
    expect(screen.queryByLabelText('Status')).toBeNull()
    expect(screen.queryByLabelText('Project')).toBeNull()
    expect(screen.queryByLabelText('Environment')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Clear filters' })).toBeNull()
  })
})

describe('SessionListControls — filter selects', () => {
  it('each select narrows, and the three compose rather than overwrite', async () => {
    const onChange = vi.fn<(next: ListFilters) => void>()
    render(<FilterHarness onChange={onChange} />)

    await userEvent.selectOptions(screen.getByLabelText('Status'), 'busy')
    expect(onChange).toHaveBeenLastCalledWith({ status: 'busy', project: null, environment: null })

    await userEvent.selectOptions(screen.getByLabelText('Project'), 'o/b')
    expect(onChange).toHaveBeenLastCalledWith({ status: 'busy', project: 'o/b', environment: null })

    await userEvent.selectOptions(screen.getByLabelText('Environment'), 'rocky')
    expect(onChange).toHaveBeenLastCalledWith({ status: 'busy', project: 'o/b', environment: 'rocky' })
    // …and the controls show what was chosen, not just what was emitted.
    expect(screen.getByLabelText('Status')).toHaveValue('busy')
    expect(screen.getByLabelText('Project')).toHaveValue('o/b')
    expect(screen.getByLabelText('Environment')).toHaveValue('rocky')
  })

  it('the first option clears that filter back to null — and only that one', async () => {
    const onChange = vi.fn<(next: ListFilters) => void>()
    render(
      <FilterHarness
        initial={{ status: 'busy', project: 'o/a', environment: 'dobby' }}
        onChange={onChange}
      />,
    )
    await userEvent.selectOptions(screen.getByLabelText('Project'), 'All projects')
    // null, NOT '' — the empty string is the DOM sentinel for "no option", and
    // leaking it into ListFilters would make `isFiltered` report a live filter
    // that matches every session.
    expect(onChange).toHaveBeenLastCalledWith({ status: 'busy', project: null, environment: 'dobby' })
    expect(screen.getByLabelText('Project')).toHaveValue('')
    expect(screen.getByLabelText('Status')).toHaveValue('busy')
  })

  it('offers the status tiers in TIER_RANK order with their tier labels', () => {
    render(<FilterHarness />)
    const labels = within(screen.getByLabelText('Status'))
      .getAllByRole('option')
      .map((o) => o.textContent)
    expect(labels).toEqual([
      'All statuses', 'Needs you', 'Unreachable', 'Failed', 'Done', 'Waiting', 'Working',
    ])
  })

  it('renders a select with no options, disabled, so the strip does not reflow', () => {
    // No fleet host registered and no session with a repo: both domains empty.
    // The control still exists — a row that grows a third select the moment a
    // repo appears jumps under the pointer.
    render(<FilterHarness options={EMPTY_OPTIONS} />)
    expect(screen.getByLabelText('Environment')).toBeDisabled()
    expect(screen.getByLabelText('Project')).toBeDisabled()
    // Status is a CLOSED vocabulary and is never empty, so it is never disabled.
    expect(screen.getByLabelText('Status')).toBeEnabled()
  })
})

describe('SessionListControls — a selection its options do not offer', () => {
  it('shows an ACTIVE filter whose domain has not loaded, and can clear it', async () => {
    // The real case: `/fleet/hosts` is still in flight, so
    // `filterOptions.environment` is [] while a stored `environment` filter is
    // already narrowing the list (the hook deliberately declines to sanitize
    // before the domains are known). Built naively from `options`, the select
    // would fall back to "All environments" — the UI would say "no filter"
    // while the list is filtered, and the control could not undo it.
    const onChange = vi.fn<(next: ListFilters) => void>()
    render(
      <FilterHarness
        initial={{ status: null, project: null, environment: 'rocky' }}
        options={EMPTY_OPTIONS}
        onChange={onChange}
      />,
    )
    const env = screen.getByLabelText('Environment')
    expect(env).toHaveValue('rocky')
    expect(within(env).getByRole('option', { name: 'rocky' })).toBeTruthy()
    // Enabled, or the applied filter would be unclearable from the control.
    expect(env).toBeEnabled()

    await userEvent.selectOptions(env, 'All environments')
    expect(onChange).toHaveBeenLastCalledWith({ status: null, project: null, environment: null })
  })

  it('does not duplicate an option the domain already offers', () => {
    render(<FilterHarness initial={{ status: null, project: null, environment: 'rocky' }} />)
    const env = screen.getByLabelText('Environment')
    expect(within(env).getAllByRole('option', { name: 'rocky' })).toHaveLength(1)
    expect(within(env).getAllByRole('option').map((o) => o.textContent))
      .toEqual(['All environments', 'dobby', 'rocky'])
  })
})

describe('SessionListControls — clear filters', () => {
  it('appears only while something is filtered and resets all three', async () => {
    const onChange = vi.fn<(next: ListFilters) => void>()
    const { rerender } = render(<FilterHarness onChange={onChange} />)
    expect(screen.queryByRole('button', { name: 'Clear filters' })).toBeNull()

    rerender(
      <FilterHarness
        key="filtered"
        initial={{ status: 'busy', project: 'o/a', environment: 'dobby' }}
        onChange={onChange}
      />,
    )
    await userEvent.click(screen.getByRole('button', { name: 'Clear filters' }))
    expect(onChange).toHaveBeenLastCalledWith(NO_FILTERS)
    expect(screen.queryByRole('button', { name: 'Clear filters' })).toBeNull()
    expect(screen.getByLabelText('Status')).toHaveValue('')
    expect(screen.getByLabelText('Project')).toHaveValue('')
    expect(screen.getByLabelText('Environment')).toHaveValue('')
  })
})

describe('SessionListControls — accessibility', () => {
  it('has no axe violations with the filters mounted (roles and names only)', async () => {
    const { container } = render(
      <FilterHarness initial={{ status: 'busy', project: null, environment: 'rocky' }} />,
    )
    expect(await axe(container)).toHaveNoViolations()
  })
})
