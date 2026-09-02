import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import css from '@/index.css?inline'

// Tailwind v4 generates a utility only when its class name appears in a scanned
// source file. This constant IS that appearance for the token utilities below —
// keep it in sync with REQUIRED.
export const PROBE =
  'bg-canvas bg-surface bg-surface-raised text-ink text-ink-muted text-ink-faint border-edge border-edge-strong ' +
  'bg-accent text-accent-ink bg-accent-wash ring-accent ' +
  'bg-success bg-success-wash text-warning bg-danger bg-info bg-neutral ' +
  'z-raised z-sticky z-nav z-popover z-overlay z-drawer z-dialog z-palette z-toast ' +
  'min-h-target min-w-target rounded-sm rounded-md rounded-lg rounded-pill text-2xs text-xs text-sm text-base'

const REQUIRED = PROBE.split(' ')

// Names the audit found shipping zero CSS. None may remain in source.
const DEAD = [
  'bg-primary', 'text-primary-foreground', 'border-input', 'text-accent-foreground', 'ring-ring',
  'bg-destructive', 'text-destructive-foreground', 'bg-muted', 'text-muted-foreground', 'bg-card',
  'border-border', 'bg-background', 'text-foreground',
  'border-[var(--border)]', 'border-[--border]', 'bg-[var(--muted)]', 'bg-[var(--accent)]',
  'text-[var(--accent-foreground)]', 'bg-[--bg]',
  'border-primary', 'text-destructive',
]

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) walk(p, out)
    else if (/\.(tsx?|css)$/.test(name)) out.push(p)
  }
  return out
}

describe('token bridge', () => {
  it('compiles every token utility the app relies on', () => {
    for (const cls of REQUIRED) {
      const sel = '.' + cls.replace(/[.[\]/]/g, (m) => '\\' + m)
      expect(css, `missing utility ${cls}`).toMatch(new RegExp(sel.replace(/\\/g, '\\\\') + '\\s*\\{'))
    }
  })

  it('does not emit the dead shadcn names (they were never defined)', () => {
    for (const cls of ['bg-primary', 'border-border', 'text-muted-foreground']) {
      expect(css).not.toMatch(new RegExp('\\.' + cls + '\\s*\\{'))
    }
  })

  it('has no dead class names left in source', () => {
    const src = join(__dirname, '..')
    const offenders: string[] = []
    for (const file of walk(src)) {
      if (file.endsWith('tokenBridge.test.ts')) continue
      const text = readFileSync(file, 'utf8')
      for (const dead of DEAD) {
        const re = new RegExp('(^|[\\s"\'`:])' + dead.replace(/[.*+?^${}()|[\]\\/]/g, '\\$&') + '(?![\\w-])')
        if (re.test(text)) offenders.push(`${file.slice(src.length + 1)} uses ${dead}`)
      }
    }
    expect(offenders).toEqual([])
  })
})
