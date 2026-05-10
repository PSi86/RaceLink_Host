import { defineConfig, mergeConfig } from 'vitest/config'
import viteConfig from './vite.config'

// Reuse the production Vite config (alias paths, plugins, etc.) so
// tests resolve modules the same way the bundler does. ``mergeConfig``
// is the documented way to compose Vite + Vitest configs.
export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      globals: true,
      environment: 'jsdom',
      include: ['src/**/*.{test,spec}.ts'],
      // Tests don't need to ship a SPA bundle to disk; cancel the
      // ``build.outDir`` Vite was about to use so a stray test never
      // overwrites racelink/static/dist/.
      clearMocks: true,
      restoreMocks: true,
    },
  }),
)
