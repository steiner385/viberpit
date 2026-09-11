import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'
import noRawPalette from './eslint-rules/no-raw-palette.js'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      globals: globals.browser,
    },
  },
  {
    // Token discipline is enforced folder-by-folder: the new UI system and
    // shell first, features/ joined in PR 4 (task 1), pipeline/ once row 5 put it
    // on tokens, the whole tree in row 7 (7a: everything but `components/`; 7f: everything).
    files: ['src/**/*.{ts,tsx}'],
    ignores: ['src/components/**', 'src/features/pipeline/charts/**'],
    plugins: { ccswitch: { rules: { 'no-raw-palette': noRawPalette } } },
    rules: { 'ccswitch/no-raw-palette': 'error' },
  },
])
