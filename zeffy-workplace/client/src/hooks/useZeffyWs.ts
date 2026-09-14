import { useEffect, useRef, useState, useCallback } from 'react';
import type { HumanDecision, WsMessage } from '../lib/protocol';
import { buildOutgoingMessage } from '../lib/protocol';

/**
 * useZeffyWs：群聊 WebSocket 钩子。
 * - 连接 / 收发 / 指数退避自动重连；token 变化时重连
 * - P3-4 新增（向后兼容）：
 *   - `token`：连接后发送 auth 帧（连接后认证，避免 query 泄露）后才视为可收发；
 *   - `taskId`：只收集该任务的事件（多任务页面共用单连接按 taskId 分发）。
 * - 断线期间本地缓存待发消息，重连成功自动补发
 */
export function useZeffyWs(opts: {
  url: string;
  token?: string | null;
  taskId?: string | null;
  onStatus?: (s: 'connecting' | 'open' | 'closed') => void;
}) {
  const { url, token, taskId, onStatus } = opts;
  const [status, setStatus] = useState<'connecting' | 'open' | 'closed'>('connecting');
  const [messages, setMessages] = useState<WsMessage[]>([]);
  const wsRef = useRef<WebSocket | null>(null);
  const retryRef = useRef(0);
  const pendingRef = useRef<WsMessage[]>([]);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const authedRef = useRef(false);

  const setStatusBoth = (s: 'connecting' | 'open' | 'closed') => {
    setStatus(s);
    onStatus?.(s);
  };

  const connect = useCallback(() => {
    const ws = new WebSocket(url);
    wsRef.current = ws;
    authedRef.current = false;
    setStatusBoth('connecting');

    const sendAuth = () => {
      if (token) ws.send(JSON.stringify({ kind: 'auth', payload: { token } }));
    };

    ws.onopen = () => {
      retryRef.current = 0;
      if (token) {
        sendAuth(); // 连接后认证帧；等待 auth_ok 前不可收发（由 onmessage 解锁）
      } else {
        authedRef.current = true;
        setStatusBoth('open');
        flushPending();
      }
    };

    const flushPending = () => {
      if (!authedRef.current) return;
      const pending = pendingRef.current.splice(0);
      for (const m of pending) ws.send(JSON.stringify(m));
    };

    ws.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data as string) as WsMessage & {
          kind: string;
        };
        // 认证帧收到前先处理认证/完成握手
        if (data.kind === 'system_notify') {
          const payload = data.payload as Record<string, unknown>;
          if (payload && (payload.auth_ok === true || payload.done === 'auth')) {
            authedRef.current = true;
            setStatusBoth('open');
            flushPending();
            return;
          }
          // 认证失败 → 关闭，由守卫跳登录
        }
        if (!authedRef.current) return; // 未认证不采信业务消息
        if (taskId) {
          const p = typeof data.payload === 'object' && data.payload !== null ? data.payload : {};
          const msgTask = (p as Record<string, unknown>).task_id as string | undefined;
          if (data.task_id !== taskId && msgTask !== taskId) return; // 只收本任务事件
        }
        setMessages((prev) => [...prev, data as WsMessage]);
      } catch {
        // 忽略非 JSON 消息
      }
    };

    ws.onclose = () => {
      setStatusBoth('closed');
      scheduleReconnect();
    };

    ws.onerror = () => {
      ws.close();
    };
  }, [url, token, taskId]);

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
    (payload: string | Record<string, unknown>, taskIdArg: string | null = null) => {
      const msg = buildOutgoingMessage('user_message', payload, taskIdArg);
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN && authedRef.current) {
        wsRef.current.send(JSON.stringify(msg));
      } else {
        pendingRef.current.push(msg); // 断线/未认证时缓存待补发
      }
    },
    [],
  );

  // P1-5/P2：对中断任务给出人工决策（审批 approve/reject / 追问 answer）
  const sendDecision = useCallback(
    (tid: string, decision: HumanDecision) => {
      const msg = buildOutgoingMessage('user_decision', { task_id: tid, decision }, tid);
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN && authedRef.current) {
        wsRef.current.send(JSON.stringify(msg));
      } else {
        pendingRef.current.push(msg);
      }
    },
    [],
  );

  return { status, messages, send, sendDecision };
}