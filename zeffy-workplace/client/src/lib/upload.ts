// P5-5 前端上传断点续传器（究极审查修订版）
// - 三段式 init/chunk/commit + 可选的 status/cancel
// - 严格按 next_offset 顺序上送（服务端强制连续，勿跳块）
// - 中断/失败保留已传块；再次 uploadArtifact 自动从断点续传
// - 任意失败抛出；调用方可用 signal 取消

import { ApiError, getToken } from '../auth';

export interface UploadProgress {
  sent: number;
  total: number;
  percent: number;
  speed: number;
}

export interface UploadInitResult {
  upload_id: string;
  chunk_size: number;
  next_offset: number;
  done: boolean;
}

async function json<T>(path: string, init: RequestInit): Promise<T> {
  const token = getToken();
  const res = await fetch(path, {
    ...init,
    headers: { ...(init.headers as Record<string, string> | undefined), ...(token ? { Authorization: `Bearer ${token}` } : {}) },
  });
  if (res.status === 401) throw new ApiError(401, '未登录或会话过期');
  const text = await res.text();
  const body = text ? JSON.parse(text) : {};
  if (!res.ok) throw new ApiError(res.status, (body as { detail?: string }).detail ?? `HTTP ${res.status}`);
  return body as T;
}

export function uploadInit(taskId: string, rel: string, size: number, md5?: string): Promise<UploadInitResult> {
  return json<UploadInitResult>('/artifacts/upload/init', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ task_id: taskId, rel, size, md5: md5 || undefined }),
  });
}

async function putChunk(uploadId: string, offset: number, data: Uint8Array, signal?: AbortSignal): Promise<{ received: number }> {
  const token = getToken();
  const res = await fetch(`/artifacts/upload/${encodeURIComponent(uploadId)}/chunk?offset=${offset}`, {
    method: 'PUT',
    headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}), 'Content-Type': 'application/octet-stream' },
    body: data,
    signal,
  });
  if (res.status === 401) throw new ApiError(401, '未登录或会话过期');
  const text = await res.text();
  const body = text ? JSON.parse(text) : {};
  if (!res.ok) throw new ApiError(res.status, (body as { detail?: string }).detail ?? `HTTP ${res.status}`);
  return body as { received: number };
}

export function uploadStatus(uploadId: string): Promise<{ received: number; next_offset: number; done: boolean; size: number }> {
  return json(`/artifacts/upload/${encodeURIComponent(uploadId)}`, { method: 'GET', cache: 'no-store' });
}

export function commitUpload(uploadId: string, signal?: AbortSignal): Promise<{ key: string; size: number; sha256: string }> {
  return json(`/artifacts/upload/${encodeURIComponent(uploadId)}/commit`, { method: 'POST', signal });
}

export function cancelUpload(uploadId: string): Promise<{ ok: boolean }> {
  return json(`/artifacts/upload/${encodeURIComponent(uploadId)}`, { method: 'DELETE' });
}

/**
 * 断点续传上传：init（拿断点）→ 按 chunk_size 顺序上送缺失块 → commit。
 * onProgress: { sent, total, percent, speed }。
 */
export async function uploadArtifact(
  taskId: string,
  rel: string,
  file: File | Blob,
  opts: { signal?: AbortSignal; onProgress?: (p: UploadProgress) => void; md5?: string } = {},
): Promise<{ key: string; sha256: string }> {
  const { signal, onProgress, md5 } = opts;
  const total = file.size;
  const init = await uploadInit(taskId, rel, total, md5);
  const { upload_id: uploadId, chunk_size } = init;

  let sent = init.next_offset; // 断点：只上送缺失部分
  let offset = init.next_offset;
  let lastStamp = Date.now();
  let lastBytes = sent;

  while (offset < total) {
    const end = Math.min(offset + chunk_size, total);
    const slice = new Uint8Array(await file.slice(offset, end).arrayBuffer());
    // 严格连续：服务端拒绝 offset > next；此处依赖 init 的 next_offset 顺序推进
    await putChunk(uploadId, offset, slice, signal);
    offset = end;
    sent = end;
    const dt = (Date.now() - lastStamp) / 1000;
    const speed = dt > 0 && sent > lastBytes ? (sent - lastBytes) / dt : 0;
    lastStamp = Date.now(); lastBytes = sent;
    onProgress?.({ sent, total, percent: (sent / total) * 100, speed });
  }

  const done = await commitUpload(uploadId, signal);
  return { key: done.key, sha256: done.sha256 };
}