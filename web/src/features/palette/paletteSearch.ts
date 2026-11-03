/**
 * The ⌘K palette's matcher — pure, no React (D-10).
 *
 * Ranking per term: PREFIX of a field (0) > a WORD START inside it (1) > any
 * SUBSTRING (2); an entry's score is the sum over the query's terms of each
 * term's best field. Every term must match. Ties keep input order
 * (`Array.prototype.sort` is stable), so a section's own order survives.
 *
 * FACET terms — `repo:<nameWithOwner>` and `branch:<name>` — are exact: an
 * entry matches only if its `facets` value equals the term (case-insensitive,
 * like everything here), and an entry without that facet fails. They are the
 * palette's drill-downs, so `branch:main` is never a session titled "Maintain…"
 * and `repo:o/app` never `o/app-two`. A bare `repo:` (still being typed)
 * constrains nothing. Facet terms add nothing to the score.
 *
 * Cost model, pinned by the 1,000-entry test: everything per-ENTRY is done once
 * in `prepareSearch` (lowercasing, and a word-boundary copy of each field), and
 * a query is lowercased once. Matching is then at most three string scans per
 * field per term — `startsWith`, and two `indexOf`s — with no regex anywhere.
 */

export interface Facets {
  repo?: string
  branch?: string
}

export interface Searchable {
  label: string
  keywords?: readonly string[]
  facets?: Facets
}

type FacetName = keyof Facets
/** The facet prefixes, with their names — matched by `startsWith`, not regex. */
const FACET_PREFIXES: readonly (readonly [string, FacetName])[] = [['repo:', 'repo'], ['branch:', 'branch']]

interface PreparedField {
  /** The field, lowercased. */
  text: string
  /** `' '` + the field with every non-alphanumeric char turned into a space,
   *  so `indexOf(' ' + term)` finds a term at a word start in one scan. */
  words: string
}

export interface Prepared<T> {
  item: T
  fields: readonly PreparedField[]
  /** `facets`, lowercased once. */
  facets: Facets
}

/** Counts string scans, for the op-budget test. */
export interface SearchOps {
  scans: number
}

const PREFIX = 0
const WORD_START = 1
const SUBSTRING = 2

function isAlnum(code: number): boolean {
  return (code >= 48 && code <= 57) || (code >= 97 && code <= 122) || code > 127
}

/** Non-alphanumerics → spaces. `s` is already lowercase. */
function wordsOf(s: string): string {
  let out = ' '
  for (let i = 0; i < s.length; i++) {
    const code = s.charCodeAt(i)
    out += isAlnum(code) ? s[i] : ' '
  }
  return out
}

function prepareField(raw: string): PreparedField {
  const text = raw.toLowerCase()
  return { text, words: wordsOf(text) }
}

export function prepareSearch<T extends Searchable>(items: readonly T[]): Prepared<T>[] {
  return items.map((item) => ({
    item,
    fields: [item.label, ...(item.keywords ?? [])].map(prepareField),
    facets: {
      repo: item.facets?.repo?.toLowerCase(),
      branch: item.facets?.branch?.toLowerCase(),
    },
  }))
}

interface Term {
  text: string
  /** `' '` + the term's word form, for the word-start scan. */
  words: string
}

function rankField(f: PreparedField, t: Term, ops: SearchOps | undefined): number | null {
  if (ops) ops.scans += 1
  if (f.text.startsWith(t.text)) return PREFIX
  if (ops) ops.scans += 1
  if (f.words.indexOf(t.words) >= 0) return WORD_START
  if (ops) ops.scans += 1
  if (f.text.indexOf(t.text) >= 0) return SUBSTRING
  return null
}

/**
 * The entries matching `query`, best first. An empty (or blank) query returns
 * every entry in input order.
 */
export function searchPrepared<T>(prepared: readonly Prepared<T>[], query: string, ops?: SearchOps): T[] {
  const lowered = query.toLowerCase()
  const terms: Term[] = []
  const facetTerms: (readonly [FacetName, string])[] = []
  for (const raw of lowered.split(' ')) {
    if (raw === '') continue
    const facet = FACET_PREFIXES.find(([prefix]) => raw.startsWith(prefix))
    if (facet) {
      const value = raw.slice(facet[0].length)
      if (value !== '') facetTerms.push([facet[1], value])
      continue
    }
    terms.push({ text: raw, words: wordsOf(raw) })
  }
  if (terms.length === 0 && facetTerms.length === 0) return prepared.map((p) => p.item)

  const hits: { item: T; score: number }[] = []
  for (const p of prepared) {
    let score = 0
    let matched = true
    for (const [name, value] of facetTerms) {
      if (ops) ops.scans += 1
      if (p.facets[name] !== value) { matched = false; break }
    }
    if (!matched) continue
    for (const t of terms) {
      let best: number | null = null
      for (const f of p.fields) {
        const r = rankField(f, t, ops)
        if (r !== null && (best === null || r < best)) best = r
        if (best === PREFIX) break
      }
      if (best === null) { matched = false; break }
      score += best
    }
    if (matched) hits.push({ item: p.item, score })
  }
  hits.sort((a, b) => a.score - b.score)
  return hits.map((h) => h.item)
}
