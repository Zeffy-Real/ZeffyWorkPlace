// P5-2 产物在线预览 modal（审查修订版）
// - 🔴3 资源安全：AbortController 可取消 + 单例（父级保证）+ 卸载 cleanup + 行数/大小双阈值
// - 🔴1/🔴2：打开前先 previewDecisionAsync 三重校验；Markdown 安全渲染
// - ⭐1 异常分层：401→登出 / 404→不存在 / 网络→重试 / 编码→不支持 / 超限→下载
// - ⭐6 无障碍：role=dialog + aria-modal + Esc + 自动聚焦 + 焦点回弹

import { useEffect, useRef, useState } from 'react';
import { ApiError, api } from '../auth';
import {
  PREVIEW_FETCH_TIMEOUT,
  decodeText,
  markdownToReact,
  previewDecisionAsync,
  truncateLines,
  type PreviewKind,
} from '../lib/preview';

export function ArtifactPreview({
  taskId,
  rel,
  onClose,
  onAuthLost,
}: {
  taskId: string;
  rel: string;
  onClose: () => void;
  onAuthLost: () => void;
}) {
  const [url, setUrl] = useState<string | null>(null);
  const [kind, setKind] = useState<PreviewKind>('image');
  const [text, setText] = useState<string | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [loading, setLoading] = useState(true);
  // 分层错误态（⭐1）
  const [error, setError] = useState<{ title: string; detail: string; canDownload: boolean } | null>(null);
  const [pdfFailed, setPdfFailed] = useState(false);

  const urlRef = useRef<string | null>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLElement | null>(null);

  const revoke = () => {
    if (urlRef.current) {
      URL.revokeObjectURL(urlRef.current);
      urlRef.current = null;
    }
  };

  useEffect(() => {
    triggerRef.current = document.activeElement as HTMLElement | null;
    const ctrl = new AbortController();
    let active = true; // ✓ 隔离 StrictMode 双 effect：过期 effect 的回调一律忽略
    const timer = window.setTimeout(() => ctrl.abort(), PREVIEW_FETCH_TIMEOUT);

    if (active) { setError(null); setText(null); setLoading(true); }

    (async () => {
      try {
        const blob = await api.artifactBlob(taskId, rel, ctrl.signal);
        if (!active) return;
        // 🔴1 三重校验：不符 → 降级下载
        const dec = await previewDecisionAsync(rel, blob);
        if (!dec.ok) {
          setError({ title: '无法预览', detail: dec.reason ?? '文件类型不支持', canDownload: true });
          setLoading(false);
          return;
        }
        setKind(dec.kind);

        if (dec.kind === 'image' || dec.kind === 'pdf') {
          const obj = URL.createObjectURL(blob);
          urlRef.current = obj;
          setUrl(obj);
        } else {
          // 文本 / markdown：编码检测解码 + 行数截断
          const r = await decodeText(blob);
          if (!active) return;
          if ('error' in r) {
            setError({ title: '无法预览', detail: r.error, canDownload: true });
          } else {
            const tr = truncateLines(r.text);
            setText(tr.text);
            setTruncated(tr.truncated); // 需在 setError(null) 之前还是之后无碍
          }
        }
      } catch (err) {
        if (!active) return; // 过期 effect（StrictMode 首轮 abort 等）不写入状态
        if (ctrl.signal.aborted) {
          setError({ title: '加载超时', detail: '拉取产物超过 15 秒，请重试或下载', canDownload: true });
        } else if (err instanceof ApiError) {
          if (err.status === 401) { onAuthLost(); return; }
          const title = err.status === 404 ? '文件不存在' : '无权限或文件不存在';
          setError({ title, detail: `HTTP ${err.status}`, canDownload: true });
        } else {
          setError({ title: '网络异常', detail: '请检查网络后重试', canDownload: true });
        }
      } finally {
        if (active) setLoading(false);
        window.clearTimeout(timer);
      }
    })();

    // 🔴3 卸载兜底：abort + revoke
    return () => {
      active = false;
      ctrl.abort();
      window.clearTimeout(timer);
      revoke();
    };
  }, [taskId, rel, onAuthLost]);

  // ⭐6 焦点管理
  useEffect(() => {
    if (!loading && panelRef.current && error === null) panelRef.current.focus();
  }, [loading, error]);

  const close = () => {
    revoke();
    onClose();
    triggerRef.current?.focus?.();
  };

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') close();
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={`预览 ${rel}`}
      onKeyDown={onKey}
      style={{
        position: 'fixed', inset: 0, zIndex: 1000, background: 'rgba(15,23,42,0.55)',
        display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 24,
      }}
      onClick={(e) => { if (e.target === e.currentTarget) close(); }}
    >
      <div
        tabIndex={-1}
        ref={panelRef}
        style={{
          width: 'min(860px, 92vw)', maxHeight: '86vh', display: 'flex', flexDirection: 'column',
          background: '#fff', borderRadius: 10, border: '1px solid #e5e7eb', boxShadow: '0 10px 40px rgba(0,0,0,0.2)',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '10px 14px', borderBottom: '1px solid #e5e7eb' }}>
          <span style={{ flex: 1, fontWeight: 600, fontSize: 14, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{rel}</span>
          <button onClick={close} aria-label="关闭预览" style={btn}>关闭</button>
        </div>

        <div style={{ flex: 1, overflow: 'auto', padding: 16, minHeight: 120, maxHeight: 'calc(86vh - 52px)' }}>
          {loading && <div style={{ color: '#6b7280', fontSize: 13 }}>加载中…</div>}

          {!loading && error && (
            <div style={{ textAlign: 'center', padding: 40 }}>
              <div style={{ fontWeight: 600, marginBottom: 6 }}>{error.title}</div>
              <div style={{ fontSize: 13, color: '#6b7280', marginBottom: 14 }}>{error.detail}</div>
              <button onClick={() => void download()} style={btn}>下载文件</button>
            </div>
          )}

          {!loading && !error && kind === 'image' && url && (
            <div style={{ textAlign: 'center' }}>
              <img
                src={url}
                alt={rel}
                style={{ maxWidth: '100%', maxHeight: '62vh', objectFit: 'contain' }}
              />
              <div style={{ fontSize: 12, color: '#6b7280', marginTop: 8 }}>SVG/图片为静态渲染，不支持脚本与交互</div>
            </div>
          )}

          {!loading && !error && kind === 'pdf' && url && (
            <>
              {!pdfFailed ? (
                <iframe
                  title={rel}
                  src={url}
                  onError={() => setPdfFailed(true)}
                  style={{ width: '100%', height: '62vh', border: '1px solid #e5e7eb', borderRadius: 6 }}
                />
              ) : (
                <div style={{ textAlign: 'center', padding: 40 }}>
                  <div style={{ fontWeight: 600, marginBottom: 6 }}>浏览器无法内嵌 PDF</div>
                  <div style={{ fontSize: 13, color: '#6b7280', marginBottom: 14 }}>请下载后查看</div>
                  <button onClick={() => void download()} style={btn}>下载文件</button>
                </div>
              )}
            </>
          )}

          {!loading && !error && (kind === 'text' || kind === 'markdown') && text !== null && (
            <div>
              {kind === 'markdown' ? (
                <div style={{ fontSize: 14, lineHeight: 1.7, color: '#111827' }}>
                  {markdownToReact(text)}
                </div>
              ) : (
                <pre style={{ fontSize: 13, lineHeight: 1.5, whiteSpace: 'pre-wrap', wordBreak: 'break-word', margin: 0 }}>
                  {text}
                </pre>
              )}
              {truncated && <div style={{ fontSize: 12, color: '#d97706', marginTop: 8 }}>文件过大，仅显示前 2000 行</div>}
            </div>
          )}
        </div>
      </div>
    </div>
  );

  async function download() {
    try {
      const blob = await api.artifactBlob(taskId, rel);
      const obj = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = obj;
      a.download = rel.split('/').pop() || 'artifact';
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(obj);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) { onAuthLost(); return; }
    }
  }
}

const btn: React.CSSProperties = {
  padding: '6px 12px', borderRadius: 6, border: '1px solid #d1d5db',
  background: '#fff', color: '#374151', cursor: 'pointer', fontSize: 13,
};