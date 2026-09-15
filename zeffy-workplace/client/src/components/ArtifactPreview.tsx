// P5-2 产物在线预览 modal（审查修订版）
// - 🔴3 资源安全：AbortController 可取消 + 单例（父级保证）+ 卸载 cleanup + 行数/大小双阈值
// - 🔴1/🔴2：打开前先 previewDecisionAsync 三重校验；Markdown 安全渲染
// - ⭐1 异常分层：401→登出 / 404→不存在 / 网络→重试 / 编码→不支持 / 超限→下载
// - ⭐6 无障碍：role=dialog + aria-modal + Esc + 自动聚焦 + 焦点回弹

import { useEffect, useRef, useState } from 'react';
import { ApiError, api } from '../auth';
import { downloadArtifact, fetchRangeSlice, probeRange } from '../lib/range';
import {
  PREVIEW_BINARY_LIMIT,
  PREVIEW_FETCH_TIMEOUT,
  PREVIEW_MAX_PREVIEW,
  decodeText,
  extensionKind,
  isTextMime,
  markdownToReact,
  previewDecisionAsync,
  sniffEncoding,
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
  // 🔎 流式首屏：大文本仅拉头部 Range，此标记用于区分「全局行截断」与「仅首屏」
  const [headOnly, setHeadOnly] = useState(false);
  // 审查 2.2 分页加载：续拉偏移 / 分页信息 / 加载中 / 文件变更 / 行数截断
  const [offset, setOffset] = useState(0);
  const [pager, setPager] = useState<{ etag: string; total: number } | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [fileChanged, setFileChanged] = useState(false);
  const decoderRef = useRef<TextDecoder | null>(null);
  const truncByLinesRef = useRef(false);

  // 🔴3 内存：objectURL 注册表（多槽），任何 create 都登记，卸载/关闭/异常统一 revoke
  const urlsRef = useRef<Set<string>>(new Set());
  const panelRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLElement | null>(null);

  const revokeAll = () => {
    for (const u of urlsRef.current) URL.revokeObjectURL(u);
    urlsRef.current.clear();
  };

  const trackUrl = (url: string) => {
    urlsRef.current.add(url);
    return url;
  };

  useEffect(() => {
    triggerRef.current = document.activeElement as HTMLElement | null;
    const ctrl = new AbortController();
    let active = true; // ✓ 隔离 StrictMode 双 effect：过期 effect 的回调一律忽略
    const timer = window.setTimeout(() => ctrl.abort(), PREVIEW_FETCH_TIMEOUT);

    if (active) { setError(null); setText(null); setLoading(true); setHeadOnly(false); setFileChanged(false); setPager(null); setOffset(0); decoderRef.current = null; truncByLinesRef.current = false; revokeAll(); }

    // 统一失败出口：写入错误态并结束加载
    const fail = (e: { title: string; detail: string; canDownload: boolean }) => {
      if (active) { setError(e); setLoading(false); }
    };

    (async () => {
      try {
        // ① 探测：HEAD 拿 contentType/size，不下载全量即先决策（流式首屏前置）
        const probe = await probeRange(taskId, rel, ctrl.signal);
        if (probe) {
          const kind = extensionKind(rel);
          const isTextKind = kind === 'text' || kind === 'markdown';
          // 图片/PDF 超二进制上限 → 直接拒绝，避免白下 8MB+ 再判
          if ((kind === 'image' || kind === 'pdf') && probe.size > PREVIEW_BINARY_LIMIT) {
            fail({ title: '无法预览', detail: '文件过大，请下载', canDownload: true });
            return;
          }
          // ② 大文本/大 Markdown → 流式首屏：只拉头部 ≤ PREVIEW_MAX_PREVIEW 的 Range
          //    （绕过 previewDecisionAsync 的 512KB 全量拒绝，读开头即可预览；用流式 TextDecoder 兜多字节边界）
          if (isTextKind && probe.size > PREVIEW_MAX_PREVIEW && isTextMime(probe.contentType)) {
            const slice = await fetchRangeSlice(
              taskId, rel, 0, Math.min(PREVIEW_MAX_PREVIEW - 1, probe.size - 1), ctrl.signal,
            );
            if (!active) return;
            if (!slice) throw new ApiError(0, '读取首屏失败');
            // 首屏字节做编码检测（对齐全量路径 🔴1，不信任扩展名/MIME 单点）
            const enc = sniffEncoding(slice.bytes);
            if (enc === 'unsupported') { fail({ title: '无法预览', detail: '编码不支持预览', canDownload: true }); return; }
            setKind(kind === 'markdown' ? 'markdown' : 'text');
            decoderRef.current = new TextDecoder(enc); // 跨页多字节安全（decode 传 {stream:true}）
            const tr = truncateLines(decoderRef.current.decode(slice.bytes, { stream: true }));
            truncByLinesRef.current = tr.truncated;
            setText(tr.text);
            setHeadOnly(true);
            setTruncated(true);
            setOffset(slice.bytes.length);
            setPager({ etag: slice.etag ?? probe.etag ?? '', total: probe.size });
            return;
          }
        }

        // ③ 小文本 / 图片 / PDF / 探测失败 → 全量路径（完整三重校验 previewDecisionAsync）
        if (!active) return;
        const blob = await api.artifactBlob(taskId, rel, ctrl.signal);
        if (!active) return;
        const dec = await previewDecisionAsync(rel, blob);
        if (!dec.ok) {
          fail({ title: '无法预览', detail: dec.reason ?? '文件类型不支持', canDownload: true });
          return;
        }
        setKind(dec.kind);

        if (dec.kind === 'image' || dec.kind === 'pdf') {
          // 生成前先清旧（防 StrictMode/重载堆积多个 blob: URL）
          revokeAll();
          const obj = trackUrl(URL.createObjectURL(blob));
          setUrl(obj);
        } else {
          // 文本 / markdown：编码检测解码 + 行数截断
          const r = await decodeText(blob);
          if (!active) return;
          if ('error' in r) {
            fail({ title: '无法预览', detail: r.error, canDownload: true });
          } else {
            const tr = truncateLines(r.text);
            setText(tr.text);
            setTruncated(tr.truncated);
          }
        }
      } catch (err) {
        if (!active) return; // 过期 effect（StrictMode 首轮 abort 等）不写入状态
        if (ctrl.signal.aborted) {
          fail({ title: '加载超时', detail: '拉取产物超过 15 秒，请重试或下载', canDownload: true });
        } else if (err instanceof ApiError) {
          if (err.status === 401) { onAuthLost(); revokeAll(); return; }
          const title = err.status === 404 ? '文件不存在' : '无权限或文件不存在';
          fail({ title, detail: `HTTP ${err.status}`, canDownload: true });
        } else {
          fail({ title: '网络异常', detail: '请检查网络后重试', canDownload: true });
        }
      } finally {
        if (active) setLoading(false);
        window.clearTimeout(timer);
      }
    })();

    // 🔴3 卸载兜底：abort + revoke 全部 objectURL
    return () => {
      active = false;
      ctrl.abort();
      window.clearTimeout(timer);
      revokeAll();
    };
  }, [taskId, rel, onAuthLost]);

  // ⭐6 焦点管理
  useEffect(() => {
    if (!loading && panelRef.current && error === null) panelRef.current.focus();
  }, [loading, error]);

  const close = () => {
    revokeAll();
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
          background: '#fff', borderRadius: 12, boxShadow: '0 8px 32px rgba(0,0,0,0.08)',
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
              {truncated && (
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: '#d97706', marginTop: 8 }}>
                  <span>{headOnly ? '文件较大，已预览开头部分' : '文件过大，仅显示前 2000 行'}</span>
                  <button onClick={() => void download()} style={{ ...btn, padding: '3px 10px', fontSize: 12 }}>下载</button>
                </div>
              )}
              {(pager || fileChanged) && (kind === 'text' || kind === 'markdown') && (
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: '#6b7280', marginTop: 8 }}>
                  {fileChanged ? (
                    <span style={{ color: '#d97706' }}>文件已变更，请重新打开预览</span>
                  ) : truncByLinesRef.current ? (
                    <span style={{ color: '#d97706' }}>已达最大行数，完整内容请下载</span>
                  ) : offset >= (pager?.total ?? 0) ? (
                    <span>已显示全部内容</span>
                  ) : (
                    <button onClick={() => void loadMore()} disabled={loadingMore} style={btn}>
                      {loadingMore ? '加载中…' : '加载更多'}
                    </button>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );

  // 审查 2.2 分页加载：继续拉取下一页（流式 TextDecoder 保证跨页多字节安全；行数/ETag 双重护栏）
  const loadMore = async () => {
    if (loadingMore || !pager || fileChanged) return;
    if (!pager.etag) return;
    setLoadingMore(true);
    try {
      const next = await fetchRangeSlice(taskId, rel, offset, offset + PREVIEW_MAX_PREVIEW - 1);
      if (!next) return;
      // ETag 变更 → 停止追加（审查 2.2🔴3）
      if (next.etag && next.etag !== pager.etag) { setFileChanged(true); return; }
      const piece = decoderRef.current ? decoderRef.current.decode(next.bytes, { stream: true }) : '';
      const nb = offset + next.bytes.length;
      setOffset(nb);
      const flush = nb >= pager.total;
      setText((prev) => {
        const merged = (prev ?? '') + piece + (flush ? (decoderRef.current?.decode() ?? '') : '');
        const tr = truncateLines(merged);
        if (tr.truncated) truncByLinesRef.current = true;
        return tr.text;
      });
    } catch { /* 网络抖动保留已加载内容 */ }
    finally { setLoadingMore(false); }
  };

  async function download() {
    try {
      // P5-4 续传下载：大文件分块续传 + 进度；小文件/无 Range 自动降级单次 fetch
      const blob = await downloadArtifact(taskId, rel);
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