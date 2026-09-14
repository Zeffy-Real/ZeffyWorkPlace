import { useCallback, useEffect, useState } from 'react';
import { useZeffyWs } from './hooks/useZeffyWs';
import type {
  AgentMessagePayload,
  HumanDecision,
  NodeInfo,
  ReviewEventPayload,
  TaskNodeStatus,
  WsMessage,
} from './lib/protocol';

// 状态 → 颜色/文案（克制扁平，避免高饱和）
const STATUS_META: Record<TaskNodeStatus, { color: string; label: string }> = {
  pending: { color: '#9ca3af', label: '等待' },
  queued: { color: '#9ca3af', label: '排队' },
  running: { color: '#2563eb', label: '执行中' },
  done: { color: '#16a34a', label: '完成' },
  failed: { color: '#dc2626', label: '失败' },
  blocked: { color: '#d97706', label: '阻塞' },
  interrupt: { color: '#d97706', label: '待人工' },
};

function NodeStrip({ nodes }: { nodes: Record<string, NodeInfo> }) {
  const entries = Object.entries(nodes);
  if (entries.length === 0) {
    return <div style={s.muted}>尚无工作流节点。</div>;
  }
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 12 }}>
      {entries.map(([name, n]) => {
        const meta = STATUS_META[n.status] ?? STATUS_META.pending;
        return (
          <div
            key={name}
            style={{
              border: '1px solid #e5e7eb', borderRadius: 6, padding: '4px 10px',
              fontSize: 13, display: 'flex', gap: 6, alignItems: 'center',
            }}
          >
            <span
              style={{
                width: 8, height: 8, borderRadius: '50%', background: meta.color, flexShrink: 0,
              }}
            />
            <span>{name}</span>
            <span style={{ color: '#6b7280' }}>{meta.label}</span>
          </div>
        );
      })}
    </div>
  );
}

function ApprovalCard({
  taskId, question, onDecision,
}: {
  taskId: string;
  question?: string;
  onDecision: (taskId: string, d: HumanDecision) => void;
}) {
  const [comment, setComment] = useState('');
  const [busy, setBusy] = useState(false);
  const decide = (approved: boolean) => {
    setBusy(true);
    onDecision(taskId, { kind: 'approval', approved, comment: comment.trim() || undefined });
    setTimeout(() => setBusy(false), 500);
  };
  return (
    <div style={s.card}>
      <div style={{ fontWeight: 600, marginBottom: 6 }}>
        🔎 审批：{taskId.slice(0, 8)}…
      </div>
      {question && (
        <div style={{ fontSize: 13, color: '#374151', marginBottom: 8 }}>{question}</div>
      )}
      <input
        value={comment}
        onChange={(e) => setComment(e.target.value)}
        placeholder="意见（可选，驳回时建议填写）"
        style={{ ...s.input, marginBottom: 8, display: 'block', width: '100%' }}
      />
      <div style={{ display: 'flex', gap: 8 }}>
        <button onClick={() => decide(true)} disabled={busy} style={s.btnPrimary}>
          通过
        </button>
        <button onClick={() => decide(false)} disabled={busy} style={s.btnDanger}>
          驳回
        </button>
      </div>
    </div>
  );
}

function AskCard({
  taskId, question, onDecision,
}: {
  taskId: string;
  question?: string;
  onDecision: (taskId: string, d: HumanDecision) => void;
}) {
  const [text, setText] = useState('');
  const submit = () => {
    if (!text.trim()) return;
    onDecision(taskId, { kind: 'answer', text: text.trim() });
    setText('');
  };
  return (
    <div style={s.card}>
      <div style={{ fontWeight: 600, marginBottom: 6 }}>✋ 需补充信息：{taskId.slice(0, 8)}…</div>
      {question && <div style={{ fontSize: 13, color: '#374151', marginBottom: 8 }}>{question}</div>}
      <input
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => e.key === 'Enter' && submit()}
        placeholder="补充信息"
        style={{ ...s.input, marginBottom: 8, display: 'block', width: '100%' }}
      />
      <button onClick={submit} style={s.btnPrimary}>
        提交
      </button>
    </div>
  );
}

function ArtifactMessage({ p }: { p: AgentMessagePayload }) {
  return (
    <div style={{ ...s.card, borderLeft: '3px solid #2563eb' }}>
      <div style={{ fontSize: 12, color: '#6b7280', marginBottom: 4 }}>
        [{p.node_name} · {p.role}]
      </div>
      <div style={{ whiteSpace: 'pre-wrap', fontSize: 14, color: '#111827' }}>{p.text}</div>
    </div>
  );
}

export default function App() {
  const [input, setInput] = useState('');
  const [nodes, setNodes] = useState<Record<string, NodeInfo>>({});
  const [awaiting, setAwaiting] = useState<{ task_id: string; kind: 'approval' | 'ask'; q?: string } | null>(null);
  const ws = useZeffyWs({ url: `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws` });

  // REST 对账：拿到 task_id 后拉取 DB 真相源，弥合断线/乱序丢事件（🔴 审查前端对账）。
  const reconcile = useCallback(async (taskId: string | null) => {
    if (!taskId) return;
    try {
      const res = await fetch(`/tasks/${taskId}/nodes`);
      if (!res.ok) return;
      const data = (await res.json()) as {
        items: {
          id: string; node_name: string; status: string; error?: string | null;
          node_type?: string;
        }[];
      };
      const map: Record<string, NodeInfo> = {};
      for (const it of data.items) {
        map[it.node_name] = {
          node_name: it.node_name,
          status: it.status as TaskNodeStatus,
          error: it.error ?? null,
          id: it.id,
          node_type: it.node_type,
        };
      }
      setNodes(map);
      // 🔴 P2-5：DB 重建审批卡——刷新/断线后，若存在 blocked 的 HITL 节点，
      // 即使没有 WS 事件也据 DB 状态重新渲染审批卡（审查：卡不依赖 WS 事件）。
      const blockedHitl = data.items.find(
        (n) => n.status === 'blocked' && n.node_type === 'hitl',
      );
      if (blockedHitl) {
        setAwaiting((prev) => prev ?? { task_id: taskId, kind: 'approval' });
      }
    } catch {
      // 对账失败不阻断
    }
  }, []);

  useEffect(() => {
    if (ws.messages.length === 0) return;
    const last = ws.messages[ws.messages.length - 1];
    const p = typeof last.payload === 'object' && last.payload !== null ? last.payload : {};
    const taskId = (p.task_id as string) || last.task_id;
    // 节点增量更新
    if (last.kind === 'task_node_update') {
      setNodes((prev) => ({
        ...prev,
        [p.node_name as string]: {
          node_name: p.node_name as string,
          status: p.status as TaskNodeStatus,
          error: (p.error as string | undefined) ?? null,
          id: (p.node_id as string | undefined) ?? (prev[p.node_name as string]?.id),
        },
      }));
    }
    // 审批/追问卡
    if (last.kind === 'review_event') {
      const rp = p as unknown as ReviewEventPayload;
      if (rp.status === 'awaiting_approval' && taskId) {
        setAwaiting({ task_id: taskId, kind: 'approval' });
        reconcile(taskId);
      } else if (rp.status === 'asking' && taskId) {
        setAwaiting({ task_id: taskId, kind: 'ask', q: rp.question });
        reconcile(taskId);
      } else if (rp.status === 'approved' || rp.verdict === 'pass') {
        setAwaiting(null);
      }
    }
    // 入队确认/收尾清理卡
    if (last.kind === 'task_update' && (p.event as string) === 'enqueued') {
      if (taskId) reconcile(taskId);
    }
  }, [ws.messages, reconcile]);

  const handleSend = () => {
    const text = input.trim();
    if (!text) return;
    ws.send(text);
    setInput('');
  };

  const handleDecision = (taskId: string, d: HumanDecision) => {
    ws.sendDecision(taskId, d);
  };

  return (
    <div style={{ maxWidth: 720, margin: '0 auto', padding: 24 }}>
      <h1 style={{ fontSize: 20 }}>Zeffy-Workplace · 任务工作台</h1>
      <div style={{ marginBottom: 12, fontSize: 12, color: '#6b7280' }}>
        连接状态：<code>{ws.status}</code>
      </div>

      <NodeStrip nodes={nodes} />

      {awaiting?.kind === 'approval' && (
        <ApprovalCard
          taskId={awaiting.task_id}
          question={awaiting.q}
          onDecision={handleDecision}
        />
      )}
      {awaiting?.kind === 'ask' && (
        <AskCard taskId={awaiting.task_id} question={awaiting.q} onDecision={handleDecision} />
      )}

      <div style={{ minHeight: 200, border: '1px solid #e5e7eb', borderRadius: 8, padding: 12, marginBottom: 16 }}>
        {ws.messages.map((m) => {
          if (m.kind === 'agent_message' && typeof m.payload === 'object' && m.payload !== null) {
            return <ArtifactMessage key={m.msg_id} p={m.payload as unknown as AgentMessagePayload} />;
          }
          return <MessageRow key={m.msg_id} msg={m} />;
        })}
      </div>

      <div style={{ display: 'flex', gap: 8 }}>
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && handleSend()}
          placeholder="输入任务目标（如：写一篇公司简介），回车提交"
          style={{ flex: 1, padding: 8, borderRadius: 6, border: '1px solid #d1d5db' }}
        />
        <button onClick={handleSend} style={s.btnPrimary}>
          提交任务
        </button>
      </div>
    </div>
  );
}

function MessageRow({ msg }: { msg: WsMessage }) {
  const isNodeKind = ['task_node_update', 'review_event', 'task_update'].includes(msg.kind);
  if (isNodeKind) {
    return null; // 节点/审批已由专用区域渲染
  }
  const role = msg.kind === 'user_message' ? '你' : msg.kind === 'system_notify' ? '系统' : msg.kind;
  const content = typeof msg.payload === 'string' ? msg.payload : JSON.stringify(msg.payload);
  return (
    <div style={{ margin: '8px 0', fontSize: 14 }}>
      <strong>{role}:</strong> <span>{content}</span>
      <div style={{ fontSize: 11, color: '#888' }}>({msg.timestamp})</div>
    </div>
  );
}

const s: Record<string, React.CSSProperties> = {
  card: { border: '1px solid #e5e7eb', borderRadius: 8, padding: 12, marginBottom: 12, background: '#fff' },
  input: { padding: 8, borderRadius: 6, border: '1px solid #d1d5db', fontSize: 14 },
  btnPrimary: { padding: '8px 16px', borderRadius: 6, border: 'none', background: '#2563eb', color: '#fff', cursor: 'pointer' },
  btnDanger: { padding: '8px 16px', borderRadius: 6, border: '1px solid #dc2626', background: '#fff', color: '#dc2626', cursor: 'pointer' },
  muted: { color: '#6b7280', fontSize: 13 },
};