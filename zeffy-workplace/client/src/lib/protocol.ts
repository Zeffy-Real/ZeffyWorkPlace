// 前端侧 WS 消息协议，对齐后端 app/wsmessage.py 的结构（消息需互通）。
// kind 枚举已预埋 P1 群聊所需的 agent/task/review 类型。

export type WsKind =
  | 'user_message'
  | 'agent_reply'
  | 'system_notify'
  | 'task_update'
  | 'review_notify';

export interface WsMessage {
  msg_id: string;
  kind: WsKind;
  payload: string | Record<string, unknown>;
  task_id: string | null;
  timestamp: string; // UTC ISO string
}

export function buildOutgoingMessage(
  kind: WsKind,
  payload: string | Record<string, unknown>,
  taskId: string | null = null,
): WsMessage {
  const msg_id =
    typeof crypto !== 'undefined' && crypto.randomUUID
      ? crypto.randomUUID()
      : Math.random().toString(36).slice(2);
  return {
    msg_id,
    kind,
    payload,
    task_id: taskId,
    timestamp: new Date().toISOString(),
  };
}