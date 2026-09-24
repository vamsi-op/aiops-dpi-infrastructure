import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The dev server proxies /api and /ws to the gateway, so the browser only ever talks to
// one origin. That keeps CORS out of the picture during development and means the same
// relative URLs work when the built assets are served behind a reverse proxy in Docker.
const GATEWAY = process.env.VITE_GATEWAY_URL ?? 'http://127.0.0.1:8080';

export default defineConfig({
  plugins: [react()],
  server: {
    port: Number(process.env.DASHBOARD_PORT ?? 5173),
    strictPort: false,
    proxy: {
      '/api': { target: GATEWAY, changeOrigin: true },
      '/ws': { target: GATEWAY, ws: true, changeOrigin: true },
      '/health': { target: GATEWAY, changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
    rollupOptions: {
      output: {
        // Charting is the heaviest dependency; splitting it keeps the initial bundle
        // small so the first paint of the health grid is fast.
        manualChunks: {
          charts: ['recharts'],
          react: ['react', 'react-dom'],
        },
      },
    },
  },
});
