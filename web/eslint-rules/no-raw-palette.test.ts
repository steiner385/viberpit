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
  ],
  invalid: [
    { code: '<div className="bg-gray-100" />', errors: [{ messageId: 'palette' }] },
    { code: '<div className="text-ink dark:text-white" />', errors: [{ messageId: 'dark' }] },
    { code: 'cn("border", "border-blue-500/50")', errors: [{ messageId: 'palette' }] },
    { code: 'cva("z-50")', errors: [{ messageId: 'zLiteral' }] },
    { code: '<div className={`min-h-[44px] ${x}`} />', errors: [{ messageId: 'pxLiteral' }] },
    { code: '<div className="bg-[#fff]" />', errors: [{ messageId: 'hex' }] },
  ],
})
