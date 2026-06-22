export type Segment =
  | { kind: 'prose'; text: string }
  | { kind: 'insight'; text: string }

// Rule glyphs we accept as delimiter "dashes": box-drawing ─, em —, and en –.
// (ASCII hyphens are intentionally excluded — they collide with Markdown rules,
// table separators, and front-matter fences.)
const RULE = '─—–'
// Decoration allowed around the anchor on a delimiter line: whitespace, the rule
// glyphs, and optional Markdown backticks (the insight-generation prompt wraps
// the delimiter lines in backticks).
// Opener: an optionally backtick-wrapped line that is "★ Insight" followed only
// by rule decoration (which must include at least one rule glyph).
const OPENER = new RegExp(`^\\s*\`?\\s*★\\s*Insight\\b[\`\\s${RULE}]*[${RULE}][\`\\s${RULE}]*$`)
// Closer: an optionally backtick-wrapped line of only rule glyphs.
const CLOSER = new RegExp(`^\\s*\`?\\s*[${RULE}]+\\s*\`?\\s*$`)

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
