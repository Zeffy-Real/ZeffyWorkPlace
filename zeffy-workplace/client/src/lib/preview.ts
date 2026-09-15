// P5-2 产物在线预览 · 前端工具库（审查修订版）
// - 类型判定：扩展名 + Blob.type + 文件头魔数 三重校验（🔴1），任一不匹配降级「仅下载」
// - 编码检测：BOM + UTF-8 序列判定，非可判编码 → unsupported（🔴1 / ⭐2）
// - Markdown 安全渲染：禁外部图片/伪协议/dangerouslySetInnerHTML，链接加固（🔴2）

import { createElement, type ReactNode } from 'react';

// ---- 阈值常量 ----
export const PREVIEW_TEXT_LIMIT = 512 * 1024; // 文本 ≥ 512KB 不读取
export const PREVIEW_BINARY_LIMIT = 8 * 1024 * 1024; // 图/PDF ≥ 8MB 不生成 objectURL
export const PREVIEW_MAX_LINES = 2000; // 文本最大渲染行数（防 DOM 爆炸，🔴3）
export const PREVIEW_MAX_PREVIEW = 256 * 1024; // 单次预览最多读入 256KB 文本
export const PREVIEW_FETCH_TIMEOUT = 15000; // 拉取超时（ms）

export type PreviewKind = 'image' | 'pdf' | 'markdown' | 'text' | 'unsupported';

export interface PreviewDecision {
  kind: PreviewKind;
  ok: boolean;
  reason?: string;
}

const IMAGE_EXT = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg']);
const TEXT_EXT = new Set([
  'txt', 'md', 'markdown', 'json', 'py', 'js', 'ts', 'tsx', 'jsx',
  'sql', 'csv', 'log', 'yaml', 'yml', 'toml', 'ini', 'sh', 'html', 'htm',
]);

const EXT_MIME: Record<string, string> = {
  png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', gif: 'image/gif',
  webp: 'image/webp', svg: 'image/svg+xml', pdf: 'application/pdf',
  md: 'text/markdown', markdown: 'text/markdown', txt: 'text/plain',
};

/** 扩展名 → 初判 kind（用于按钮显隐）。 */
export function extensionKind(rel: string): PreviewKind | null {
  const ext = rel.split('.').pop()?.toLowerCase() ?? '';
  if (IMAGE_EXT.has(ext)) return 'image';
  if (ext === 'pdf') return 'pdf';
  if (ext === 'md' || ext === 'markdown') return 'markdown';
  if (TEXT_EXT.has(ext)) return 'text';
  return null;
}

function startsWithBytes(h: Uint8Array, b: number[]): boolean {
  if (h.length < b.length) return false;
  for (let i = 0; i < b.length; i++) if (h[i] !== b[i]) return false;
  return true;
}

/** 文件头魔数（🔴1：防伪装扩展名）。 */
function matchMagic(ext: string, head: Uint8Array, headAsText: string): boolean {
  switch (ext) {
    case 'png': return startsWithBytes(head, [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
    case 'jpg':
    case 'jpeg': return startsWithBytes(head, [0xff, 0xd8, 0xff]);
    case 'gif': return startsWithBytes(head, [0x47, 0x49, 0x46, 0x38]);
    case 'webp': return startsWithBytes(head, [0x52, 0x49, 0x46, 0x46]) && head.length >= 12
      && head[8] === 0x57 && head[9] === 0x45 && head[10] === 0x42 && head[11] === 0x50;
    case 'svg': return /^\s*(<\?xml[^>]*>\s*)?<svg\b/i.test(headAsText);
    case 'pdf': return startsWithBytes(head, [0x25, 0x50, 0x44, 0x46]);
    default: return true; // 文本类无需魔数（走编码检测）
  }
}

/** 三重校验（🔴1）：扩展名 → Blob.type 前缀 → 魔数 + 编码。不符 → unsupported。 */
export async function previewDecisionAsync(rel: string, blob: Blob): Promise<PreviewDecision> {
  const kind = extensionKind(rel);
  if (!kind) return { kind: 'unsupported', ok: false, reason: '类型不支持预览' };
  const ext = rel.split('.').pop()?.toLowerCase() ?? '';
  const expect = EXT_MIME[ext];
  if (expect && !blob.type.startsWith(expect)) {
    return { kind: 'unsupported', ok: false, reason: `MIME 不匹配（期望 ${expect}）` };
  }
  if (kind === 'image' || kind === 'pdf') {
    if (blob.size > PREVIEW_BINARY_LIMIT) return { kind: 'unsupported', ok: false, reason: '文件过大，请下载' };
  } else if (blob.size > PREVIEW_TEXT_LIMIT) {
    return { kind: 'unsupported', ok: false, reason: '文件过大，请下载' };
  }
  try {
    const { head, headAsText } = await readHead(blob);
    if (!matchMagic(ext, head, headAsText)) {
      return { kind: 'unsupported', ok: false, reason: '文件内容与类型不符，拒绝预览' };
    }
    if (kind === 'text' || kind === 'markdown') {
      if (sniffEncoding(head) === 'unsupported') {
        return { kind: 'unsupported', ok: false, reason: '编码不支持预览' };
      }
    }
    return { kind, ok: true };
  } catch {
    return { kind: 'unsupported', ok: false, reason: '无法读取文件内容' };
  }
}

/** 仅扩展名判定（列表按钮预判；真正打开前再做完整校验）。 */
export function previewable(rel: string): boolean {
  return extensionKind(rel) !== null;
}

// ---- 编码检测（🔴1 / ⭐2）----
export function sniffEncoding(head: Uint8Array): 'utf-8' | 'utf-16le' | 'utf-16be' | 'gbk' | 'unsupported' {
  if (head.length >= 3 && head[0] === 0xef && head[1] === 0xbb && head[2] === 0xbf) return 'utf-8';
  if (head.length >= 2 && head[0] === 0xff && head[1] === 0xfe) return 'utf-16le';
  if (head.length >= 2 && head[0] === 0xfe && head[1] === 0xff) return 'utf-16be';
  let high = 0;
  let bad = 0;
  for (let i = 0; i < head.length; i++) {
    const b = head[i];
    if (b >= 0x80) high++;
    if (b >= 0xc2 && i + 1 < head.length) {
      const nb = head[i + 1];
      if (nb < 0x80 || nb > 0xbf) bad++;
      i++;
    }
  }
  if (high > 0 && bad > high / 2) return 'gbk'; // 疑似 GBK（大量非法 UTF-8 多字节）
  return 'utf-8';
}

function readHead(blob: Blob): Promise<{ head: Uint8Array; headAsText: string }> {
  return blob.slice(0, 8192).arrayBuffer().then((buf) => {
    const head = new Uint8Array(buf);
    let headAsText = '';
    try { headAsText = new TextDecoder('utf-8').decode(head); } catch { headAsText = ''; }
    return { head, headAsText };
  });
}

/** 按检测编码解码文本（⭐2 GBK 转码）。超长截断到 PREVIEW_MAX_PREVIEW。 */
export async function decodeText(blob: Blob): Promise<{ text: string; encoding: string } | { error: string }> {
  const bytes = new Uint8Array(await blob.slice(0, PREVIEW_MAX_PREVIEW).arrayBuffer());
  const enc = sniffEncoding(bytes);
  if (enc === 'unsupported') return { error: '编码不支持或文件损坏' };
  try {
    return { text: new TextDecoder(enc).decode(bytes), encoding: enc };
  } catch {
    return { error: '解码失败（文件损坏或编码不支持）' };
  }
}

/** 文本预览行数截断（🔴3）。 */
export function truncateLines(text: string, maxLines = PREVIEW_MAX_LINES): { text: string; truncated: boolean } {
  const lines = text.split('\n');
  if (lines.length <= maxLines) return { text, truncated: false };
  return { text: lines.slice(0, maxLines).join('\n'), truncated: true };
}

// ---- Markdown 安全渲染（🔴2，React 元素，禁 dangerouslySetInnerHTML）----

const SAFE_LINK = /^https?:\/\//i;

/** 行内渲染：行内码 + 链接（http/https 白名单 + 加固）；其余转义。 */
function renderInline(text: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const parts = text.split(/(`[^`]+`)/g);
  let lk = 0;
  parts.forEach((p, idx) => {
    if (p.startsWith('`') && p.endsWith('`') && p.length >= 2) {
      nodes.push(createElement('code', { key: `c${idx}` }, p.slice(1, -1)));
      return;
    }
    if (!p) return;
    const linkRe = /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g;
    let last = 0;
    let m: RegExpExecArray | null;
    while ((m = linkRe.exec(p)) !== null) {
      if (m.index > last) nodes.push(p.slice(last, m.index));
      const url = m[2];
      if (SAFE_LINK.test(url)) {
        nodes.push(createElement('a', {
          key: `l${idx}-${lk++}`, href: url, target: '_blank',
          rel: 'noopener noreferrer', title: url,
        }, m[1], ' ⧉'));
      } else {
        nodes.push(`[${m[1]}](${url})`); // 非白名单 → 纯文本
      }
      last = m.index + m[0].length;
    }
    if (last < p.length) nodes.push(p.slice(last));
  });
  return nodes;
}

/** 粗体 **x** 处理（外层 append 到段落/列表项）。 */
function appendInline(base: ReactNode[], text: string): ReactNode[] {
  const parts = text.split(/(\*\*[^*]+\*\*)/g);
  parts.forEach((p, i) => {
    if (p.startsWith('**') && p.endsWith('**') && p.length >= 4) {
      base.push(createElement('strong', { key: `b${i}` }, ...renderInline(p.slice(2, -2))));
    } else if (p) {
      base.push(...renderInline(p));
    }
  });
  return base;
}

function splitRow(line: string): string[] {
  return line.replace(/^\s*\|\s*/, '').replace(/\s*\|\s*$/, '').split(/\s*\|\s*/);
}

/** Markdown（子集）→ ReactNode[]。外部图片纯文本化；伪协议剥除。 */
export function markdownToReact(md: string): ReactNode[] {
  const lines = md.split(/\r?\n/);
  const out: ReactNode[] = [];
  let i = 0;
  let key = 0;
  let code: string[] | null = null;
  let list: { ordered: boolean; items: ReactNode[] } | null = null;

  const flushList = () => {
    if (list) {
      out.push(createElement(list.ordered ? 'ol' : 'ul', { key: key++ }, list.items));
      list = null;
    }
  };

  while (i < lines.length) {
    const line = lines[i];
    if (/^```/.test(line)) {
      flushList();
      if (code === null) { code = []; i++; continue; }
      out.push(createElement('pre', { key: key++ }, createElement('code', null, code.join('\n'))));
      code = null; i++; continue;
    }
    if (code !== null) { code.push(line); i++; continue; }

    // 表格
    if (/^\s*\|/.test(line) && /^\s*\|[\s:|]*-+[\s:|]*-*[\s:|]*\|\s*$/.test(lines[i + 1] ?? '')) {
      flushList();
      const header = splitRow(line);
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) { rows.push(splitRow(lines[i])); i++; }
      const trs: ReactNode[] = [
        createElement('tr', { key: 'h' }, header.map((c, ci) => createElement('th', { key: ci }, ...renderInline(c)))),
        ...rows.map((r, ri) => createElement('tr', { key: ri }, r.map((c, ci) => createElement('td', { key: ci }, ...renderInline(c))))),
      ];
      out.push(createElement('table', { key: key++ }, createElement('tbody', null, trs)));
      continue;
    }

    const hm = /^(#{1,6})\s+(.*)$/.exec(line);
    if (hm) {
      flushList();
      out.push(createElement(`h${hm[1].length}` as 'h1', { key: key++ }, ...renderInline(hm[2])));
      i++; continue;
    }

    const lm = /^(\s*)([-*+]|\d+\.)\s+(.*)$/.exec(line);
    if (lm) {
      const ordered = /\d+\./.test(lm[2]);
      if (!list || list.ordered !== ordered) { flushList(); list = { ordered, items: [] }; }
      const liNode: ReactNode[] = [];
      appendInline(liNode, lm[3]);
      list.items.push(createElement('li', { key: list.items.length }, ...liNode));
      i++; continue;
    }
    flushList();

    if (/^\s*([-*_])\s*\1\s*\1[-\s*_]*\s*$/.test(line)) { out.push(createElement('hr', { key: key++ })); i++; continue; }
    if (line.trim() === '') { i++; continue; }

    // 普通段落：外部图片 ![](url) 纯文本化（🔴2：不请求第三方）
    const imgInline = line.replace(/!\[([^\]]*)\]\((https?:\/\/[^)\s]+)\)/g, '![$1]($2)');
    const p: ReactNode[] = [];
    appendInline(p, imgInline);
    out.push(createElement('p', { key: key++ }, ...p));
    i++;
  }
  flushList();
  if (code !== null) out.push(createElement('pre', { key: key++ }, createElement('code', null, code.join('\n'))));
  return out;
}