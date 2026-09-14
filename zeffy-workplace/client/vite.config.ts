import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // HTTP API 代理到后端
      '/api': {
        target: 'http://localhost:8787',
        changeOrigin: true,
      },
      // WebSocket 代理到后端（注意：ws:true 开关，遗漏会导致 WS 联调不通）
      '/ws': {
        target: 'ws://localhost:8787',
        ws: true,
        changeOrigin: true,
      },
    },
  },
});