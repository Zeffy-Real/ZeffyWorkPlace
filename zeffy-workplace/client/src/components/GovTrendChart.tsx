import { useMemo, useState } from 'react';
import { colors as C } from '../theme';

/* P6-6-2：语义色收敛于 theme */
const P = { accent: C.accent, accent10: C.accent10, mist: C.mist, slate: C.slate };

export interface TrendPoint {
  ts: number;
  used: number;
}

interface Props {
  /** [[ts_ms, used],...]；空/单点由组件守卫。 */
  points: Array<[number, number]>;
  unit?: (b: number) => string;
}

/** 手写 SVG 迷你折线（P6-4-A）：≥2 点才渲染折线，hover 显示数值；坐标轴自适应加 padding。 */
export function GovTrendChart({ points, unit }: Props) {
  const [hover, setHover] = useState<number | null>(null);
  const W = 280;
  const H = 64;
  const PAD = 4;

  const model = useMemo(() => {
    if (!points || points.length < 1) return null;
    const used = points.map((p) => p[1]);
    const minV = Math.min(...used);
    const maxV = Math.max(...used);
    const range = maxV - minV || 1; // 全 0 / 单值 → 1，避免零高
    return { origins: points, minV, maxV, range };
  }, [points]);

  if (!model) {
    return (
      <div style={{ color: P.mist, fontSize: 12, padding: '8px 0' }}>
        {points && points.length === 1 ? '单点数据（仅一条采样）' : '暂无采样'}
      </div>
    );
  }

  const x = (i: number) => {
    const n = model.origins.length;
    return n <= 1 ? PAD : PAD + (i / (n - 1)) * (W - PAD * 2);
  };
  const y = (v: number) => H - PAD - ((v - model.minV) / model.range) * (H - PAD * 2);
  const line = model.origins.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p[1]).toFixed(1)}`).join(' ');
  const area = `${line} L${x(model.origins.length - 1).toFixed(1)},${(H - PAD).toFixed(1)} L${PAD},${(H - PAD).toFixed(1)} Z`;
  const hv = hover !== null ? model.origins[hover] : null;

  return (
    <div>
      <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-label="配额趋势">
        <path d={area} fill="#2563eb1a" />
        <path
          d={line}
          fill="none"
          stroke="#2563eb"
          strokeWidth={2}
          strokeLinejoin="round"
          style={{ pointerEvents: 'none' }}
        />
        {model.origins.map((p, i) => (
          <circle
            key={i}
            cx={x(i)}
            cy={y(p[1])}
            r={hover === i ? 4 : 3}
            fill={hover === i ? P.accent : P.mist}
            onMouseEnter={() => setHover(i)}
            onMouseLeave={() => setHover(null)}
            style={{ cursor: 'crosshair', transition: 'fill 0.16s ease-out' }}
          />
        ))}
        {hv && (
          <text x={Math.min(x(model.origins.indexOf(hv)) , W - 80)} y={10} fill="#374151" fontSize={10}>
            {(unit ? unit(hv[1]) : String(hv[1]))} · {new Date(hv[0]).toLocaleDateString()}
          </text>
        )}
      </svg>
    </div>
  );
}
