import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', '')
  return {
    plugins: [react()],
    server: {
      host: '0.0.0.0',
      proxy: {
        '/api': {
          target: env.VITE_BACKEND_PROXY_TARGET || 'http://localhost:8000',
          changeOrigin: true,
        },
      },
    },
  }
})
