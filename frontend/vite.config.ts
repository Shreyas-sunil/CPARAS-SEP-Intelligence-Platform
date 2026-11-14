import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// CPARAS Platform frontend. /api is proxied to the backend (default
// http://127.0.0.1:8000); override with API_TARGET=http://host:port.
// Bound to 127.0.0.1 (not exposed to the LAN) and strictPort, so a port
// already in use is reported instead of Vite silently picking another port.
const apiTarget = process.env.API_TARGET || 'http://127.0.0.1:8000'
const proxy = { '/api': { target: apiTarget, changeOrigin: true } }

export default defineConfig({
  plugins: [react()],
  server: { host: '127.0.0.1', port: 5173, strictPort: true, proxy },
  preview: { host: '127.0.0.1', port: 4173, strictPort: true, proxy },
})
