import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'

// 前端构建配置（本工程约定）：
// - 直连后端（CORS 已放行 5173 开发来源），不做 dev 代理；
//   后端地址经 VITE_API_BASE_URL 覆盖（默认 http://127.0.0.1:8000）。
// - 路径别名 @ → src/，与 tsconfig paths 保持一致。
export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
  ],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 5173,
  },
})
