// P5-6 stream-disk 冒烟：能力探测降级（🔴4）。

import { describe, expect, it } from 'vitest';
import { streamToDisk, streamToDiskCapable } from './stream-disk';

describe('streamToDisk 冒烟', () => {
  it('无 FS Access API 时上报 unsupported（降级到内存下载）', async () => {
    // node 环境默认无 showSaveFilePicker
    expect(streamToDiskCapable()).toBe(false);
    const r = await streamToDisk('t1', 'a.bin', {});
    expect(r.ok).toBe(false);
    expect(r.reason).toBe('unsupported');
  });
});