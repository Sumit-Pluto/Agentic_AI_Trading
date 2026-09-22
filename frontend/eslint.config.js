import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

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
    rules: {
      // Backend endpoints return loosely-typed, highly dynamic JSON (option
      // chains, advisor structures, news payloads) — modelling every shape
      // would be pure busywork, so `any` is used deliberately at those
      // boundaries.
      '@typescript-eslint/no-explicit-any': 'off',
      // Standard fetch-on-mount/poll-on-interval effects (used throughout
      // this dashboard) trip this React-Compiler-oriented rule; it's not a
      // bug here.
      'react-hooks/set-state-in-effect': 'off',
    },
  },
])
