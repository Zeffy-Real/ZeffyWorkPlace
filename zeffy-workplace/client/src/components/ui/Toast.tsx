import { useEffect, useState } from 'react';
import { colors, radius, shadow, space } from '../../theme';
import { Icon } from './Icon';

/* 极简 Toast：模块级 pub/sub，任意组件 `toast('已提交', 'ok')` 即弹；ToastRegion 挂 App。
 * 含 aria-live（polite）播报，读屏可感知。*/

type Tone = 'ok' | 'error' | 'info';
interface Item {
  id: number;
  text: string;
  tone: Tone;
}

const listeners = new Set<(l: Item[]) => void>();
let items: Item[] = [];
let seq = 0;

export function toast(text: string, tone: Tone = 'info', duration = 4200): void {
  const id = ++seq;
  items = [...items, { id, text, tone }];
  listeners.forEach((fn) => fn(items));
  setTimeout(() => {
    items = items.filter((i) => i.id !== id);
    listeners.forEach((fn) => fn(items));
  }, duration);
}

export function ToastRegion() {
  const [list, setList] = useState<Item[]>([]);
  useEffect(() => {
    const sub = (l: Item[]) => setList(l);
    listeners.add(sub);
    return () => {
      listeners.delete(sub);
    };
  }, []);

  const toneColor: Record<Tone, string> = {
    ok: colors.ok,
    error: colors.danger,
    info: colors.slate,
  };
  const toneIcon: Record<Tone, 'check' | 'warning' | 'info'> = {
    ok: 'check',
    error: 'warning',
    info: 'info',
  };

  return (
    <div className="zf-toast-region" aria-live="polite" aria-atomic="false">
      {list.map((it) => (
        <div
          key={it.id}
          role="status"
          style={{
            display: 'flex', alignItems: 'center', gap: space[2], padding: `${space[2]}px ${space[4]}px`,
            background: colors.white, border: `1px solid ${colors.line}`, borderRadius: radius.control,
            boxShadow: shadow.overlay, color: colors.slate, fontSize: 13, pointerEvents: 'none',
            maxWidth: 360,
          }}
        >
          <span style={{ color: toneColor[it.tone], display: 'inline-flex' }} aria-hidden="true">
            <Icon name={toneIcon[it.tone]} size={16} />
          </span>
          {it.text}
        </div>
      ))}
    </div>
  );
}