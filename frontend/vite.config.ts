import { defineConfig } from 'vitest/config'
import { loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', '')
  return {
    plugins: [react()],
    test: {
      environment: 'jsdom',
      setupFiles: ['./src/test/setup.ts'],
      restoreMocks: true,
    },
    server: {
      host: '0.0.0.0',
      proxy: {
        '/api': {
          // process.env first: inside a container the target comes from the
          // environment, and loadEnv only reads .env files, so the container
          // fell back to localhost and never reached the backend service.
          target: process.env.VITE_BACKEND_PROXY_TARGET
            || env.VITE_BACKEND_PROXY_TARGET
            || 'http://localhost:8000',
          changeOrigin: true,
          // Narration takes minutes and the package download is one long
          // response; the default timeout would cut both off.
          timeout: 3_600_000,
          proxyTimeout: 3_600_000,
        },
      },
    },
  }
})
