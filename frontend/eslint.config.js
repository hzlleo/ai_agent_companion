// ESLint flat config — AI 陪伴伙伴前端
//
// 技术栈：React 19 + Vite 6 + TypeScript 5.6 + Tailwind 4 + React Query 5
//
// 插件面：
//   - @typescript-eslint（parser + strict-type-checked 全 error）
//   - eslint-plugin-react-hooks（rules-of-hooks / exhaustive-deps 均 error）
//   - eslint-plugin-react-refresh（only-export-components warn）
//   - eslint-plugin-jsx-a11y（无障碍 recommended）
//   - eslint-plugin-security（安全 recommended）
//   - eslint-plugin-import（循环依赖检测 + 导入规范）
//
// Lint 命令：`pixi run lint`（= tsc --noEmit && eslint src/）。
// 零容忍基线（与后端基线对齐）：无 any、无 @ts-ignore、无 eslint-disable、
// 无非空断言逃逸；strict-type-checked 规则不得降级为 warn。

import parser from '@typescript-eslint/parser'
import tseslintPlugin from '@typescript-eslint/eslint-plugin'
import reactHooks from 'eslint-plugin-react-hooks'
import { reactRefresh } from 'eslint-plugin-react-refresh'
import jsxA11y from 'eslint-plugin-jsx-a11y'
import securityPlugin from 'eslint-plugin-security'
import importPlugin from 'eslint-plugin-import'

const importRules = {
  // lint 期检测循环依赖。
  'import/no-cycle': [
    'error',
    {
      maxDepth: 10,
      ignoreExternal: true,
    },
  ],
  // 导入排序交由人工约定（import/order 在部分 ESLint 大版本上存在兼容问题）。
  'import/order': 'off',
  // 最后一个 import 后强制空行。
  'import/newline-after-import': 'error',
  // 模块解析由 tsc 负责，此处关闭避免重复报错。
  'import/no-unresolved': 'off',
}

export default [
  // ─── 全局忽略 ────────────────────────────────────────────────────
  {
    ignores: [
      'dist/',
      'node_modules/',
      'coverage/',
      'dist-node/',
      '*.config.ts',
      '*.config.js',
      'eslint.config.js',
    ],
  },

  // ─── TypeScript strict-type-checked（替代 recommended）──────────
  ...tseslintPlugin.configs['flat/strict-type-checked'],

  // ─── 可选插件 recommended 集 ────────────────────────────────────
  jsxA11y.flatConfigs.recommended,
  securityPlugin.configs.recommended,
  importPlugin.flatConfigs.recommended,
  importPlugin.flatConfigs.typescript,

  // ─── TypeScript + React 源文件 ──────────────────────────────────
  {
    files: ['src/**/*.{ts,tsx}'],
    languageOptions: {
      parser,
      ecmaVersion: 2024,
      sourceType: 'module',
      parserOptions: {
        ecmaFeatures: { jsx: true },
        // projectService 为 type-checked 规则提供类型信息。
        projectService: true,
      },
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh.plugin,
    },
    rules: {
      // React Hooks：rules-of-hooks 与 exhaustive-deps 均 error。
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'error',

      // React Refresh：组件与非组件导出混排告警（allowConstantExport
      // 匹配 Vite HMR 行为）。
      'react-refresh/only-export-components': [
        'warn',
        { allowConstantExport: true },
      ],

      // 未使用变量允许下划线前缀约定。
      '@typescript-eslint/no-unused-vars': [
        'error',
        {
          argsIgnorePattern: '^_',
          varsIgnorePattern: '^_',
          caughtErrorsIgnorePattern: '^_',
          ignoreRestSiblings: true,
        },
      ],

      // any 禁用（零容忍）：要求显式 unknown 或正确类型。
      '@typescript-eslint/no-explicit-any': 'error',

      // 非空断言（`!`）禁用：强制显式空值检查。
      '@typescript-eslint/no-non-null-assertion': 'error',

      // strict-type-checked 核心 rules——全部 error，严禁降级。
      '@typescript-eslint/no-confusing-void-expression': [
        'error',
        // 事件回调的箭头缩写（`() => voidFn()`）是 React 惯用形态，
        // 仅放行该缩写；返回值位置的 void 混用仍然报错。
        { ignoreArrowShorthand: true },
      ],
      '@typescript-eslint/no-redundant-type-constituents': 'error',
      '@typescript-eslint/no-unnecessary-type-assertion': 'error',
      '@typescript-eslint/no-floating-promises': 'error',
      '@typescript-eslint/unbound-method': 'error',
      '@typescript-eslint/no-misused-promises': 'error',
      '@typescript-eslint/restrict-template-expressions': [
        'error',
        // 路由参数 / 列表索引为 number 主键——允许 number 插值，
        // 其余类型（对象 / symbol 等）仍然报错。
        { allowNumber: true },
      ],
      '@typescript-eslint/no-unnecessary-condition': 'error',
      '@typescript-eslint/no-deprecated': 'error',
      '@typescript-eslint/require-await': 'error',
      '@typescript-eslint/no-unsafe-assignment': 'error',
      '@typescript-eslint/no-unsafe-member-access': 'error',
      '@typescript-eslint/no-unsafe-argument': 'error',
      '@typescript-eslint/no-base-to-string': 'error',
      '@typescript-eslint/return-await': 'error',
      '@typescript-eslint/only-throw-error': 'error',

      // ─── import：循环依赖检测 + 规范 ────────────────────────────
      ...importRules,
    },
  },

  // ─── 测试文件放宽（防误报）──────────────────────────────────────
  {
    files: [
      'src/**/*.test.{ts,tsx}',
      'src/**/__tests__/**',
      'src/test/**',
    ],
    rules: {
      // 测试装置常渲染不完整子树 / 直接操作 DOM——a11y 误报面关闭。
      'jsx-a11y/no-static-element-interactions': 'off',
      'jsx-a11y/click-events-have-key-events': 'off',
      'jsx-a11y/no-noninteractive-element-interactions': 'off',
      'jsx-a11y/no-autofocus': 'off',
      // 测试代码允许非空断言（简洁优先）。
      '@typescript-eslint/no-non-null-assertion': 'off',
    },
  },

  // ─── 生产源码覆盖：已知误报规则关闭 ─────────────────────────────
  // security/detect-object-injection 对任意 obj[变量键] 访问均报警
  // （文档标注的高误报规则）；属性访问安全由 TypeScript 类型系统保证。
  {
    files: ['src/**/*.{ts,tsx}'],
    rules: {
      'security/detect-object-injection': 'off',
    },
  },
]
