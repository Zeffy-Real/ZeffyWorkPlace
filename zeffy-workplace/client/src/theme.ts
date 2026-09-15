/** P6-4-C 前端 UI 令牌（两层）：
 *  - 基础令牌层：纯变量（色/圆角/间距/字体），不产样式
 *  - 组件工厂层 t.*：输出 { style, className }，·className 指向 tokens.css 的 .zf-* 类（含全状态）
 */
import type { CSSProperties } from 'react';

// ---- 基础令牌 ----
export const colors = {
  // 冷调灰 6 档（深→浅）
  ink: '#111827',
  slate: '#374151',
  gray: '#6b7280', // 辅助正文 on 白 ≥4.5:1
  mist: '#9ca3af', // 弱提示（大文本/图标 on 白 ≥3:1）
  line: '#e5e7eb', // 边框
  fill: '#f0f2f5', // 浅分隔/点状底
  bg: '#f9fafb', // 页面浅底
  white: '#ffffff',
  hoverBg: '#f3f4f6', // hover 背景（列表项/幽灵按钮）
  focusRing: '#2563eb', // focus 描边
  // 强调（单强调色）
  accent: '#2563eb',
  accent10: '#2563eb1a',
  accent20: '#2563eb33',
  accent50: '#2563eb80',
  // 语义（低饱和）
  ok: '#15803d',
  okLine: '#22c55e',
  danger: '#b91c1c',
  dangerLine: '#f87171',
  info: '#2563eb',
} as const;

export const radius = { tag: 4, control: 6, card: 8, modal: 12 } as const;
export const space = { 1: 4, 2: 8, 3: 12, 4: 16, 6: 24 } as const;
export const type = {
  title: 20, body: 15, helper: 13, hint: 12,
  weightTitle: 600, weightStrong: 500, weightBody: 400,
  lhBody: 1.55, lhTitle: 1.3, lhHelper: 1.4,
} as const;

export const border = (c: string = colors.line) => `1px solid ${c}`;

// ---- 组件工厂（style + .zf-* className） ----
export const t = {
  page: (): { style: CSSProperties; className: string } => ({
    style: { maxWidth: 720, margin: '0 auto', padding: space[6] },
    className: 'zf-page',
  }),
  card: (): { style: CSSProperties; className: string } => ({
    style: { border: border(), borderRadius: radius.card, padding: space[3], background: colors.white },
    className: 'zf-card',
  }),
  input: (): { style: CSSProperties; className: string } => ({
    style: {
      padding: space[2], borderRadius: radius.control, border: border('#d1d5db'),
      fontSize: type.body, color: colors.ink, boxSizing: 'border-box', fontFamily: 'inherit',
    },
    className: 'zf-input',
  }),
  btnPrimary: (): { style: CSSProperties; className: string } => ({
    style: {
      padding: `${space[2]}px ${space[4]}px`, borderRadius: radius.control, border: 'none',
      background: colors.accent, color: colors.white, cursor: 'pointer', fontSize: type.body,
      fontFamily: 'inherit',
    },
    className: 'zf-btn zf-btn-primary',
  }),
  btnGhost: (): { style: CSSProperties; className: string } => ({
    style: {
      padding: `${space[1]}px ${space[3]}px`, borderRadius: radius.control, border: border('#d1d5db'),
      background: colors.white, color: colors.slate, cursor: 'pointer', fontSize: type.body,
      fontFamily: 'inherit',
    },
    className: 'zf-btn zf-btn-ghost',
  }),
  muted: (): { style: CSSProperties; className: string } => ({
    style: { color: colors.gray, fontSize: type.body, lineHeight: type.lhBody },
    className: '',
  }),
  tag: (): { style: CSSProperties; className: string } => ({
    style: { fontSize: type.helper, padding: '2px 8px', borderRadius: radius.tag, whiteSpace: 'nowrap' },
    className: 'zf-tag',
  }),
  section: (): { style: CSSProperties; className: string } => ({
    style: { border: border(), borderRadius: radius.card, padding: `${space[3]}px ${space[4]}px`, marginBottom: space[4], background: colors.white },
    className: 'zf-card',
  }),
  alertError: (): { style: CSSProperties; className: string } => ({
    style: { color: colors.danger, fontSize: type.body, lineHeight: type.lhBody },
    className: '',
  }),
} as const;