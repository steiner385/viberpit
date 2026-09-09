import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'
import { execSync } from 'node:child_process'
import { buildManifest } from './src/pwa/manifest'

function resolveBuildSha(): string {
  if (process.env.BUILD_SHA) return process.env.BUILD_SHA
  try {
    return execSync('git rev-parse --short HEAD').toString().trim()
  } catch {
    return 'dev'
  }
}

/** Emits the web app manifest at build time from the same source the app's
 *  brand comes from. `emitFile` only runs on a real build, so
 *  `/manifest.webmanifest` is absent under `vite dev` — testing installation
 *  needs `npm run build && npm run preview`, not the dev server. */
function webManifestPlugin(): Plugin {
  const body = () => JSON.stringify(buildManifest(), null, 2)
  return {
    name: 'viberpit-web-manifest',
    apply: 'build',
    generateBundle() {
      this.emitFile({ type: 'asset', fileName: 'manifest.webmanifest', source: body() })
    },
  }
}

export default defineConfig({
  plugins: [react(), tailwindcss(), webManifestPlugin()],
  define: {
    __BUILD_SHA__: JSON.stringify(resolveBuildSha()),
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8787',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://127.0.0.1:8787',
        ws: true,
      },
    },
  },
})
