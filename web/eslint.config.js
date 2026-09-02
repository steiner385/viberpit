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
    // Token discipline is enforced folder-by-folder: the new UI system first,
    // each feature folder as sub-spec C migrates it, the whole tree in PR 11.
    files: ['src/ui/**/*.{ts,tsx}', 'src/shell/**/*.{ts,tsx}'],
    ignores: ['src/features/pipeline/charts/**'],
    plugins: { ccswitch: { rules: { 'no-raw-palette': noRawPalette } } },
    rules: { 'ccswitch/no-raw-palette': 'error' },
  },
])
