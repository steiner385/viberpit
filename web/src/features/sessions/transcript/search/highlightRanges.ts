/**
 * Where the transcript search's terms occur in a piece of rendered text (D-11).
 *
 * Pure and regex-free on purpose: the terms are whatever the user typed, so a
 * `RegExp` built from them would turn `a.*b` or `(` into a pattern (or a throw).
 * Plain `indexOf` over lowercased text is the same matching rule the server's
 * transcript search uses (`server/sessions/search.py` `terms_match`).
 */

/** A half-open `[start, end)` range of UTF-16 offsets into the original text. */
export type Range = [number, number]

/**
 * The query's terms — lowercased, split on whitespace, empties dropped. The client
 * twin of the server's `parse_terms`, so what is highlighted is what was matched.
 */
export function parseSearchTerms(query: string): string[] {
  return query.toLowerCase().split(/\s+/).filter(Boolean)
}

/**
 * Lowercase `text`, keeping for every lowered code unit the original `[start, end)`
 * of the character it came from. `toLowerCase` can change a string's length
 * ("İ" lowers to two units), so offsets found in the lowered copy are mapped back
 * through this rather than assumed equal.
 */
function lowerWithMap(text: string): { lower: string; starts: number[]; ends: number[] } {
  let lower = ''
  const starts: number[] = []
  const ends: number[] = []
  for (let i = 0; i < text.length;) {
    const cp = text.codePointAt(i) ?? 0
    const width = cp > 0xffff ? 2 : 1
    const lc = text.slice(i, i + width).toLowerCase()
    for (let k = 0; k < lc.length; k++) {
      starts.push(i)
      ends.push(i + width)
    }
    lower += lc
    i += width
  }
  return { lower, starts, ends }
}

/**
 * Every occurrence of every term in `text`, case-insensitive, as sorted ranges with
 * overlapping or touching ones merged — one `<mark>` per merged range.
 */
export function highlightRanges(text: string, terms: readonly string[]): Range[] {
  const needles = terms.map((t) => t.toLowerCase()).filter((t) => t !== '')
  if (text === '' || needles.length === 0) return []
  const { lower, starts, ends } = lowerWithMap(text)
  const found: Range[] = []
  for (const needle of needles) {
    for (let at = lower.indexOf(needle); at !== -1; at = lower.indexOf(needle, at + 1)) {
      found.push([starts[at], ends[at + needle.length - 1]])
    }
  }
  found.sort((a, b) => a[0] - b[0] || a[1] - b[1])
  const merged: Range[] = []
  for (const [s, e] of found) {
    const last = merged[merged.length - 1]
    if (last && s <= last[1]) last[1] = Math.max(last[1], e)
    else merged.push([s, e])
  }
  return merged
}

/** A run of `text`: marked or not, and whether it holds the LEAD (first) term. */
export interface HighlightSegment {
  text: string
  mark: boolean
  /** A marked run containing the query's first term — the server places a hit at the
   *  first term's first occurrence, so the current hit's stronger style goes on the
   *  first `lead` run of its block. */
  lead: boolean
}

/** `text` cut into plain and marked runs (`highlightRanges`), in order. */
export function highlightSegments(text: string, terms: readonly string[]): HighlightSegment[] {
  const ranges = highlightRanges(text, terms)
  if (ranges.length === 0) return [{ text, mark: false, lead: false }]
  const leadTerm = terms[0]?.toLowerCase() ?? ''
  const out: HighlightSegment[] = []
  let pos = 0
  for (const [s, e] of ranges) {
    if (s > pos) out.push({ text: text.slice(pos, s), mark: false, lead: false })
    const run = text.slice(s, e)
    out.push({ text: run, mark: true, lead: leadTerm !== '' && run.toLowerCase().includes(leadTerm) })
    pos = e
  }
  if (pos < text.length) out.push({ text: text.slice(pos), mark: false, lead: false })
  return out
}
