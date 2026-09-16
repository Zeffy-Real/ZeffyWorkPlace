import { colors, space } from '../../theme';

/* 骨架屏：加载占位（配合 .zf-skeleton 脉冲动画）。 */
export function Skeleton({ lines = 3, gap = 8 }: { lines?: number; gap?: number }) {
  return (
    <div role="status" aria-label="加载中" style={{ display: 'flex', flexDirection: 'column', gap }}>
      {Array.from({ length: lines }).map((_, i) => (
        <div
          key={i}
          className="zf-skeleton"
          style={{
            height: 14, borderRadius: 6, background: colors.fill,
            width: i === lines - 1 ? '60%' : '100%',
            marginInlineStart: i % 2 === 0 ? undefined : space[3],
          }}
        />
      ))}
    </div>
  );
}