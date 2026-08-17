import { describe, it, expect } from 'vitest'
import { repoFromCwd } from '../repoFromCwd'

describe('repoFromCwd', () => {
  it('uses the repo dir above a ccswitch worktree path', () => {
    expect(repoFromCwd('/home/u/GitHub/cc-account-switcher/.ccswitch/worktrees/cc-account-switcher/19bc'))
      .toBe('cc-account-switcher')
  })
  it('takes the segment after worktrees/ for a shared .ccswitch dir', () => {
    // Real layout: .ccswitch sits under a shared parent (GitHub), not under the repo.
    expect(repoFromCwd('/home/tony/GitHub/.ccswitch/worktrees/KinDash/9be6e554')).toBe('KinDash')
  })
  it('uses the basename for a ccswitch clone path', () => {
    // Clones live at .ccswitch/clones/<owner>/<repo>; the repo is the basename.
    expect(repoFromCwd('/home/tony/GitHub/.ccswitch/clones/cairnea/KinDash')).toBe('KinDash')
  })
  it('falls back to the basename for a plain repo dir', () => {
    expect(repoFromCwd('/home/u/GitHub/KinDash')).toBe('KinDash')
  })
  it('ignores a trailing slash', () => {
    expect(repoFromCwd('/home/u/GitHub/KinDash/')).toBe('KinDash')
  })
  it('returns null for empty / nullish', () => {
    expect(repoFromCwd('')).toBeNull()
    expect(repoFromCwd(null)).toBeNull()
    expect(repoFromCwd(undefined)).toBeNull()
  })
})
