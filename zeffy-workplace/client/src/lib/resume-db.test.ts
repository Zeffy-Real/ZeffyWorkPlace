// P5-4 遗留项 · resume-db / persistedDownloadArtifact 冒烟测试（Vitest + fake-indexeddb）
// 覆盖：IndexedDB 可用性、追加/探测完整性对账、drop 清理、断点续传减少拉取量。

import 'fake-indexeddb/auto';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { RANGE_CHUNK } from './range';
import { createResumeLoader, persistedDownloadArtifact } from './resume-db';
import { makeFetchMock } from './test-mock';

afterEach(() => vi.unstubAllGlobals());

function stubLocalStorage() {
  vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} });
}
function mkData(n: number): Uint8Array {
  const a = new Uint8Array(n);
  for (let i = 0; i < n; i++) a[i] = (i * 17) & 0xff;
  return a;
}

describe('resume-db 冒烟', () => {
  it('loader 可用 + append/probe 完整对账 + drop 清理', async () => {
    stubLocalStorage();
    const loader = await createResumeLoader();
    expect(loader.available).toBe(true);
    const total = RANGE_CHUNK;
    const blob = new Blob([new Uint8Array(RANGE_CHUNK)]);
    expect(await loader.append('t1', 'a.bin', 'e1', 0, blob, total)).toBe(true);
    expect(await loader.probe('t1', 'a.bin', 'e1', total)).toBe(RANGE_CHUNK);
    const chunks = await loader.readChunks('t1', 'a.bin', 'e1');
    expect(chunks.length).toBe(1);
    await loader.drop('t1', 'a.bin', 'e1');
    expect(await loader.probe('t1', 'a.bin', 'e1', total)).toBe(0);
  });

  it('续传时只拉取剩余块，拼装结果与原文件一致', async () => {
    stubLocalStorage();
    const data = mkData(3 * RANGE_CHUNK); // 3 块
    // 预置断点：只存 block0 → received=RANGE_CHUNK
    const loader = await createResumeLoader();
    await loader.append('t2', 'big.bin', '"x1"', 0, new Blob([data.slice(0, RANGE_CHUNK)]), data.length);

    let fetchedBytes = 0;
    const base = makeFetchMock(data, { etag: '"x1"' });
    const counting = async (input: unknown, init?: RequestInit): Promise<Response> => {
      if ((init?.method ?? 'GET').toUpperCase() === 'GET') {
        const rh = new Headers(init?.headers as HeadersInit | undefined);
        const range = rh.get('range');
        if (range && range.startsWith('bytes=')) {
          const [s, e] = range.slice(6).split('-').map(Number);
          if (!Number.isNaN(s) && !Number.isNaN(e)) fetchedBytes += e - s + 1;
        }
      }
      return base(input, init);
    };
    vi.stubGlobal('fetch', counting);

    const r = await persistedDownloadArtifact('t2', 'big.bin', {});
    expect(r.persisted).toBe(true);
    expect(new Uint8Array(await r.blob.arrayBuffer())).toEqual(data);
    // 只续拉剩余 2 块，而非重新拉 3 块
    expect(fetchedBytes).toBe(2 * RANGE_CHUNK);
  });
});