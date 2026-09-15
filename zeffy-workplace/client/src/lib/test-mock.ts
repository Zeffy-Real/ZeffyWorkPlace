// 前端冒烟测试辅助：模拟支持 Range/HEAD/ETag 的后端文件响应。
// 行为镜像 server app/api/artifacts.py：
// - HEAD 返回 size + Accept-Ranges + ETag
// - GET Range: bytes=s-e → 206 + Content-Range（可选 acceptRanges=false 降级 200 全量）
// - wrongLen/corruptFrom：从某偏移起返回错误长度（测块级完整性），触发重试/失败

export interface FetchMockOpts {
  acceptRanges?: boolean;
  etag?: string;
  wrongLen?: boolean;
  corruptFrom?: number;
}

export function makeFetchMock(data: Uint8Array, opts: FetchMockOpts = {}) {
  const total = data.length;
  const accept = opts.acceptRanges ?? true;
  const etag = opts.etag ?? '"local-1"';
  const corruptFrom = opts.corruptFrom ?? 0;

  return async (input: unknown, init?: RequestInit): Promise<Response> => {
    void input;
    const method = (init?.method ?? 'GET').toUpperCase();
    const reqHeaders = new Headers(init?.headers as HeadersInit | undefined);
    const base = { 'accept-ranges': accept ? 'bytes' : 'none', etag, 'content-type': 'application/octet-stream' };

    if (method === 'HEAD') {
      return new Response(null, { status: 200, headers: { ...base, 'content-length': String(total) } });
    }

    const range = reqHeaders.get('range');
    if (range && range.startsWith('bytes=') && accept) {
      const parts = range.slice(6).split('-');
      const s = Number(parts[0]);
      const e = Math.min(Number(parts[1] ?? total - 1), total - 1);
      let chunk = data.slice(s, e + 1);
      if (opts.wrongLen && s >= corruptFrom) chunk = new Uint8Array(chunk.length + 5); // 使长度校验失败
      return new Response(chunk, {
        status: 206,
        headers: { ...base, 'content-range': `bytes ${s}-${e}/${total}`, 'content-length': String(chunk.length) },
      });
    }

    return new Response(data, { status: 200, headers: { ...base, 'content-length': String(total) } });
  };
}