import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    environment: 'node', // 逻辑层冒烟（range/resume-db），无需 DOM
    include: ['src/**/*.test.ts'],
    globals: false,
    testTimeout: 20000,
  },
});