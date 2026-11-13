import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import path from 'path'

// 测试配置（本工程约定）：
// - jsdom 环境 + RTL；API 全量经 msw 拦截，测试永不触达真实后端；
// - 覆盖率门禁：src/ 全模块 perFile ≥80%（防止单个 0 覆盖文件被
//   同模块高覆盖文件平均掉）。
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    exclude: ['**/node_modules/**', '**/dist/**'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'text-summary', 'lcov'],
      include: ['src/**/*.{ts,tsx}'],
      exclude: [
        'src/test/**',
        'src/main.tsx',
        'src/vite-env.d.ts',
        'src/**/*.test.{ts,tsx}',
        'src/**/__tests__/**',
      ],
      thresholds: {
        perFile: true,
        lines: 80,
        functions: 80,
        branches: 80,
        statements: 80,
      },
    },
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
})
