import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'
import { fileURLToPath, URL } from 'node:url'

const FLASK_TARGET = process.env.RACELINK_FLASK_URL || 'http://127.0.0.1:5000'

// Vite serves the SPA for Flask. The blueprint reads
// ``racelink/static/dist/index.html`` and renders it through Jinja so the
// ``rl_base_path`` placeholder still works (see § 3 of the migration spec).
//
// ``base`` is hard-coded to the URL where Flask serves the bundle's hashed
// assets (``{url_prefix}/static/dist/...``). The PoC assumes the default
// ``url_prefix=/racelink`` — operators with a custom prefix re-build with
// ``RACELINK_BUNDLE_BASE=/their-prefix/static/dist/``. Phase 2 swaps this
// for a runtime-resolved manifest read.
//
// The dev proxy mirrors the production routing: every API + SSE call under
// /racelink/api/ hits Flask. ``configure: ws=false`` keeps Vite from
// buffering ``text/event-stream`` (the SSE wire-format relies on the chunks
// not being held back).
const BUNDLE_BASE = process.env.RACELINK_BUNDLE_BASE || '/racelink/static/dist/'

export default defineConfig({
  plugins: [vue(), tailwindcss()],
  base: BUNDLE_BASE,
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  build: {
    outDir: '../racelink/static/dist',
    emptyOutDir: true,
    assetsDir: 'assets',
    sourcemap: true,
    manifest: true,
    rollupOptions: {
      input: fileURLToPath(new URL('./index.html', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    strictPort: false,
    proxy: {
      '/racelink/api': {
        target: FLASK_TARGET,
        changeOrigin: false,
        ws: false,
        // Don't buffer SSE.
        configure: (proxy) => {
          proxy.on('proxyRes', (proxyRes) => {
            const ctype = String(proxyRes.headers['content-type'] || '')
            if (ctype.includes('text/event-stream')) {
              proxyRes.headers['cache-control'] = 'no-cache'
              proxyRes.headers['x-accel-buffering'] = 'no'
            }
          })
        },
      },
    },
  },
})
