import { resolve } from 'node:path'
import { readFileSync } from 'node:fs'
import { execFileSync } from 'node:child_process'

function buildIdentity() {
  const root = resolve(import.meta.dirname, '..')
  const git = (...args: string[]) => execFileSync('git', ['-C', root, ...args],
    { encoding: 'utf8', timeout: 5000 }).trim()
  const release = readFileSync(resolve(root, 'pyproject.toml'), 'utf8').match(/^version = "([^"]+)"/m)?.[1]
  return { component: 'pc-ui', release_version: release, git_commit: git('rev-parse', 'HEAD'),
    built_at: new Date().toISOString(), dirty: git('status', '--porcelain', '--untracked-files=normal').length > 0,
    identity_source: 'build' }
}

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

export default defineConfig({
  root: resolve(import.meta.dirname, 'v3'),
  plugins: [vue(), {
    name: 'allday-build-identity',
    transformIndexHtml(html) { return html.replace(/\r\n?/g, '\n') },
    generateBundle() {
      this.emitFile({ type: 'asset', fileName: 'build-info.json',
        source: JSON.stringify(buildIdentity(), null, 2) })
    },
  }],
  server: {
    host: '127.0.0.1',
    port: 5174,
    proxy: {
      '/api/v3': {
        target: 'http://127.0.0.1:8766',
        changeOrigin: true,
        configure(proxy) {
          proxy.on('proxyReq', (request) => {
            request.setHeader('Origin', 'http://127.0.0.1:8766')
          })
        },
      },
    },
  },
  build: {
    outDir: '../../src/allday_asr/v3/web_assets',
    emptyOutDir: true,
    assetsDir: 'assets',
    rollupOptions: {
      output: {
        entryFileNames: 'assets/app.js',
        chunkFileNames: 'assets/[name]-[hash].js',
        assetFileNames: (assetInfo) =>
          assetInfo.names.some((name) => name.endsWith('.css'))
            ? 'assets/styles.css'
            : 'assets/[name][extname]',
      },
    },
  },
})
