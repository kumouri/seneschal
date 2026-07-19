import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev server proxies /api to the cockpit backend (see ../server/app.py) so there's no CORS story to
// tell — `npm run dev` here + `uv run uvicorn cockpit.server.app:app --port 8760` in another shell.
// `ws: true` extends that proxy to the v2 chat pane's GET /api/ws WebSocket upgrade.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8760',
        changeOrigin: true,
        ws: true,
      },
    },
  },
  build: {
    outDir: 'dist',
  },
})
