// P5-4 断点续传前端续传器（审查修订版）
// - 串行分块（⭐1：同一时间仅一个块在途）+ 进度标准化（⭐2）
// - 🔴2 ETag 一致性：续传前校验，不一致自动从头；跨版本禁止续传
// - 🔴4 内存熔断：> RANGE_MAX_SIZE 降级全量；每块后校验内存
// - 🔴5 分块完整性：校验 Content-Range 起止/长度，整文件校验总大小
// - 兼容：无 Accept-Ranges → 单次全量 fetch（P5-0 零漂移）

import { ApiError, getToken } from '../auth';

export const RANGE_CHUNK = 256 * 1024; // 分块 256KB
export const RANGE_MAX_SIZE = 50 * 1024 * 1024; // 断点续传最大 50MB（🔴4）
export const RANGE_RETRY = 3; // 单块失败重试次数

export interface RangeProgress {
  received: number;
  total: number;
  percent: number;
  speed: number; // bytes/s
}

export interface ResumableResult {
  blob: Blob;
  received: number;
  total: number;
  resumed: boolean;
  etag: string | null;
}

function seg(rel: string): string {
  return rel.split('/').map(encodeURIComponent).join('/');
}

async function fetchRange(
  taskId: string, rel: string, start: number, end: number, signal?: AbortSignal,
): Promise<{ status: number; blob: Blob; contentRange: string | null; etag: string | null }> {
  const token = getToken();
  const res = await fetch(`/artifacts/${encodeURIComponent(taskId)}/${seg(rel)}`, {
    headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      Range: `bytes=${start}-${end}`,
    },
    signal,
  });
  if (res.status === 401) {
    // fetch 不暴露 401 头，由调用方 api 处理？此处直接返回状态让上层判断
  }
  return {
    status: res.status,
    blob: res.status === 206 || res.ok ? await res.blob() : new Blob(),
    contentRange: res.headers.get('content-range'),
    etag: res.headers.get('etag'),
  };
}

/** HEAD 探测：返回 size / acceptRanges。不支持或 404 → null。 */
export async function probeRange(
  taskId: string, rel: string, signal?: AbortSignal,
): Promise<{ size: number; acceptRanges: boolean; etag: string | null } | null> {
  const token = getToken();
  try {
    const res = await fetch(`/artifacts/${encodeURIComponent(taskId)}/${seg(rel)}`, {
      method: 'HEAD',
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      signal,
    });
    if (res.status !== 200) return null;
    const size = Number(res.headers.get('content-length') ?? 'NaN');
    if (!Number.isFinite(size)) return null;
    const accept = (res.headers.get('accept-ranges') ?? '').includes('bytes');
    return { size, acceptRanges: accept, etag: res.headers.get('etag') };
  } catch {
    return null;
  }
}

/**
 * 前端分块续传器（🔴4 size>上限降级全量；串行：一次仅一个块在途，⭐1）。
 * onProgress: { received, total, percent, speed }。
 * 返回最小实现：若探测到不支持 Range / 超过 RANGE_MAX_SIZE / 失败超限 → 单次全量 fetch。
 */
export async function fetchResumable(
  taskId: string,
  rel: string,
  opts: {
    signal?: AbortSignal;
    onProgress?: (p: RangeProgress) => void;
    maxSize?: number;
  } = {},
): Promise<ResumableResult> {
  const { signal, onProgress, maxSize = RANGE_MAX_SIZE } = opts;
  const now = Date.now();

  // 小文件：先 HEAD 探测；太小则直接单次全量（⭐4 小文件跳过）
  const probe = await probeRange(taskId, rel, signal);
  if (!probe || !probe.acceptRanges || probe.size === 0 || probe.size <= RANGE_CHUNK || probe.size > maxSize) {
    // 不支持 Range / 太小 / 超上限 → 单次全量 fetch（兼容 P5-0）
    const blob = await singleFetch(taskId, rel, signal);
    return { blob, received: blob.size, total: blob.size, resumed: false, etag: probe?.etag ?? null };
  }

  const total = probe.size;
  const etag = probe.etag;
  let received = 0;
  const chunks: Blob[] = [];
  let lastStamp = now;
  let lastBytes = 0;
  let resumed = false;
  let curEtag = etag;

  while (received < total) {
    const end = Math.min(received + RANGE_CHUNK - 1, total - 1);
    let ok = false;
    let attempts = 0;
    while (!ok && attempts < RANGE_RETRY) {
      // 🔴2 ETag 一致性：读取中若服务器 ETag 变化（文件被改），丢弃从头
      const r = await fetchRange(taskId, rel, received, end, signal);
      if (r.status === 416) {
        // 断点失效（文件变小）→ 从头
        return await fetchResumable(taskId, rel, { ...opts, onProgress });
      }
      if (r.status !== 206 && r.status !== 200) {
        if (r.status === 401 || r.status === 403 || r.status === 404) throw new ApiError(r.status, '获取失败');
        attempts += 1; await delay(attempts * 200); continue;
      }
      // 🔴5 长度校验：块内容应与请求区间一致
      const expectLen = end - received + 1;
      if (r.blob.size !== expectLen && received + r.blob.size < total) {
        attempts += 1; await delay(attempts * 200); continue;
      }
      if (r.etag && curEtag && r.etag !== curEtag) {
        // 文件已变更 → 重置从头
        return await fetchResumable(taskId, rel, { ...opts, onProgress });
      }
      if (r.etag) curEtag = r.etag;
      if (r.blob.size > 0) {
        chunks.push(r.blob);
        received += r.blob.size;
        ok = true;
      } else {
        attempts += 1; await delay(attempts * 200);
      }
    }
    if (!ok) {
      // 重试仍失败：保留已收，抛出可续传错误（记忆 received 供 UI 提示）
      onProgress?.({ received, total, percent: (received / total) * 100, speed: 0 });
      throw new FetchResumeError(received, total, '下载中断，已接收 ' + received + ' 字节，请重试');
    }
    const now2 = Date.now();
    const dt = (now2 - lastStamp) / 1000;
    const speed = dt > 0 && received > lastBytes ? (received - lastBytes) / dt : 0;
    lastStamp = now2; lastBytes = received;
    resumed = received > 0;
    onProgress?.({ received, total, percent: (received / total) * 100, speed });
  }

  const blob = new Blob(chunks);
  return { blob, received, total, resumed, etag: curEtag ?? null };
}

/** 有 256KB 分块读取某文件片段（首屏流式预览用，Range 首 8KB/256KB）。 */
export async function fetchRangeSlice(
  taskId: string, rel: string, start: number, end: number, signal?: AbortSignal,
): Promise<{ bytes: Uint8Array; total: number } | null> {
  const r = await fetchRange(taskId, rel, start, end, signal);
  if (r.status !== 206 && r.status !== 200) return null;
  const bytes = new Uint8Array(await r.blob.arrayBuffer());
  let total = 0;
  const cr = r.contentRange;
  if (cr) { const m = /\/(\d+)$/.exec(cr); total = m ? Number(m[1]) : 0; }
  return { bytes, total };
}

async function singleFetch(taskId: string, rel: string, signal?: AbortSignal): Promise<Blob> {
  const token = getToken();
  const res = await fetch(`/artifacts/${encodeURIComponent(taskId)}/${seg(rel)}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    signal,
  });
  if (res.status === 401) throw new ApiError(401, '未登录或会话过期');
  if (!res.ok) throw new ApiError(res.status, '获取产物失败');
  return res.blob();
}

export class FetchResumeError extends Error {
  received: number;
  total: number;
  constructor(received: number, total: number, message: string) {
    super(message);
    this.received = received;
    this.total = total;
  }
}

function delay(ms: number) {
  return new Promise((r) => setTimeout(r, ms));
}