// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { formatRepoLabel } from '../repoLabel'

describe('formatRepoLabel', () => {
  it('labels a repo-less session "Scratch" and flags it as scratch', () => {
    for (const empty of [null, undefined, '']) {
      const label = formatRepoLabel(empty)
      expect(label.text).toBe('Scratch')
      expect(label.isScratch).toBe(true)
    }
  })

  it('shows the repo and is not scratch when a repo is set', () => {
    const label = formatRepoLabel('acme/widget')
    // The owner/name-vs-name choice is yours, but the repo's short name must
    // always be visible so the user can tell repos apart.
    expect(label.text).toContain('widget')
    expect(label.isScratch).toBe(false)
  })
})
