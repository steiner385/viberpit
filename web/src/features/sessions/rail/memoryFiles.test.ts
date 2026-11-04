import { describe, it, expect } from 'vitest'
import { memoryDisplayPath, memoryTypeLabel } from './memoryFiles'

describe('memoryTypeLabel', () => {
  it('maps the CLI types to their labels and shows an unknown type as-is', () => {
    expect(['User', 'Project', 'Local', 'Rule', 'Import', 'Managed', 'AutoMem'].map(memoryTypeLabel))
      .toEqual(['User', 'Project', 'Local', 'Rule', 'Import', 'Managed', 'AutoMem'])
    expect(memoryTypeLabel('Plugin')).toBe('Plugin')
  })
})

describe('memoryDisplayPath', () => {
  it('is worktree-relative inside the worktree', () => {
    expect(memoryDisplayPath('/home/u/wt/CLAUDE.md', '/home/u/wt')).toBe('CLAUDE.md')
    expect(memoryDisplayPath('/home/u/wt/.claude/rules/r.md', '/home/u/wt/')).toBe('.claude/rules/r.md')
  })

  it('is absolute outside it, and for a sibling that only shares a prefix', () => {
    expect(memoryDisplayPath('/home/u/.claude/CLAUDE.md', '/home/u/wt')).toBe('/home/u/.claude/CLAUDE.md')
    expect(memoryDisplayPath('/home/u/wt2/CLAUDE.md', '/home/u/wt')).toBe('/home/u/wt2/CLAUDE.md')
  })

  it('is absolute when the worktree is unknown', () => {
    expect(memoryDisplayPath('/w/CLAUDE.md', null)).toBe('/w/CLAUDE.md')
    expect(memoryDisplayPath('/w/CLAUDE.md', '')).toBe('/w/CLAUDE.md')
  })
})
