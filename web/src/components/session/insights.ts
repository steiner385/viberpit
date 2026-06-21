export type Segment =
  | { kind: 'prose'; text: string }
  | { kind: 'insight'; text: string }

// Opener: a line containing ★, the word Insight, and at least one box-drawing ─.
const OPENER = /^\s*★\s*Insight\b[^\n]*─[^\n]*$/
// Closer: a line of only box-drawing ─ (plus optional surrounding whitespace).
const CLOSER = /^\s*─+\s*$/

export function parseInsights(text: string): Segment[] {
  const lines = text.split('\n')
  const segments: Segment[] = []
  let prose: string[] = []

  const flushProse = () => {
    if (prose.length > 0) {
      segments.push({ kind: 'prose', text: prose.join('\n') })
      prose = []
    }
  }

  for (let i = 0; i < lines.length; i++) {
    if (OPENER.test(lines[i])) {
      let j = i + 1
      while (j < lines.length && !CLOSER.test(lines[j])) j++
      if (j < lines.length) {
        flushProse()
        segments.push({ kind: 'insight', text: lines.slice(i + 1, j).join('\n').trim() })
        i = j // skip past the closing delimiter line
        continue
      }
      // No closer found: fall through and treat the opener line as plain prose.
    }
    prose.push(lines[i])
  }
  flushProse()
  return segments
}
