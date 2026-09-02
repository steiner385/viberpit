import { RuleTester } from 'eslint'
import tseslint from 'typescript-eslint'
import rule from './no-raw-palette.js'

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
    { code: '<div className="bg-gray-100" />', errors: [{ messageId: 'palette' }] },
    { code: '<div className="text-ink dark:text-white" />', errors: [{ messageId: 'dark' }] },
    { code: 'cn("border", "border-blue-500/50")', errors: [{ messageId: 'palette' }] },
    { code: 'cva("z-50")', errors: [{ messageId: 'zLiteral' }] },
    { code: '<div className={`min-h-[44px] ${x}`} />', errors: [{ messageId: 'pxLiteral' }] },
    { code: '<div className="bg-[#fff]" />', errors: [{ messageId: 'hex' }] },
    {
      code: "cva('base', { variants: { tone: { bad: 'bg-gray-100' } } })",
      errors: [{ messageId: 'palette' }],
    },
  ],
})
