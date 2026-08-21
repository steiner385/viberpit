import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { HostBadge } from '../HostBadge'

describe('HostBadge', () => {
  it('renders the host name', () => {
    render(<HostBadge host="rocky" healthy />)
    expect(screen.getByText('rocky')).toBeInTheDocument()
  })

  it('renders nothing when the session has no host', () => {
    // Pre-fleet sessions carry host=null; an "unknown" chip on every historical
    // card would be noise rather than information.
    const { container } = render(<HostBadge host={null} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('marks an unhealthy host for the screen reader, not just by colour', () => {
    render(<HostBadge host="rocky" healthy={false} />)
    expect(screen.getByLabelText(/rocky.*unhealthy/i)).toBeInTheDocument()
  })

  it('does not claim a host is unhealthy when health is merely unknown', () => {
    // healthy=undefined/null means "not yet probed". Rendering that as a warning
    // would cry wolf on every freshly-registered host.
    render(<HostBadge host="rocky" healthy={null} />)
    expect(screen.queryByLabelText(/unhealthy/i)).not.toBeInTheDocument()
    expect(screen.getByText('rocky')).toBeInTheDocument()
  })
})
