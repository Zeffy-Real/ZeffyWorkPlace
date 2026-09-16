import type { CSSProperties } from 'react';

/* 统一内联 SVG 图标集（stroke=currentColor，可继承色；禁 emoji 当图标）。
 * 仅装饰性使用：业务可交互处图标应在外层加 aria-label 或由文字承载语义。 */

export type IconName =
  | 'plus' | 'send' | 'back' | 'close' | 'check' | 'chevronRight'
  | 'help' | 'logout' | 'download' | 'upload' | 'spinner'
  | 'info' | 'warning' | 'folder' | 'task' | 'spark' | 'dot';

const PATHS: Record<IconName, JSX.Element> = {
  plus: <path d="M12 5v14M5 12h14" />,
  send: <path d="M22 2 11 13M22 2l-7 20-4-9-9-4 20-7Z" />,
  back: <path d="M19 12H5m6-6-6 6 6 6" />,
  close: <path d="M18 6 6 18M6 6l12 12" />,
  check: <path d="M20 6 9 17l-5-5" strokeLinecap="round" strokeLinejoin="round" />,
  chevronRight: <path d="m9 18 6-6-6-6" />,
  help: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M9.5 9a2.5 2.5 0 0 1 4.9.7c0 1.6-2.4 2.1-2.4 3.3M12 17h.01" strokeLinecap="round" />
    </>
  ),
  logout: <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4m7 14 5-5-5-5m5 5H9" strokeLinecap="round" strokeLinejoin="round" />,
  download: <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4m4-5 5 5 5-5m-5 5V3" strokeLinecap="round" strokeLinejoin="round" />,
  upload: <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4m4-5 5-5 5 5m-5-5v14" strokeLinecap="round" strokeLinejoin="round" />,
  spinner: <path d="M21 12a9 9 0 1 1-6.2-8.6" strokeLinecap="round" />,
  info: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 16v-5M12 8h.01" strokeLinecap="round" />
    </>
  ),
  warning: (
    <>
      <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" />
      <path d="M12 9v4m0 4h.01" strokeLinecap="round" />
    </>
  ),
  folder: <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z" strokeLinejoin="round" />,
  task: (
    <>
      <rect x="4" y="4" width="16" height="16" rx="2" />
      <path d="M8 8h8M8 12h6M8 16h4" strokeLinecap="round" />
    </>
  ),
  spark: <path d="M12 2 13.6 9.2 20 3.4 15.4 11 22 13.6 14.8 15 20.6 21 13 16.6 11.4 24 10.6 15.4 4 20.4 8.4 12.8 2 10.2 9.6 8.4 3.9 2.6 10.8 8" />,
  dot: <circle cx="12" cy="12" r="5" fill="currentColor" stroke="none" />,
};

export function Icon({
  name,
  size = 18,
  className,
  style,
}: {
  name: IconName;
  size?: number;
  className?: string;
  style?: CSSProperties;
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      className={className}
      style={style}
    >
      {PATHS[name]}
    </svg>
  );
}