import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  // 生产部署在 Caddy 的 /client/ 子路径下，资源引用必须带 /client/ 前缀，
  // 否则浏览器请求 /assets/xxx 会 404（打到 frontend-studio 的根路径）。
  // 开发环境（npm run dev）不受影响。
  base: '/client/',
  plugins: [react()],
  publicDir: '../frontend-studio/public',
  server: {
    proxy: {
      '/api/': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
