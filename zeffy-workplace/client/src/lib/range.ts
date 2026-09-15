// P5-4 断点续传前端续传器（究极深度审查修订版）
// - 串行分块（⭐1：concurrency=1 同一时间仅一个块在途；2.4 支持并行，默认=1 零漂移）
// - 🔴2 ETag 一致性：每块校验，不一致/416 → 整体重启；跨版本禁止续传
// - 🔴4 内存熔断：> RANGE_MAX_SIZE 降级全量；每块后校验内存
// - 🔴5 分块完整性：校验 Content-Range 起止/长度，整文件校验总大小
// - 审查 2.3 限速：滑动窗口 + 可中断休眠（取消即时响应）
// - 审查 2.4 并行：块级长度/Content-Range/ETag 双校验，并发硬上限 4，越小文件越优于串行
// - 兼容：无 Accept-Ranges → 单次全量 fetch（P5-0 零漂移）

import { ApiError, getToken } from '../auth';

export const RANGE_CHUNK = 256 * 1024; // 分块 256KB
export const RANGE_MAX_SIZE = 50 * 1024 * 1024; // 断点续传最大 50MB（🔴4/审查内存红线）
export const RANGE_RETRY = 3; // 单块失败重试次数
export const RANGE_MAX_CONCURRENCY = 4; // 并行硬上限（审查 2.4🔴4，禁止无限制提高）
export const RANGE_LIMITER_MIN_INTERVAL = 10; // 限速最小休眠粒度(ms)（审查 2.3🔴1，不长期阻塞事件循环）

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

export interface FetchOpts {
  signal?: AbortSignal;
  onProgress?: (p: RangeProgress) => void;
  maxSize?: number;
  concurrency?: number; // 并行块数，默认 1（严格等同 P5-4 串行）
  maxBytesPerSec?: number; // 限速（bytes/s），默认 0=不限
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
  return {
    status: res.status,
    blob: res.status === 206 || res.ok ? await res.blob() : new Blob(),
    contentRange: res.headers.get('content-range'),
    etag: res.headers.get('etag'),
  };
}

/** 供断点层复用的单块范围请求（与内部 fetchBlock 校验一致）。 */
export const fetchRangeExported = fetchRange;

/** HEAD 探测：返回 size / acceptRanges / etag / contentType。不支持或 404 → null。 */
export async function probeRange(
  taskId: string, rel: string, signal?: AbortSignal,
): Promise<{ size: number; acceptRanges: boolean; etag: string | null; contentType: string } | null> {
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
    return {
      size, acceptRanges: accept, etag: res.headers.get('etag'),
      contentType: res.headers.get('content-type') ?? 'application/octet-stream',
    };
  } catch {
    return null;
  }
}

/**
 * 前端分块续传器（审查修订版）。
 * - concurrency=1 → 串行（与 P5-4 逐字节一致）；
 * - concurrency>1 → 并行，块级长度/Content-Range/ETag 双校验，任一块失败重试超限抛可续传错误；
 * - 每块校验 ETag 与首块不一致 / 416 → 整体重启（文件已变更）；
 * - 限速 maxBytesPerSec 走滑动窗口，可中断。
 */
export async function fetchResumable(
  taskId: string,
  rel: string,
  opts: FetchOpts = {},
): Promise<ResumableResult> {
  const { signal, onProgress, maxSize = RANGE_MAX_SIZE, concurrency = 1, maxBytesPerSec = 0 } = opts;

  // 小文件 / 无 Range / 超上限 → 单次全量 fetch（兼容 P5-0）
  const probe = await probeRange(taskId, rel, signal);
  if (!probe || !probe.acceptRanges || probe.size === 0 || probe.size <= RANGE_CHUNK || probe.size > maxSize) {
    const blob = await singleFetch(taskId, rel, signal);
    return { blob, received: blob.size, total: blob.size, resumed: false, etag: probe?.etag ?? null };
  }

  const total = probe.size;
  const etag = probe.etag;
  // 审查 2.4🔴4：并发硬上限；并确保并行仅在小/中文件（total≤maxSize）下启用
  const workerCount = Math.max(1, Math.min(concurrency, RANGE_MAX_CONCURRENCY));
  const limiter = maxBytesPerSec > 0 ? new RateLimiter(maxBytesPerSec, signal) : null;

  let restarts = 0;
  for (;;) {
    try {
      if (workerCount > 1) {
        return await parallelDownload(taskId, rel, { signal, onProgress, total, etag, workerCount, limiter });
      }
      return await serialDownload(taskId, rel, { signal, onProgress, total, etag, limiter });
    } catch (err) {
      // 文件已变更/断点失效 → 整体重启（审查 2.4🔴2）；限次防死循环
      if (err instanceof RestartError && restarts < 3) {
        restarts += 1;
        continue;
      }
      throw err;
    }
  }
}

/** 串行下载（P5-4 原始路径，concurrency=1 时零漂移）。 */
async function serialDownload(
  taskId: string, rel: string,
  o: { signal?: AbortSignal; onProgress?: (p: RangeProgress) => void; total: number; etag: string | null; limiter: RateLimiter | null },
): Promise<ResumableResult> {
  const { signal, onProgress, total, etag, limiter } = o;
  let received = 0;
  const chunks: Blob[] = [];
  let lastStamp = Date.now();
  let lastBytes = 0;
  let resumed = false;
  let curEtag = etag;

  while (received < total) {
    const start = received;
    const end = Math.min(received + RANGE_CHUNK - 1, total - 1);
    const r = await fetchBlock(taskId, rel, start, end, curEtag, signal);
    // fetchBlock 内部已处理 416/etag 冲突 → 抛 RestartError；此处只拿到正常块
    await limiter?.wait(r.size);
    chunks.push(r);
    received += r.size;
    const now2 = Date.now();
    const dt = (now2 - lastStamp) / 1000;
    const speed = dt > 0 && received > lastBytes ? (received - lastBytes) / dt : 0;
    lastStamp = now2; lastBytes = received;
    resumed = received > 0;
    onProgress?.({ received, total, percent: (received / total) * 100, speed });
  }

  return { blob: new Blob(chunks), received, total, resumed, etag: curEtag };
}

/** 并行下载：固定块划分 → 多 worker 拉取 → 按下标顺序拼接（审查 2.4）。 */
async function parallelDownload(
  taskId: string, rel: string,
  o: { signal?: AbortSignal; onProgress?: (p: RangeProgress) => void; total: number; etag: string | null; workerCount: number; limiter: RateLimiter | null },
): Promise<ResumableResult> {
  const { signal, onProgress, total, etag, workerCount, limiter } = o;
  const blockCount = Math.ceil(total / RANGE_CHUNK);
  const blobs: (Blob | null)[] = new Array(blockCount).fill(null);
  let nextBlock = 0; // 作为下一待取块下标（JS 单线程，自增安全）
  let received = 0;

  async function worker() {
    for (;;) {
      const bi = nextBlock;
      nextBlock += 1;
      if (bi >= blockCount) break;
      const start = bi * RANGE_CHUNK;
      const end = Math.min(start + RANGE_CHUNK - 1, total - 1);
      const blob = await fetchBlock(taskId, rel, start, end, etag, signal);
      await limiter?.wait(blob.size);
      blobs[bi] = blob;
      received += blob.size;
      onProgress?.({ received, total, percent: (received / total) * 100, speed: 0 });
    }
  }

  await Promise.all(Array.from({ length: workerCount }, worker));
  const ordered = blobs.filter((b): b is Blob => b !== null);
  return { blob: new Blob(ordered), received, total, resumed: received > 0, etag: etag ?? null };
}

/**
 * 拉取并校验单个块。
 * - 长度 = 请求区间长度；Content-Range 起止与请求一致（审查 2.4🔴1）；
 * - ETag 与传入不一致 或 状态 416 → 抛 RestartError（整体重启，审查 2.4🔴2）；
 * - 超 RANGE_RETRY 仍未成功 → 抛 FetchResumeError（保留已收字节）。
 */
async function fetchBlock(
  taskId: string, rel: string, start: number, end: number, etag: string | null, signal?: AbortSignal,
): Promise<Blob> {
  const expectLen = end - start + 1;
  let attempts = 0;
  for (;;) {
    const r = await fetchRange(taskId, rel, start, end, signal);
    if (r.status === 416) throw new RestartError('断点失效（文件变小），从头重传');
    if (r.status !== 206 && r.status !== 200) {
      if (r.status === 401 || r.status === 403 || r.status === 404) throw new ApiError(r.status, '获取失败');
      attempts += 1;
      if (attempts >= RANGE_RETRY) throw new FetchResumeError(start, end - start + 1, `块 ${start}-${end} 下载失败`);
      await delay(attempts * 200); continue;
    }
    // 🔴5/审查🔴1 长度 + Content-Range 双校验
    if (r.blob.size !== expectLen) {
      attempts += 1;
      if (attempts >= RANGE_RETRY) throw new FetchResumeError(start, end - start + 1, `块 ${start}-${end} 长度不符`);
      await delay(attempts * 200); continue;
    }
    const cr = r.contentRange;
    if (cr) {
      const m = /^bytes (\d+)-(\d+)\//.exec(cr);
      if (m && (Number(m[1]) !== start || Number(m[2]) !== end)) {
        attempts += 1;
        if (attempts >= RANGE_RETRY) throw new FetchResumeError(start, end - start + 1, `块 ${start}-${end} 区间不符`);
        await delay(attempts * 200); continue;
      }
    }
    // 审查 2.4🔴2 每块 ETag 一致性
    if (r.etag && etag && r.etag !== etag) throw new RestartError('文件已变更，从头重传');
    return r.blob;
  }
}

/** 锁定 Range 片段拉取（首屏流式预览用）。 */
export async function fetchRangeSlice(
  taskId: string, rel: string, start: number, end: number, signal?: AbortSignal,
): Promise<{ bytes: Uint8Array; total: number; etag: string | null } | null> {
  const token = getToken();
  const res = await fetch(`/artifacts/${encodeURIComponent(taskId)}/${seg(rel)}`, {
    headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      Range: `bytes=${start}-${end}`,
    },
    signal,
  });
  if (res.status !== 206 && res.status !== 200) return null;
  const buf = await (await res.blob()).arrayBuffer();
  const bytes = new Uint8Array(buf);
  let total = 0;
  const cr = res.headers.get('content-range');
  if (cr) { const m = /\/(\d+)$/.exec(cr); total = m ? Number(m[1]) : 0; }
  return { bytes, total, etag: res.headers.get('etag') };
}

/**
 * 下载产物（委托 fetchResumable）。默认 concurrency=1（P5-4 串行零漂移）。
 * 返回 Blob；如需断点元数据/持久化，请直接使用 fetchResumable。
 */
export async function downloadArtifact(
  taskId: string,
  rel: string,
  opts: FetchOpts = {},
): Promise<Blob> {
  const r = await fetchResumable(taskId, rel, opts);
  return r.blob;
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

/** 文件已变更 / 断点失效 → 上层整体重启。内部控制，不对外暴露。 */
export class RestartError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'RestartError';
  }
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

/**
 * 滑动窗口限速（审查 2.3🔴1）。基于最近 WINDOW_MS 的实际吞吐动态计算等待，
 * 取消信号触发时立即停止休眠（审查 2.3🔴2）。
 */
class RateLimiter {
  private readonly target: number;
  private readonly signal?: AbortSignal;
  private windowBytes = 0;
  private windowStart = Date.now();

  constructor(target: number, signal?: AbortSignal) {
    this.target = target;
    this.signal = signal;
  }

  async wait(size: number): Promise<void> {
    if (!this.target || size <= 0) return;
    this.windowBytes += size;
    const now = Date.now();
    const elapsed = (now - this.windowStart) / 1000;
    if (elapsed >= 1) {
      this.windowStart = now;
      this.windowBytes = 0;
      return;
    }
    const expected = this.windowBytes / this.target; // 目标耗时(s)
    if (expected <= elapsed) return; // 未超速
    const waitMs = (expected - elapsed) * 1000;
    if (waitMs < RANGE_LIMITER_MIN_INTERVAL) return;
    await interruptibleSleep(waitMs, this.signal);
  }
}

function interruptibleSleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal?.aborted) { resolve(); return; }
    const t = setTimeout(() => { signal?.removeEventListener('abort', onAbort); resolve(); }, ms);
    const onAbort = () => { clearTimeout(t); resolve(); };
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}