import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // HTTP API 代理到后端（覆盖前端实际调用的全部前缀，避免 vite 把 SPA 当 200 返回）
      '/api': { target: 'http://localhost:8787', changeOrigin: true },
      '/tasks': { target: 'http://localhost:8787', changeOrigin: true },
      '/auth': { target: 'http://localhost:8787', changeOrigin: true },
      '/metrics': { target: 'http://localhost:8787', changeOrigin: true },
      '/billing': { target: 'http://localhost:8787', changeOrigin: true },
      '/admin': { target: 'http://localhost:8787', changeOrigin: true },
      '/storage': { target: 'http://localhost:8787', changeOrigin: true },
      '/plugins': { target: 'http://localhost:8787', changeOrigin: true },
      '/health': { target: 'http://localhost:8787', changeOrigin: true },
      // P5 产物读取（鉴权同 API）
      '/artifacts': {
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