export type Segment =
  | { kind: 'prose'; text: string }
  | { kind: 'insight'; text: string }

// Rule glyphs we accept as delimiter "dashes": box-drawing ─, em —, and en –.
// (ASCII hyphens are intentionally excluded — they collide with Markdown rules,
// table separators, and front-matter fences.)
const RULE = '─—–'
// A "run" is ≥3 consecutive rule glyphs. The run — not a stray single dash — is
// the callout signal, so a prose line like "★ Insight — a passing thought" is NOT
// mistaken for an opener.
const RUN = `[${RULE}]{3,}`
// Opener: a line that starts with "★ Insight" (optionally backtick-prefixed) and
// contains a rule run somewhere — whether right after "Insight"
// (`★ Insight ─────`), with body trailing the run (`★ Insight ───── note`), or
// with an inline title before a trailing run (`★ Insight — title ─────`). The
// rest of the line (capture group 1) is cleaned into inline body by parseInsights.
const OPENER = new RegExp(`^\\s*\`?\\s*★\\s*Insight\\b(?=.*${RUN})(.*)$`)
const RUN_G = new RegExp(RUN, 'g')
// Leading separators to strip off an inline title: whitespace, backticks, rule
// glyphs (e.g. the "— " in "★ Insight — title"), and a colon.
const LEAD_SEP = new RegExp(`^[\\s\`${RULE}:]+`)
// Closer: an optionally backtick-wrapped line of only rule glyphs.
const CLOSER = new RegExp(`^\\s*\`?\\s*[${RULE}]+\\s*\`?\\s*$`)

// Extract the inline body/title an opener carried on its own line: drop backticks
// and rule runs, then strip a leading separator. Returns '' for a pure-rule opener.
function inlineBody(remainder: string): string {
  return remainder
    .replace(/`/g, '')
    .replace(RUN_G, ' ')
    .replace(LEAD_SEP, '')
    .trim()
}

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
    const opener = OPENER.exec(lines[i])
    if (opener) {
      let j = i + 1
      while (j < lines.length && !CLOSER.test(lines[j])) j++
      if (j < lines.length) {
        flushProse()
        // Body = any text the opener carried on its own line, then the lines
        // up to the closer.
        const head = inlineBody(opener[1] ?? '')
        const text = [head, ...lines.slice(i + 1, j)].join('\n').trim()
        segments.push({ kind: 'insight', text })
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
