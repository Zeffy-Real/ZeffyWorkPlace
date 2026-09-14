// 前端侧 WS 消息协议，对齐后端 app/wsmessage.py 的结构（消息需互通）。
// kind 枚举涵盖 P1 群聊 + P1-5 审批/追问 + P2 节点状态（与后端保持同步）。

export type WsKind =
  | 'user_message'
  | 'agent_reply'
  | 'system_notify'
  | 'task_update'
  | 'review_notify'
  | 'agent_message'
  | 'task_node_update'
  | 'review_event'
  | 'user_decision';

export type TaskNodeStatus =
  | 'pending'
  | 'queued'
  | 'running'
  | 'done'
  | 'failed'
  | 'blocked'
  | 'interrupt';

export interface NodeInfo {
  node_name: string;
  status: TaskNodeStatus;
  error?: string | null;
  id?: string;
}

export interface TaskNodeUpdatePayload {
  task_id?: string;
  node_id?: string;
  node_name: string;
  status: TaskNodeStatus;
  error?: string;
  reason?: string;
}

export interface ReviewEventPayload {
  task_id: string;
  node_id?: string;
  node_name: string;
  status?: 'awaiting_approval' | 'asking' | 'approved' | 'revise';
  verdict?: 'pass' | 'revise';
  comments?: string[];
  question?: string;
}

export interface AgentMessagePayload {
  task_id: string;
  node_id?: string;
  node_name: string;
  role: string;
  text: string;
}

export interface WsMessage {
  msg_id: string;
  kind: WsKind;
  payload: string | Record<string, unknown>;
  task_id: string | null;
  timestamp: string; // UTC ISO string
}

// 人工决策（审批 approve/reject / 追问 answer）
export interface HumanDecision {
  kind: 'approval' | 'answer';
  approved?: boolean;
  comment?: string;
  text?: string;
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

export function isNodePayload(p: string | Record<string, unknown>): p is Record<string, unknown> {
  return typeof p === 'object' && p !== null;
}