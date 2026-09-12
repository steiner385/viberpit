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
    // Token discipline covers the whole tree (row 7, 2026-09); the chart
    // allowlist is the one exemption.
    files: ['src/**/*.{ts,tsx}'],
    ignores: ['src/features/pipeline/charts/**'],
    plugins: { viberpit: { rules: { 'no-raw-palette': noRawPalette } } },
    rules: { 'viberpit/no-raw-palette': 'error' },
  },
])
