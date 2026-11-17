import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Build-Ausgabe landet direkt in backend/static/ (wird committed – im
// Container wird kein Node installiert, FastAPI serviert die Dateien).
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: '../backend/static',
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8030',
    },
  },
})
