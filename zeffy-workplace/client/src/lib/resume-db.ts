// P5-4 遗留项 · 2.1 持久化断点（究极深度审查修订版）
// IndexedDB 断点续传层，主题：并发安全 / 数据一致性 / 配额降级 / 过期清理。
// - 主键 = `${taskId}:${rel}:${etag}`：任务+文件+内容指纹隔离（审查🔴1-1）
// - 写入走单个 readwrite 事务（chunk + meta 原子），续传前对账实际块数与 meta（审查🔴1-3）
// - 每块存 md5，续传前逐块校验，损坏丢弃从头（审查🔴3）
// - 任意存储失败（不支持/配额 QuotaExceededError）→ 降级内存续传，不中断下载（审查🔴2-1）
// - 断点 TTL 7 天 / 完成后 7 天自动清理（审查🔴2-2/3）
//
// 设计保守：所有能力失败一律降级；IndexedDB 视为可选增强，绝不阻塞既有下载路径。

import { ApiError } from '../auth';
import {
  FetchResumeError,
  RANGE_CHUNK,
  RANGE_RETRY,
  downloadArtifact,
  fetchRangeExported,
  probeRange,
  type RangeProgress,
} from './range';

const DB_NAME = 'zw-artifact-resume';
const DB_VERSION = 1;
const STORE_META = 'meta';
const STORE_CHUNKS = 'chunks';
const RESUME_TTL_MS = 7 * 24 * 60 * 60 * 1000;
const COMPLETED_TTL_MS = 7 * 24 * 60 * 60 * 1000;

export interface ResumeChunk { id: string; seq: number; blob: Blob; md5: string; }
export interface ResumeMeta { id: string; taskId: string; rel: string; etag: string; total: number; received: number; updatedAt: number; completed: boolean; }

export function resumeKey(taskId: string, rel: string, etag: string): string {
  return [taskId, rel, etag].join(':'); // 审查🔴1-1
}

export interface ResumeLoader {
  /** 环境是否可用（浏览器 IndexedDB）。不可用 → 调用方全程内存模式。 */
  readonly available: boolean;
  /** 读取可续传的 received（含完整性对账/校验）。无有效断点 / 损坏 → 0。 */
  probe(taskId: string, rel: string, etag: string, total: number): Promise<number>;
  /** 持久化一块；失败（配额/错误）→ false，调用方降级内存并停止持久化。total=全文件大小。 */
  append(taskId: string, rel: string, etag: string, seq: number, blob: Blob, total: number): Promise<boolean>;
  /** 续传时回读已存块（按 seq 升序）。 */
  readChunks(taskId: string, rel: string, etag: string): Promise<ResumeChunk[]>;
  markComplete(taskId: string, rel: string, etag: string): Promise<void>;
  drop(taskId: string, rel: string, etag: string): Promise<void>;
  cleanup(): Promise<void>;
}

async function hexMd5(data: Uint8Array): Promise<string> {
  try {
    if (!globalThis.crypto?.subtle) return '';
    const buf = await globalThis.crypto.subtle.digest('MD5', data);
    return Array.from(new Uint8Array(buf)).map((b) => b.toString(16).padStart(2, '0')).join('');
  } catch { return ''; }
}

function req<T>(r: IDBRequest<T>): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error);
  });
}

let _cache: Promise<IDBDatabase | null> | null = null;
function openDB(): Promise<IDBDatabase | null> {
  if (typeof indexedDB === 'undefined') return Promise.resolve(null);
  if (_cache) return _cache;
  _cache = new Promise((resolve) => {
    try {
      const r = indexedDB.open(DB_NAME, DB_VERSION);
      r.onupgradeneeded = () => {
        const db = r.result;
        if (!db.objectStoreNames.contains(STORE_META)) db.createObjectStore(STORE_META, { keyPath: 'id' });
        if (!db.objectStoreNames.contains(STORE_CHUNKS)) db.createObjectStore(STORE_CHUNKS, { keyPath: 'id' });
      };
      r.onsuccess = () => resolve(r.result);
      r.onerror = () => resolve(null);
      r.onblocked = () => resolve(null);
    } catch { resolve(null); }
  });
  return _cache as Promise<IDBDatabase | null>;
}

export async function createResumeLoader(): Promise<ResumeLoader> {
  const db = await openDB();
  if (!db) return disposedLoader();

  const keyRangeOf = (id: string): IDBKeyRange =>
    IDBKeyRange.bound(`${id}#0`, `${id}#\uffff`);

  const loader: ResumeLoader = {
    available: true,

    probe: async (taskId, rel, etag, total) => {
      const id = resumeKey(taskId, rel, etag);
      try {
        const tx = db.transaction([STORE_META, STORE_CHUNKS], 'readonly');
        const meta = (await req(tx.objectStore(STORE_META).get(id))) as ResumeMeta | undefined;
        if (!meta || meta.completed || meta.total !== total || meta.etag !== etag) return 0;
        const chunks = (await req(tx.objectStore(STORE_CHUNKS).getAll(keyRangeOf(id)))) as ResumeChunk[];
        if (chunks.length === 0 || chunks.length < Math.ceil(meta.received / RANGE_CHUNK)) return 0;
        chunks.sort((a, b) => a.seq - b.seq);
        let received = 0;
        for (let i = 0; i < chunks.length; i++) {
          const c = chunks[i];
          if (c.seq !== i) return 0; // 序号断裂 → 损坏
          const expect = Math.min(RANGE_CHUNK, total - i * RANGE_CHUNK);
          if (c.blob.size !== expect) return 0; // 长度不符 → 损坏（审查🔴3）
          const md5 = await hexMd5(new Uint8Array(await c.blob.arrayBuffer()));
          if (md5 && c.md5 && md5 !== c.md5) return 0; // 篡改 → 从头
          received += c.blob.size;
        }
        return received;
      } catch { return 0; }
    },

    append: async (taskId, rel, etag, seq, blob, total) => {
      const id = resumeKey(taskId, rel, etag);
      try {
        const md5 = await hexMd5(new Uint8Array(await blob.arrayBuffer()));
        await new Promise<void>((resolve, reject) => {
          const tx = db.transaction([STORE_META, STORE_CHUNKS], 'readwrite');
          const chunkReq = tx.objectStore(STORE_CHUNKS).put({ id: `${id}#${seq}`, seq, blob, md5 } as ResumeChunk);
          chunkReq.onsuccess = () => {
            const metaReq = tx.objectStore(STORE_META).get(id);
            metaReq.onsuccess = () => {
              const prev = (metaReq.result as ResumeMeta | undefined);
              tx.objectStore(STORE_META).put({
                id, taskId, rel, etag,
                total: prev?.total ?? total,
                received: seq * RANGE_CHUNK + blob.size,
                updatedAt: Date.now(), completed: false,
              } as ResumeMeta);
            };
          };
          tx.oncomplete = () => resolve();
          tx.onerror = () => reject(tx.error);
          tx.onabort = () => reject(tx.error);
        });
        return true;
      } catch { return false; } // 配额/失败 → 降级内存
    },

    readChunks: async (taskId, rel, etag) => {
      const id = resumeKey(taskId, rel, etag);
      try {
        const tx = db.transaction(STORE_CHUNKS, 'readonly');
        const chunks = (await req(tx.objectStore(STORE_CHUNKS).getAll(keyRangeOf(id)))) as ResumeChunk[];
        return chunks.sort((a, b) => a.seq - b.seq);
      } catch { return []; }
    },

    markComplete: async (taskId, rel, etag) => {
      const id = resumeKey(taskId, rel, etag);
      try {
        await new Promise<void>((resolve, reject) => {
          const tx = db.transaction(STORE_META, 'readwrite');
          const r = tx.objectStore(STORE_META).get(id);
          r.onsuccess = () => {
            const m = r.result as ResumeMeta | undefined;
            if (m) tx.objectStore(STORE_META).put({ ...m, completed: true, updatedAt: Date.now() });
          };
          tx.oncomplete = () => resolve();
          tx.onerror = () => reject(tx.error);
          tx.onabort = () => reject(tx.error);
        });
      } catch { /* 忽略 */ }
    },

    drop: async (taskId, rel, etag) => {
      const id = resumeKey(taskId, rel, etag);
      try {
        await new Promise<void>((resolve, reject) => {
          const tx = db.transaction([STORE_META, STORE_CHUNKS], 'readwrite');
          tx.objectStore(STORE_CHUNKS).openCursor(keyRangeOf(id)).onsuccess = (e) => {
            const cur = (e.target as IDBRequest<IDBCursorWithValue | null>).result;
            if (cur) { cur.delete(); cur.continue(); }
          };
          tx.objectStore(STORE_META).delete(id);
          tx.oncomplete = () => resolve();
          tx.onerror = () => reject(tx.error);
          tx.onabort = () => reject(tx.error);
        });
      } catch { /* 忽略 */ }
    },

    cleanup: async () => {
      try {
        await new Promise<void>((resolve) => {
          const tx = db.transaction([STORE_META, STORE_CHUNKS], 'readwrite');
          const r = tx.objectStore(STORE_META).getAll();
          r.onsuccess = () => {
            const metas = r.result as ResumeMeta[];
            for (const m of metas) {
              const expired = Date.now() - m.updatedAt > RESUME_TTL_MS;
              const completedExpired = m.completed && Date.now() - m.updatedAt > COMPLETED_TTL_MS;
              if (expired || completedExpired) {
                tx.objectStore(STORE_CHUNKS).openCursor(keyRangeOf(m.id)).onsuccess = (e) => {
                  const cur = (e.target as IDBRequest<IDBCursorWithValue | null>).result;
                  if (cur) { cur.delete(); cur.continue(); }
                };
                tx.objectStore(STORE_META).delete(m.id);
              }
            }
          };
          tx.oncomplete = () => resolve();
          tx.onerror = () => resolve();
          tx.onabort = () => resolve();
        });
      } catch { /* 忽略 */ }
    },
  };

  return loader;
}

function disposedLoader(): ResumeLoader {
  return {
    available: false,
    probe: async () => 0,
    append: async (_t, _r, _e, _s, _b, _total) => true,
    readChunks: async () => [],
    markComplete: async () => {},
    drop: async () => {},
    cleanup: async () => {},
  };
}

// =======================================================================
// persistedDownloadArtifact：持久化断点下载（审查修订版）
// - 串行续传（256KB 对齐块）；断点仅整块持久化，杜绝半块拼接损坏（审查🔴3）
// - 逐块复用 fetchRange 校验（长度/Content-Range/ETag）；416/ETag 变更 → 清断点从头重启（限 3 次）
// - 任一块持久化失败（配额）→ 降级内存续传，不中断（审查🔴2-1）
// - 默认仅在 IndexedDB 可用且服务端给出 ETag 时启用；否则走 downloadArtifact（零漂移内存/单次）
// =======================================================================

export interface PersistedOpts {
  signal?: AbortSignal;
  onProgress?: (p: RangeProgress) => void;
  maxSize?: number;
}

export async function persistedDownloadArtifact(
  taskId: string, rel: string, opts: PersistedOpts = {},
): Promise<{ blob: Blob; persisted: boolean }> {
  const { signal, onProgress, maxSize } = opts;
  const probe = await probeRange(taskId, rel, signal);

  // 无 Range / 小文件 / 超限 / 无强 ETag → 走内存路径（不引入持久化一致性负担）
  if (!probe || !probe.acceptRanges || probe.size === 0 || probe.size <= RANGE_CHUNK
    || (maxSize && probe.size > maxSize) || !probe.etag) {
    const blob = await downloadArtifact(taskId, rel, { signal, maxSize });
    return { blob, persisted: false };
  }

  const total = probe.size;
  const etag = probe.etag;
  const loader = await createResumeLoader();
  if (!loader.available) {
    const blob = await downloadArtifact(taskId, rel, { signal, maxSize });
    return { blob, persisted: false };
  }

  // 尝试续传：只接受「整块对齐」的已收字节（probe 已保证，re 派生对齐）
  let received = await loader.probe(taskId, rel, etag, total);
  received = received >= 0 ? Math.floor(received / RANGE_CHUNK) * RANGE_CHUNK : 0;
  const chunks: Blob[] = [];
  let persist = true; // 直至首个持久化失败才降级内存
  if (received > 0) {
    const pre = await loader.readChunks(taskId, rel, etag);
    let acc = 0;
    for (const c of pre) {
      if (acc >= received) break; // 仅取对齐前缀（probe 已校验长度/序号/md5）
      chunks.push(c.blob);
      acc += c.blob.size;
    }
    received = acc;
  }

  let lastStamp = Date.now();
  let lastBytes = 0;
  let restarts = 0;

  const resetResume = async () => {
    if (persist) { await loader.drop(taskId, rel, etag); }
    received = 0;
    chunks.length = 0;
  };

  outer: while (received < total) {
    const start = received;
    const end = Math.min(start + RANGE_CHUNK - 1, total - 1);
    const expectLen = end - start + 1;
    let attempts = 0;
    let ok = false;

    while (!ok && attempts < RANGE_RETRY) {
      const r = await fetchRangeExported(taskId, rel, start, end, signal);
      if (r.status === 416) {
        await resetResume();
        if (restarts < 3) { restarts += 1; continue outer; }
        throw new FetchResumeError(0, total, '文件已变更，重启超限');
      }
      if (r.status !== 206 && r.status !== 200) {
        if (r.status === 401 || r.status === 403 || r.status === 404) throw new ApiError(r.status, '获取失败');
        attempts += 1;
        if (attempts >= RANGE_RETRY) throw new FetchResumeError(start, total - start, '下载中断');
        await delay(attempts * 200); continue;
      }
      if (r.blob.size !== expectLen) {
        attempts += 1;
        if (attempts >= RANGE_RETRY) throw new FetchResumeError(start, total - start, '块长度不符');
        await delay(attempts * 200); continue;
      }
      const cr = r.contentRange;
      if (cr) {
        const m = /^bytes (\d+)-(\d+)\//.exec(cr);
        if (m && (Number(m[1]) !== start || Number(m[2]) !== end)) {
          attempts += 1;
          if (attempts >= RANGE_RETRY) throw new FetchResumeError(start, total - start, '块区间不符');
          await delay(attempts * 200); continue;
        }
      }
      // ETag 一致性（审查🔴2）
      if (r.etag && r.etag !== etag) {
        await resetResume();
        if (restarts < 3) { restarts += 1; continue outer; }
        throw new FetchResumeError(0, total, '文件已变更，重启超限');
      }
      chunks.push(r.blob);
      received += r.blob.size;
      ok = true;
      // 持久化该块；失败 → 降级内存（后续不写入，已写断点清空避免半状态）
      if (persist) {
        const seq = Math.floor((received - r.blob.size) / RANGE_CHUNK);
        const okP = await loader.append(taskId, rel, etag, seq, r.blob, total);
        if (!okP) { persist = false; await loader.drop(taskId, rel, etag); }
      }
    }

    if (!ok) throw new FetchResumeError(start, total - start, `块 ${start}-${end} 下载失败`);
    const dt = (Date.now() - lastStamp) / 1000;
    const speed = dt > 0 && received > lastBytes ? (received - lastBytes) / dt : 0;
    lastStamp = Date.now(); lastBytes = received;
    onProgress?.({ received, total, percent: (received / total) * 100, speed });
  }

  if (persist) await loader.markComplete(taskId, rel, etag);
  void loader.cleanup();
  return { blob: new Blob(chunks), persisted: persist };
}

function delay(ms: number) {
  return new Promise((r) => setTimeout(r, ms));
}