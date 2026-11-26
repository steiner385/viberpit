/**
 * The accessibility gate for the cross-browser specs (#1412, G14).
 *
 * `@axe-core/playwright` scans the rendered page in the engine under test. There
 * is NO blanket `disableRules`: a finding is either fixed, or it is listed below
 * with a classification and the control that covers the ground axe cannot. A
 * finding that is in neither place fails the run — including a rule axe merely
 * marks "incomplete" (needs review), because "incomplete" is exactly where an
 * analyzer hides what it could not decide.
 *
 * `docs/product/ui-a11y-analyzer-limitations.md` explains the entries for a
 * human reader; this table is the source of truth the gate reads.
 */
import AxeBuilder from '@axe-core/playwright'
import { expect, type Page, type TestInfo } from '@playwright/test'
import { publishEvidence } from './evidence'

/** The WCAG levels ViberPit targets (docs/ACCESSIBILITY.md §1: 2.2 AA in full). */
export const AXE_TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'] as const

export type LimitationKind =
  /** axe reports something that is not a defect in our markup. */
  | 'analyzer-false-positive'
  /** axe could not decide; a different control decides it. */
  | 'analyzer-undecidable'
  /** A real, accepted, tracked deviation — not silenced, just not blocking. */
  | 'known-deviation'

export interface AnalyzerLimitation {
  /** Stable id, quoted in the doc. */
  id: string
  /** axe rule id. */
  rule: string
  /** Which axe result bucket it appears in. */
  bucket: 'violations' | 'incomplete'
  kind: LimitationKind
  /** Only entries whose offending node matches this CSS-ish substring are tolerated. */
  targetIncludes?: string
  /** Why this is not a fix-now defect. */
  why: string
  /** What covers the ground instead (a test, a doc section, an issue). */
  coveredBy: string
}

export const ANALYZER_LIMITATIONS: readonly AnalyzerLimitation[] = [
  {
    id: 'L1-contrast-overlap',
    rule: 'color-contrast',
    bucket: 'incomplete',
    kind: 'analyzer-undecidable',
    why: 'axe cannot compute a background colour for text that sits under another positioned element '
      + '("background color could not be determined because it is overlapped by another element"). '
      + 'It reports "needs review", not a ratio.',
    coveredBy: 'web/src/__tests__/contrast.test.ts asserts every foreground/background token pair in both themes; '
      + 'axe still FAILS on any node it can measure.',
  },
  {
    id: 'L2-session-listbox-label',
    rule: 'aria-prohibited-attr',
    bucket: 'incomplete',
    kind: 'known-deviation',
    targetIncludes: '#session-listbox',
    why: 'The session list is a deliberately role-less <div aria-label> — the dock textarea aria-controls it and '
      + 'owns aria-activedescendant, which is not allowed on a role-less element (docs/ACCESSIBILITY.md §2).',
    coveredBy: 'docs/ACCESSIBILITY.md §2; the list is also reachable as the labelled <aside> region that contains it.',
  },
  {
    id: 'L3-status-pill-label',
    rule: 'aria-prohibited-attr',
    bucket: 'incomplete',
    kind: 'analyzer-false-positive',
    targetIncludes: 'aria-label="Status:',
    why: 'StatusPill names itself with aria-label on a <span> AND renders the same words as visible text, so '
      + 'no information depends on the prohibited attribute being honoured.',
    coveredBy: 'ui/StatusPill.test.tsx (icon + text label, never colour alone); docs/ACCESSIBILITY.md StatusPill entry.',
  },
  {
    id: 'L4-usage-badge-label',
    rule: 'aria-prohibited-attr',
    bucket: 'incomplete',
    kind: 'known-deviation',
    targetIncludes: 'aria-label="weekly usage',
    why: 'The session card usage badge puts a descriptive aria-label ("weekly usage: 50% — no active window") on a '
      + 'role-less <span>. ARIA 1.2 prohibits that, and unlike L3 the label adds words the visible "50%" does not '
      + 'carry, so a reader that ignores it loses the window name. Real, accepted for now, not silenced.',
    coveredBy: 'Follow-up: give the badge a role (role="img") or move the words into visually-hidden text. '
      + 'Listed in docs/audits/ui-conformance-scorecard.md.',
  },
  {
    id: 'L5-logotype-contrast',
    rule: 'color-contrast',
    bucket: 'violations',
    kind: 'analyzer-false-positive',
    targetIncludes: 'class="text-brand"',
    why: 'The "Pit" half of the wordmark is the brand green at 2.3:1. WCAG 1.4.3 exempts "text that is part of a '
      + 'logo or brand name"; axe has no way to know a span is a logotype. It is aria-hidden — the accessible name '
      + 'is the whole word.',
    coveredBy: 'shell/BrandMark.tsx (documents the exemption); --t-brand is used for the logotype only (index.css).',
  },
]

interface AxeNode { target: unknown; html: string }
interface AxeFinding { id: string; impact?: string | null; help: string; nodes: AxeNode[] }

const nodeText = (n: AxeNode): string => `${JSON.stringify(n.target)} ${n.html}`

/** Is every node of this finding covered by a listed limitation? */
function isClassified(finding: AxeFinding, bucket: AnalyzerLimitation['bucket']): boolean {
  const entries = ANALYZER_LIMITATIONS.filter((l) => l.rule === finding.id && l.bucket === bucket)
  if (entries.length === 0) return false
  return finding.nodes.every((n) =>
    entries.some((l) => l.targetIncludes === undefined || nodeText(n).includes(l.targetIncludes)))
}

export interface A11yReport {
  /** Violations nobody classified — the gate fails on these. */
  violations: AxeFinding[]
  /** "Needs review" results nobody classified — the gate fails on these too. */
  incomplete: AxeFinding[]
  /** Everything axe said, for the artifact. */
  all: { violations: AxeFinding[]; incomplete: AxeFinding[] }
}

/** Scan without asserting — the seeded-defect specs use this to prove the scan can bite. */
export async function scanA11y(page: Page, include?: string): Promise<A11yReport> {
  const builder = new AxeBuilder({ page }).withTags([...AXE_TAGS])
  if (include) builder.include(include)
  const result = await builder.analyze()
  const violations = result.violations as AxeFinding[]
  const incomplete = result.incomplete as AxeFinding[]
  return {
    violations: violations.filter((f) => !isClassified(f, 'violations')),
    incomplete: incomplete.filter((f) => !isClassified(f, 'incomplete')),
    all: { violations, incomplete },
  }
}

const describeFinding = (f: AxeFinding): string =>
  `${f.id} (${f.impact ?? 'n/a'}) — ${f.help}\n`
  + f.nodes.slice(0, 3).map((n) => `    ${JSON.stringify(n.target)}\n    ${n.html.slice(0, 160)}`).join('\n')

/**
 * Fail unless the page has no unclassified axe finding. The full, unfiltered
 * result is attached to the test so the CI artifact shows what axe saw — the
 * classified entries included — without a reader re-running anything.
 */
export async function expectNoA11yViolations(page: Page, testInfo: TestInfo, include?: string): Promise<void> {
  const report = await scanA11y(page, include)
  await publishEvidence(testInfo, 'axe-results.json', JSON.stringify({
    url: new URL(page.url()).pathname, // path only: never the host or a query string
    violations: report.all.violations.map((f) => ({ id: f.id, impact: f.impact, nodes: f.nodes.length })),
    incomplete: report.all.incomplete.map((f) => ({ id: f.id, nodes: f.nodes.length })),
  }, null, 2), 'application/json')
  expect(report.violations.map(describeFinding), 'axe violations').toEqual([])
  expect(report.incomplete.map(describeFinding), 'axe "needs review" results with no classification').toEqual([])
}
