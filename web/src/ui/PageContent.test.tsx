import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { PageContent } from './PageContent'

describe('PageContent keyboard scrolling', () => {
  it('puts a scrolling page body in the tab order so read-only content can be scrolled', async () => {
    render(<PageContent><p>long copy</p></PageContent>)
    await userEvent.tab()
    expect(screen.getByTestId('page-content')).toHaveFocus()
  })

  it('leaves a clipping workspace body out of the tab order', () => {
    render(<PageContent scroll={false}><p>panes scroll themselves</p></PageContent>)
    expect(screen.getByTestId('page-content')).not.toHaveAttribute('tabindex')
  })
})
