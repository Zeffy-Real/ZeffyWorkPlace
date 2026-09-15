// P5-4 遗留项 · range.ts 冒烟测试（Vitest / node 环境，纯逻辑）
// 覆盖：串行/并行分块拼装正确性、降级单次 fetch、块完整性失败、限速。

import { afterEach, describe, expect, it, vi } from 'vitest';
import { FetchResumeError, fetchResumable } from './range';
import { makeFetchMock } from './test-mock';

afterEach(() => vi.unstubAllGlobals());

function stubLocalStorage() {
  vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} });
}
function mkData(n: number): Uint8Array {
  const a = new Uint8Array(n);
  for (let i = 0; i < n; i++) a[i] = (i * 31) & 0xff;
  return a;
}

describe('fetchResumable 冒烟', () => {
  it('串行(concurrency=1) 分块拼装与原文件一致', async () => {
    stubLocalStorage();
    const data = mkData(1_000_000);
    vi.stubGlobal('fetch', makeFetchMock(data));
    const r = await fetchResumable('t1', 'a.bin', { concurrency: 1 });
    expect(new Uint8Array(await r.blob.arrayBuffer())).toEqual(data);
    expect(r.total).toBe(data.length);
    expect(r.received).toBe(data.length);
  });

  it('并行(concurrency=4) 分块拼装与原文件一致', async () => {
    stubLocalStorage();
    const data = mkData(1_000_000);
    vi.stubGlobal('fetch', makeFetchMock(data));
    const r = await fetchResumable('t1', 'a.bin', { concurrency: 4 });
    expect(new Uint8Array(await r.blob.arrayBuffer())).toEqual(data);
    expect(r.received).toBe(data.length);
  });

  it('无 Accept-Ranges → 单次全量 fetch（降级，resumed=false）', async () => {
    stubLocalStorage();
    const data = mkData(200_000);
    vi.stubGlobal('fetch', makeFetchMock(data, { acceptRanges: false }));
    const r = await fetchResumable('t1', 'a.bin');
    expect(new Uint8Array(await r.blob.arrayBuffer())).toEqual(data);
    expect(r.resumed).toBe(false);
  });

  it('块长度不符 → 重试耗尽抛 FetchResumeError（不返回损坏文件）', async () => {
    stubLocalStorage();
    const data = mkData(600_000);
    vi.stubGlobal('fetch', makeFetchMock(data, { wrongLen: true, corruptFrom: 0 }));
    await expect(fetchResumable('t1', 'a.bin')).rejects.toBeInstanceOf(FetchResumeError);
  });

  it('限速后总时长不低于目标（0.1MB/s，400KB → ≥3s）', async () => {
    stubLocalStorage();
    const data = mkData(400_000);
    vi.stubGlobal('fetch', makeFetchMock(data));
    const start = Date.now();
    const r = await fetchResumable('t1', 'a.bin', { maxBytesPerSec: 100_000 });
    const elapsed = Date.now() - start;
    expect(new Uint8Array(await r.blob.arrayBuffer())).toEqual(data);
    expect(elapsed).toBeGreaterThanOrEqual(2500);
  });
});