import { RuleTester } from 'eslint'
import tseslint from 'typescript-eslint'
import rule, { rewriteClassString } from './no-raw-palette.js'

const tester = new RuleTester({
  languageOptions: {
    parser: tseslint.parser,
    parserOptions: { ecmaFeatures: { jsx: true } },
  },
})

tester.run('no-raw-palette', rule, {
  valid: [
    { code: '<div className="bg-surface text-ink-muted border-edge z-dialog min-h-target" />' },
    { code: 'cn("rounded-lg", open && "bg-accent-wash")' },
    { code: 'const x = "gray-500"' }, // not a className / cn() context
    // The gate, not the patterns: each of these strings WOULD be reported inside a
    // className / cn() / cva() call. Outside one they must be ignored — if the
    // inClassContext ancestor walk is ever weakened, these fail.
    { code: 'const x = "bg-gray-500"' },
    // Note: this bare hex string can never match HEX (which requires the
    // bracketed arbitrary-value form, e.g. "[#fff]"), so it does not exercise
    // the gate under the deliberate-break test below — kept anyway because it
    // mirrors theme.tsx's real CANVAS constants verbatim.
    { code: "const CANVAS = { light: '#ffffff', dark: '#16171d' }" },
    // This one does exercise the hex gate: "bg-[#fff]" matches HEX's bracketed form.
    { code: 'const x = "bg-[#fff]"' },
    { code: 'const label = "z-50"' },
    // className as an ordinary object property of a non-cn call is NOT a JSX
    // className attribute — the gate must not treat it as one.
    { code: 'foo({ className: "bg-gray-500" })' },
    // The ink tokens the whiteOnFill rule asks for are themselves clean.
    { code: '<div className="bg-accent text-accent-ink" />' },
    // text-white is only a finding BESIDE an accent/-strong fill. On anything
    // else it is someone's deliberate choice and stays unreported.
    { code: '<div className="bg-surface text-white" />' },
  ],
  invalid: [
    { code: '<div className="bg-gray-100" />', output: '<div className="bg-surface-raised" />', errors: [{ messageId: 'palette' }] },
    { code: '<div className="text-ink dark:text-white" />', output: null, errors: [{ messageId: 'dark' }] },
    { code: 'cn("border", "border-blue-500/50")', output: null, errors: [{ messageId: 'palette' }] },
    { code: 'cva("z-50")', output: 'cva("z-dialog")', errors: [{ messageId: 'zLiteral' }] },
    { code: '<div className={`min-h-[44px] ${x}`} />', output: '<div className={`min-h-target ${x}`} />', errors: [{ messageId: 'pxLiteral' }] },
    { code: '<div className="bg-[#fff]" />', output: null, errors: [{ messageId: 'hex' }] },
    {
      code: "cva('base', { variants: { tone: { bad: 'bg-gray-100' } } })",
      output: "cva('base', { variants: { tone: { bad: 'bg-surface-raised' } } })",
      errors: [{ messageId: 'palette' }],
    },
    // fixable: paired light/dark → one token, dark deleted
    {
      code: `<div className="text-gray-500 dark:text-gray-400 bg-white dark:bg-gray-800 border-gray-200 dark:border-gray-700" />`,
      output: `<div className="text-ink-muted bg-surface border-edge" />`,
      errors: [{ messageId: 'palette' }, { messageId: 'dark' }, { messageId: 'palette' }, { messageId: 'dark' }, { messageId: 'palette' }, { messageId: 'dark' }],
    },
    // fixable: variant prefixes survive; z and px literals map
    {
      code: `cn('hover:bg-gray-100 dark:hover:bg-gray-700 z-50 min-h-[44px] text-[11px]', open && 'ring-blue-500')`,
      output: `cn('hover:bg-surface-raised z-dialog min-h-target text-2xs', open && 'ring-accent')`,
      errors: 6,
    },
    // not fixable: unknown palette stays an error with no output change
    {
      code: `<div className="bg-fuchsia-500" />`,
      output: null,
      errors: [{ messageId: 'palette' }],
    },
    // A dark token with no same-key light partner is a HAND-MAPPING RESIDUAL: it
    // stays exactly as written (the dark value is not the light value, so the
    // fixer may not strip `dark:` and emit the base's mapping) and the error has
    // no fix.
    {
      code: `<div className="dark:text-gray-300" />`,
      output: null,
      errors: [{ messageId: 'dark' }],
    },
    // template literal: only the static chunk is rewritten
    {
      code: 'cn(`text-gray-500 ${x} dark:text-gray-400`)',
      output: 'cn(`text-ink-muted ${x} `)',
      errors: 2,
    },
    // C1: a dark: token whose light partner (same partner key) mapped is dropped,
    // including across sibling ARGUMENTS of the same cn() call — cn is
    // twMerge(clsx(…)), so a dark:-only argument merges last-wins over its light
    // sibling and must be judged together with it. Both dark tokens go and the
    // second literal empties (a valid cn argument).
    {
      code: `cn('bg-blue-600 hover:bg-blue-700', 'dark:bg-blue-700 dark:hover:bg-blue-600')`,
      output: `cn('bg-accent hover:bg-accent/90', '')`,
      errors: [{ messageId: 'palette' }, { messageId: 'palette' }, { messageId: 'dark' }, { messageId: 'dark' }],
    },
    // C1: the partner key is per utility prefix, not string-wide — `bg-white`
    // mapping does not license dropping a `dark:border-*` token.
    {
      code: `<div className="border-transparent bg-white dark:border-gray-700" />`,
      output: `<div className="border-transparent bg-surface dark:border-gray-700" />`,
      errors: [{ messageId: 'palette' }, { messageId: 'dark' }],
    },
    // C1: a CONDITIONAL sibling argument does not seed partner keys — that string
    // may not be applied at all, so its light token cannot stand in for the dark one.
    {
      code: `cn('dark:text-gray-400', open && 'text-gray-500')`,
      output: `cn('dark:text-gray-400', open && 'text-ink-muted')`,
      errors: [{ messageId: 'dark' }, { messageId: 'palette' }],
    },
    // I1: an interior quasi that empties leaves ONE separator behind — otherwise
    // the neighbouring interpolations glue together (`${a}${b}` → "ring-2p-4").
    {
      code: 'cn(`text-gray-500 ${a} dark:text-gray-400 ${b} p-2`)',
      output: 'cn(`text-ink-muted ${a} ${b} p-2`)',
      errors: 2,
    },
    // C2: text-white over an accent fill is ~2.7:1 in dark mode. Reported, never
    // auto-fixed — which ink token applies is a per-site hand map.
    {
      code: `<div className="bg-blue-600 text-white" />`,
      output: `<div className="bg-accent text-white" />`,
      errors: [{ messageId: 'palette' }, { messageId: 'whiteOnFill' }],
    },
    // `!`-marked classes map through the table with the marker re-attached
    // (leading `!` is Tailwind v3, trailing `!` is v4).
    {
      code: `<div className="!text-gray-500 text-gray-500!" />`,
      output: `<div className="!text-ink-muted text-ink-muted!" />`,
      errors: [{ messageId: 'palette' }, { messageId: 'palette' }],
    },
    // Only COLOUR tokens partner: a mapped size literal sharing the `text`/`min`
    // prefix must not delete an unrelated dark: colour (or dark: size) token.
    {
      code: `<div className="text-[11px] dark:text-gray-400" />`,
      output: `<div className="text-2xs dark:text-gray-400" />`,
      errors: [{ messageId: 'pxLiteral' }, { messageId: 'dark' }],
    },
    {
      code: `<div className="min-h-[44px] dark:min-w-[44px]" />`,
      output: `<div className="min-h-target dark:min-w-[44px]" />`,
      errors: [{ messageId: 'pxLiteral' }, { messageId: 'dark' }],
    },
    // whiteOnFill also catches an opacity-suffixed text-white.
    {
      code: `<div className="bg-accent text-white/80" />`,
      output: null,
      errors: [{ messageId: 'whiteOnFill' }],
    },
    // Recommendation 1: a hover/focus/active onto `bg-canvas` is a no-op on a
    // canvas-backed page — the fixer bumps it a tier to bg-surface-raised
    // instead of following the light-state bg-gray-50 → bg-canvas mapping.
    {
      code: `<div className="bg-surface hover:bg-gray-50 dark:hover:bg-gray-700" />`,
      output: `<div className="bg-surface hover:bg-surface-raised" />`,
      errors: [{ messageId: 'palette' }, { messageId: 'dark' }],
    },
  ],
})

describe('rewriteClassString', () => {
  it('leaves already-token strings alone', () => {
    expect(rewriteClassString('bg-surface text-ink')).toEqual({ out: 'bg-surface text-ink', changed: false })
  })
  it('drops a dark twin only when a light token with the SAME partner key mapped', () => {
    expect(rewriteClassString('text-gray-500 dark:text-gray-400').out).toBe('text-ink-muted')
    // different utility prefix → not a partner; the dark token is left in place
    expect(rewriteClassString('border-gray-100 dark:border-gray-700')).toEqual({
      out: 'border-gray-100 dark:border-gray-700', changed: false,
    })
    expect(rewriteClassString('text-gray-500 dark:border-gray-700').out).toBe('text-ink-muted dark:border-gray-700')
  })
  it('takes partner keys seeded from a sibling quasi or argument', () => {
    expect(rewriteClassString('dark:bg-gray-800', new Set(['|bg'])).out).toBe('')
    expect(rewriteClassString('dark:bg-gray-800', new Set(['|text'])).out).toBe('dark:bg-gray-800')
    expect(rewriteClassString('dark:hover:bg-gray-700', new Set(['hover:|bg'])).out).toBe('')
  })
  it('collapses duplicate tokens produced by a pair mapping to the same token', () => {
    expect(rewriteClassString('text-gray-700 dark:text-gray-300 text-gray-900').out).toBe('text-ink')
  })
})
