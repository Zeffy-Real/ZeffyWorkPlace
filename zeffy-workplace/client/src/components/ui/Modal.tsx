import { useEffect, useRef } from 'react';
import { radius, shadow, space, type as T, colors, t } from '../../theme';
import { Icon } from './Icon';

/* 可访问模态：Esc/遮罩关闭、打开聚焦首交互元素并 trap 焦点、role=dialog。 */
export function Modal({
  open,
  onClose,
  title,
  children,
  footer,
  maxWidth = 480,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: React.ReactNode;
  footer?: React.ReactNode;
  maxWidth?: number;
}) {
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const focusables = () =>
      Array.from(
        boxRef.current?.querySelectorAll<HTMLElement>(
          'button, [href], input, textarea, select, [tabindex]:not([tabindex="-1"])',
        ) ?? [],
      ).filter((el) => !el.hasAttribute('disabled'));
    const first = focusables()[0];
    first?.focus();

    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        onClose();
        return;
      }
      if (e.key !== 'Tab') return;
      const list = focusables();
      if (list.length === 0) return;
      const firstEl = list[0];
      const lastEl = list[list.length - 1];
      if (e.shiftKey && document.activeElement === firstEl) {
        e.preventDefault();
        lastEl.focus();
      } else if (!e.shiftKey && document.activeElement === lastEl) {
        e.preventDefault();
        firstEl.focus();
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      role="presentation"
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, zIndex: 60,
        background: 'rgba(17,24,39,0.4)', display: 'flex',
        alignItems: 'flex-start', justifyContent: 'center', padding: '10vh 16px 32px',
      }}
    >
      <div
        ref={boxRef}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
        style={{
          width: '100%', maxWidth, background: colors.white, borderRadius: radius.modal,
          boxShadow: shadow.modal, padding: space[5], outline: 'none',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: space[2], marginBottom: space[4] }}>
          <h2 style={{ margin: 0, flex: 1, fontSize: T.title, fontWeight: 600, color: colors.ink }}>
            {title}
          </h2>
          <button
            onClick={onClose}
            aria-label="关闭"
            className="zf-btn zf-btn-ghost"
            style={t.btnGhost().style}
          >
            <Icon name="close" size={16} />
          </button>
        </div>
        <div style={{ color: colors.slate, lineHeight: T.lhBody, fontSize: T.body }}>{children}</div>
        {footer && <div style={{ marginTop: space[5], display: 'flex', justifyContent: 'flex-end', gap: space[2] }}>{footer}</div>}
      </div>
    </div>
  );
}