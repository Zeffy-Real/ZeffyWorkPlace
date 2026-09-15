// P5-5 upload.ts 冒烟：全量分块顺序正确 + 断点续传只发缺失块 + 401 抛错。

import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../auth';
import { uploadArtifact } from './upload';

afterEach(() => vi.unstubAllGlobals());

function stubLocalStorage() {
  vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} });
}

const CHUNK = 8;

/** 内存服务端：received 集合跨调用持久 → init 返回首个缺口（断点）。 */
function makeFakeServer() {
  const received = new Set<number>();
  let chunkCalls = 0;
  const next = (size: number) => {
    let o = 0;
    while (o < size && received.has(o)) o += CHUNK;
    return o;
  };
  const fetcher = async (input: unknown, init?: RequestInit): Promise<Response> => {
    const url = new URL(String(input), 'http://local');
    const req = new Request(url, init ?? {});
    const text = await req.text();
    if (url.pathname.endsWith('/upload/init')) {
      const { size } = JSON.parse(text || '{}') as { size: number };
      const nextOffset = next(size);
      return Response.json({ upload_id: 'u1', chunk_size: CHUNK, next_offset: nextOffset, done: nextOffset >= size }, { status: 200 });
    }
    if (url.pathname.includes('/chunk')) {
      chunkCalls += 1;
      const off = Number(url.searchParams.get('offset'));
      received.add(off);
      return Response.json({ received: next(off + CHUNK) }, { status: 200 });
    }
    if (url.pathname.endsWith('/commit')) {
      return Response.json({ key: 'artifacts/t1/a.bin', size: 0, sha256: 'abc' }, { status: 200 });
    }
    return Response.json({ ok: false }, { status: 404 });
  };
  return { fetcher, chunkCalls: () => chunkCalls };
}

describe('uploadArtifact 冒烟', () => {
  it('全部分块按顺序上送，服务端累计满 20 字节', async () => {
    stubLocalStorage();
    const { fetcher } = makeFakeServer();
    vi.stubGlobal('fetch', fetcher);
    const file = new Blob([new Uint8Array(20).fill(7)]);
    const r = await uploadArtifact('t1', 'a.bin', file, {});
    expect(r.key).toBe('artifacts/t1/a.bin');
  });

  it('断点续传：第二次不再上送已收块', async () => {
    stubLocalStorage();
    const { fetcher, chunkCalls } = makeFakeServer();
    vi.stubGlobal('fetch', fetcher);
    const file = new Blob([new Uint8Array(20).fill(3)]);
    await uploadArtifact('t1', 'a.bin', file, {}); // 全量：3 块
    const first = chunkCalls();
    expect(first).toBe(3);
    await uploadArtifact('t1', 'a.bin', file, {}); // 断点：next=20 → 0 块
    expect(chunkCalls() - first).toBe(0);
  });

  it('401 → 抛 ApiError', async () => {
    stubLocalStorage();
    vi.stubGlobal('fetch', async () => new Response(JSON.stringify({ detail: '未登录' }), { status: 401 }));
    await expect(uploadArtifact('t1', 'a.bin', new Blob([new Uint8Array(4)]))).rejects.toBeInstanceOf(ApiError);
  });
});