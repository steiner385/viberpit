// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { repoFromCwd } from '../repoFromCwd'

describe('repoFromCwd', () => {
  it('uses the repo dir above a ViberPit worktree path', () => {
    expect(repoFromCwd('/home/u/GitHub/viberpit/.ccswitch/worktrees/viberpit/19bc'))
      .toBe('viberpit')
  })
  it('takes the segment after worktrees/ for a shared .ccswitch dir', () => {
    // Real layout: .ccswitch sits under a shared parent (GitHub), not under the repo.
    expect(repoFromCwd('/home/tony/GitHub/.ccswitch/worktrees/KinDash/9be6e554')).toBe('KinDash')
  })
  it('reads a worktree under the .viberpit namespace the same way (#1373)', () => {
    expect(repoFromCwd('/home/tony/GitHub/.viberpit/worktrees/KinDash/9be6e554')).toBe('KinDash')
    expect(repoFromCwd('/home/tony/GitHub/.viberpit/clones/cairnea/KinDash')).toBe('KinDash')
  })
  it('uses the basename for a ViberPit clone path', () => {
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
