import { useEffect, useRef, useState, useCallback } from 'react';
import type { WsMessage } from '../lib/protocol';
import { buildOutgoingMessage } from '../lib/protocol';

/**
 * useZeffyWs：群聊 WebSocket 钩子。
 * - 连接 / 收发 / 指数退避自动重连
 * - 断线期间本地缓存待发消息，重连成功自动补发
 * P1 群聊业务直接复用此钩子扩展，WS 逻辑不与 UI 组件耦合。
 */
export function useZeffyWs(opts: { url: string }) {
  const { url } = opts;
  const [status, setStatus] = useState<'connecting' | 'open' | 'closed'>('connecting');
  const [messages, setMessages] = useState<WsMessage[]>([]);
  const wsRef = useRef<WebSocket | null>(null);
  const retryRef = useRef(0);
  const pendingRef = useRef<WsMessage[]>([]);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const connect = useCallback(() => {
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      retryRef.current = 0;
      setStatus('open');
      // 补发断线期间的待发消息
      const pending = pendingRef.current.splice(0);
      for (const m of pending) {
        ws.send(JSON.stringify(m));
      }
    };

    ws.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data as string) as WsMessage;
        setMessages((prev) => [...prev, data]);
      } catch {
        // 忽略非 JSON 消息
      }
    };

    ws.onclose = () => {
      setStatus('closed');
      scheduleReconnect();
    };

    ws.onerror = () => {
      ws.close();
    };
  }, [url]);

  const scheduleReconnect = useCallback(() => {
    // 指数退避：1s, 2s, 4s, 8s ... 上限 30s
    const base = 1000;
    const cap = 30000;
    const delay = Math.min(base * 2 ** retryRef.current, cap);
    retryRef.current += 1;
    if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
    reconnectTimerRef.current = setTimeout(() => connect(), delay);
  }, [connect]);

  useEffect(() => {
    connect();
    return () => {
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
      wsRef.current?.close();
    };
  }, [connect]);

  const send = useCallback(
    (payload: string | Record<string, unknown>, taskId: string | null = null) => {
      const msg = buildOutgoingMessage('user_message', payload, taskId);
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify(msg));
      } else {
        pendingRef.current.push(msg); // 断线时缓存待补发
      }
    },
    [],
  );

  return { status, messages, send };
}