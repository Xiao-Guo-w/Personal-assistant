import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig, loadEnv } from 'vite'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), 'VITE_')
  const backendOrigin = env.VITE_BACKEND_ORIGIN || 'http://localhost:8000'

  // 开发/预览服务器把 /api 转发到 FastAPI，浏览器侧不产生跨域问题
  const proxy = {
    '/api': { target: backendOrigin, changeOrigin: true },
    '/health': { target: backendOrigin, changeOrigin: true },
  }

  return {
    plugins: [vue()],
    resolve: {
      alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
    },
    server: { port: 5173, proxy },
    preview: { port: 4173, proxy },
    build: { outDir: 'dist', sourcemap: false },
  }
})
