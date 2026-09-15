// P5-6 流式直写磁盘（File System Access API，究极审查修订版）
// - 🔴4 降级体验：能力探测，调用方据此决定是否展示「另存为」入口
// - 🔴3 背压：严格串行（在途≤1），无内存堆积；小文件走内存路径由调用方决定
// - 🔴2/🔴1 中断清理：取消/失败 abort + 关闭句柄，不泄漏（FS API 无法原子 rename，残余已标注）
// - 复用下载侧块校验（probeRange/fetchRangeSlice/RANGE_CHUNK 对齐）

import { ApiError } from '../auth';
import { RANGE_CHUNK, fetchRangeSlice, probeRange, type RangeProgress } from './range';

/** File System Access API 是否可用（仅 Chromium 系）。 */
export function streamToDiskCapable(): boolean {
  return typeof globalThis !== 'undefined'
    && typeof (globalThis as unknown as { showSaveFilePicker?: unknown }).showSaveFilePicker === 'function';
}

export interface StreamDiskResult {
  ok: boolean;
  reason?: 'unsupported' | 'cancelled' | 'error';
  total?: number;
}

export async function streamToDisk(
  taskId: string,
  rel: string,
  opts: { signal?: AbortSignal; onProgress?: (p: RangeProgress) => void } = {},
): Promise<StreamDiskResult> {
  const { signal, onProgress } = opts;
  const picker = (globalThis as unknown as {
    showSaveFilePicker?: (o: { suggestedName: string }) => Promise<{ createWritable: () => Promise<{ write: (d: Uint8Array) => Promise<void>; close: () => Promise<void>; abort: () => Promise<void> }> }>;
  }).showSaveFilePicker;

  if (typeof picker !== 'function') return { ok: false, reason: 'unsupported' };

  let handle: Awaited<ReturnType<NonNullable<typeof picker>>>;
  try {
    handle = await picker({ suggestedName: rel.split('/').pop() || 'download' });
  } catch {
    return { ok: false, reason: 'cancelled' }; // 用户取消保存框，静默终止
  }

  const probe = await probeRange(taskId, rel, signal);
  if (!probe) return { ok: false, reason: 'error' };
  const total = probe.size;
  const writable = await handle.createWritable();
  let received = 0;
  try {
    // 严格串行：上一块写盘完成后再请求下一块（🔴3 背压，在途≤1）
    for (let start = 0; start < total; start += RANGE_CHUNK) {
      if (signal?.aborted) throw new ApiError(0, '已取消');
      const end = Math.min(start + RANGE_CHUNK - 1, total - 1);
      const slice = await fetchRangeSlice(taskId, rel, start, end, signal);
      if (!slice) throw new ApiError(0, '读取失败');
      await writable.write(slice.bytes);
      received += slice.bytes.length;
      onProgress?.({ received, total, percent: (received / total) * 100, speed: 0 });
    }
    await writable.close();
    return { ok: true, total };
  } catch (err) {
    try { await writable.abort(); } catch { /* 忽略 */ }
    if (signal?.aborted || (err instanceof ApiError && err.status === 0)) {
      return { ok: false, reason: 'cancelled' };
    }
    return { ok: false, reason: 'error' };
  }
}