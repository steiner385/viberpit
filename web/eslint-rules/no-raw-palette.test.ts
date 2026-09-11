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
  ],
  invalid: [
    { code: '<div className="bg-gray-100" />', output: '<div className="bg-surface-raised" />', errors: [{ messageId: 'palette' }] },
    { code: '<div className="text-ink dark:text-white" />', errors: [{ messageId: 'dark' }] },
    { code: 'cn("border", "border-blue-500/50")', errors: [{ messageId: 'palette' }] },
    { code: 'cva("z-50")', output: 'cva("z-dialog")', errors: [{ messageId: 'zLiteral' }] },
    { code: '<div className={`min-h-[44px] ${x}`} />', output: '<div className={`min-h-target ${x}`} />', errors: [{ messageId: 'pxLiteral' }] },
    { code: '<div className="bg-[#fff]" />', errors: [{ messageId: 'hex' }] },
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
    // dark token with no light partner maps by its base
    {
      code: `<div className="dark:text-gray-300" />`,
      output: `<div className="text-ink" />`,
      errors: [{ messageId: 'dark' }],
    },
    // template literal: only the static chunk is rewritten
    {
      code: 'cn(`text-gray-500 ${x} dark:text-gray-400`)',
      output: 'cn(`text-ink-muted ${x}`)',
      errors: 2,
    },
  ],
})

describe('rewriteClassString', () => {
  it('leaves already-token strings alone', () => {
    expect(rewriteClassString('bg-surface text-ink')).toEqual({ out: 'bg-surface text-ink', changed: false })
  })
  it('drops a dark twin only when its light side mapped', () => {
    expect(rewriteClassString('text-gray-500 dark:text-gray-400').out).toBe('text-ink-muted')
    expect(rewriteClassString('text-fuchsia-500 dark:text-gray-400').out).toBe('text-fuchsia-500 text-ink-muted')
  })
  it('collapses duplicate tokens produced by a pair mapping to the same token', () => {
    expect(rewriteClassString('text-gray-700 dark:text-gray-300 text-gray-900').out).toBe('text-ink')
  })
})
