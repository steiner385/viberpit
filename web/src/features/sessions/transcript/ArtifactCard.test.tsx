import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ArtifactCard } from './ArtifactCard'
import type { Block } from '@/session/reducer'

type ToolBlock = Extract<Block, { kind: 'tool' }>

function envelope(data: Record<string, unknown>): Record<string, unknown> {
  return { content: [{ type: 'text', text: JSON.stringify({ source: 'viberpit', now: 't', data }) }] }
}

function share(input: Record<string, unknown>, result?: ToolBlock['result']): ToolBlock {
  return {
    kind: 'tool', id: 'b1', toolId: 'b1', name: 'mcp__viberpit__share_artifact',
    toolKind: 'app_action', input, result,
  }
}

describe('ArtifactCard', () => {
  it('renders a url artifact as its label plus a clickable link that opens in a new tab', () => {
    render(<ArtifactCard block={share({ kind: 'url', value: 'https://g.h/o/n/pull/7', label: 'PR #7', note: 'auto-merge armed' })} />)
    expect(screen.getByText('PR #7')).toBeInTheDocument()
    expect(screen.getByText('auto-merge armed')).toBeInTheDocument()
    const link = screen.getByRole('link', { name: 'https://g.h/o/n/pull/7' })
    expect(link).toHaveAttribute('href', 'https://g.h/o/n/pull/7')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', expect.stringContaining('noopener'))
  })

  it('renders a path artifact as a copyable chip carrying the full path', () => {
    render(<ArtifactCard block={share({ kind: 'path', value: '/tmp/w/report.md', label: 'Audit report' })} />)
    expect(screen.getByText('Audit report')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '/tmp/w/report.md' })).toBeInTheDocument()
    expect(screen.queryByRole('link')).toBeNull()
  })

  it('renders an ingested image inline from the served attachment url', () => {
    const block = share(
      { kind: 'image', value: '/tmp/shot.png', label: 'Settings page' },
      { ok: true, payload: envelope({ kind: 'image', value: '/tmp/shot.png', label: 'Settings page', url: '/api/attachments/abc' }) },
    )
    render(<ArtifactCard block={block} />)
    const img = screen.getByRole('img', { name: 'Settings page' })
    expect(img).toHaveAttribute('src', '/api/attachments/abc')
    expect(screen.getByRole('link', { name: /open/i })).toHaveAttribute('href', '/api/attachments/abc')
  })

  it('shows the image as still uploading until the handler result lands', () => {
    render(<ArtifactCard block={share({ kind: 'image', value: '/tmp/shot.png', label: 'Settings page' })} />)
    expect(screen.queryByRole('img')).toBeNull()
    expect(screen.getByText(/uploading/i)).toBeInTheDocument()
  })

  it('shows the handler refusal so a broken share is visible, not silent', () => {
    const block = share(
      { kind: 'image', value: '/tmp/nope.png', label: 'Missing' },
      { ok: false, payload: { content: [{ type: 'text', text: 'no such file on this host: /tmp/nope.png' }] } },
    )
    render(<ArtifactCard block={block} />)
    expect(screen.getByText('no such file on this host: /tmp/nope.png')).toBeInTheDocument()
    expect(screen.queryByRole('img')).toBeNull()
  })
})
