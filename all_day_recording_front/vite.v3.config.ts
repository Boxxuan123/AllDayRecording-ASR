import { resolve } from 'node:path'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

export default defineConfig({
  root: resolve(import.meta.dirname, 'v3'),
  plugins: [vue()],
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
