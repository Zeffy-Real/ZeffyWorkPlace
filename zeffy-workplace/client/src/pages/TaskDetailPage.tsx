import { useCallback, useEffect, useMemo, useState } from 'react';
import { ApiError, api, artifactRel, NodeDTO } from '../auth';
import { ArtifactPreview } from '../components/ArtifactPreview';
import { downloadArtifact as downloadArtifactResumable } from '../lib/range';
import { previewable } from '../lib/preview';
import { streamToDisk, streamToDiskCapable } from '../lib/stream-disk';
import { uploadArtifact } from '../lib/upload';
import { colors, space, t } from '../theme';
import type {
  AgentMessagePayload,
  HumanDecision,
  NodeInfo,
  ReviewEventPayload,
  TaskNodeStatus,
  WsMessage,
} from '../lib/protocol';

const STATUS_META: Record<string, { color: string; label: string }> = {
  pending: { color: '#9ca3af', label: '等待' },
  queued: { color: '#9ca3af', label: '排队' },
  running: { color: '#2563eb', label: '执行中' },
  done: { color: '#16a34a', label: '完成' },
  failed: { color: '#dc2626', label: '失败' },
  blocked: { color: '#d97706', label: '阻塞' },
  interrupt: { color: '#d97706', label: '待人工' },
};

export function TaskDetailPage({
  taskId,
  messages,
  wsStatus,
  send,
  sendDecision,
  onBack,
  onAuthLost,
}: {
  taskId: string;
  messages: WsMessage[];
  wsStatus: 'connecting' | 'open' | 'closed';
  send: (payload: string | Record<string, unknown>, taskIdArg?: string | null) => void;
  sendDecision: (taskId: string, d: HumanDecision) => void;
  onBack: () => void;
  onAuthLost: () => void;
}) {
  const [nodes, setNodes] = useState<Record<string, NodeInfo>>({});
  const [awaiting, setAwaiting] = useState<{ task_id: string; kind: 'approval' | 'ask'; q?: string } | null>(null);
  const [loaded, setLoaded] = useState(false);
  // P5 产物面板
  const [artifacts, setArtifacts] = useState<string[]>([]);
  const [artVer, setArtVer] = useState(0); // 上传后手动触发产物列表刷新
  const [artLoading, setArtLoading] = useState(false);
  const [artError, setArtError] = useState<string | null>(null);
  // P5-2 在线预览（单例：同时至多 1 个，🔴3）
  const [preview, setPreview] = useState<{ task_id: string; rel: string } | null>(null);

  // 🔴 订阅先于拉取：仅当全局 WS 已 open 才发起 GET 全量拉取，消灭事件缝隙。
  const reconcile = useCallback(async () => {
    try {
      const data = await api.taskNodes(taskId);
      const map: Record<string, NodeInfo> = {};
      for (const it of data.items as NodeDTO[]) {
        map[it.node_name] = {
          node_name: it.node_name,
          status: it.status as TaskNodeStatus,
          error: it.error ?? null,
          id: it.id,
          node_type: it.node_type,
        };
      }
      setNodes(map);
      const blockedHitl = data.items.find((n) => n.status === 'blocked' && n.node_type === 'hitl');
      if (blockedHitl) {
        setAwaiting((prev) => prev ?? { task_id: taskId, kind: 'approval' });
      }
      setLoaded(true);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        onAuthLost();
        return;
      }
      setLoaded(true);
    }
  }, [taskId, onAuthLost]);

  useEffect(() => {
    if (wsStatus !== 'open') return; // WS 建立后才拉取（订阅先于拉取）
    void reconcile();
    return () => setLoaded(false);
  }, [wsStatus, reconcile]);

  // 只处理本任务事件（全局单连接按 taskId 分发）
  const taskMsgs = useMemo(
    () =>
      messages.filter((m) => {
        const p = typeof m.payload === 'object' && m.payload !== null ? m.payload : {};
        const ptid = (p as Record<string, unknown>).task_id as string | undefined;
        return m.task_id === taskId || ptid === taskId;
      }),
    [messages, taskId],
  );

  // 事件 → 节点/审批卡增量（复用 P2 对账逻辑）
  useEffect(() => {
    if (taskMsgs.length === 0) return;
    const last = taskMsgs[taskMsgs.length - 1];
    const p = typeof last.payload === 'object' && last.payload !== null ? last.payload : {};
    if (last.kind === 'task_node_update') {
      setNodes((prev) => ({
        ...prev,
        [p.node_name as string]: {
          node_name: p.node_name as string,
          status: p.status as TaskNodeStatus,
          error: (p.error as string | undefined) ?? null,
          id: (p.node_id as string | undefined) ?? prev[p.node_name as string]?.id,
        },
      }));
    }
    if (last.kind === 'review_event') {
      const rp = p as unknown as ReviewEventPayload;
      if (rp.status === 'awaiting_approval') setAwaiting({ task_id: taskId, kind: 'approval' });
      else if (rp.status === 'asking') setAwaiting({ task_id: taskId, kind: 'ask', q: rp.question });
      else if (rp.status === 'approved' || rp.verdict === 'pass') setAwaiting(null);
    }
    if (last.kind === 'task_update' && (p.event as string) === 'enqueued') {
      void reconcile();
    }
  }, [taskMsgs, taskId, reconcile]);

  const handleNewTask = () => send('请开始');

  // P5：任务状态就绪后拉取产物列表（只读 → can_view；401 → 登出）
  useEffect(() => {
    if (!loaded) return;
    let cancelled = false;
    setArtLoading(true);
    api
      .artifactList(taskId)
      .then((d) => {
        if (!cancelled) setArtifacts(d.keys);
      })
      .catch((err) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 401) {
          onAuthLost();
          return;
        }
        setArtError(err instanceof Error ? err.message : '产物加载失败');
      })
      .finally(() => {
        if (!cancelled) setArtLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [loaded, taskId, onAuthLost, artVer]);

  const downloadArtifact = async (key: string) => {
    const rel = artifactRel(key);
    setArtError(null);
    try {
      // P5-6 另存为：FS Access API 可用时直写磁盘，不用整包进内存；否则内存路径
      if (streamToDiskCapable()) {
        const r = await streamToDisk(taskId, rel, {});
        if (r.ok || r.reason !== 'unsupported') return; // cancelled/ok 均结束；unsupported 才回退
      }
      const blob = await downloadArtifactResumable(taskId, rel);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = rel.split('/').pop() || 'artifact';
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        onAuthLost();
        return;
      }
      setArtError(err instanceof Error ? err.message : '下载失败');
    }
  };

  // P5-5 上传附件（断点续传；上传后刷新产物列表）
  const handleUpload = async (file: File) => {
    setArtError(null);
    try {
      await uploadArtifact(taskId, file.name, file, {});
      setArtVer((v) => v + 1);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) { onAuthLost(); return; }
      setArtError(err instanceof Error ? err.message : '上传失败');
    }
  };

  return (
    <div style={{ maxWidth: 720, margin: '0 auto', padding: 24 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 12 }}>
        <button onClick={onBack} style={s.btnGhost} className={cls.ghost}>← 返回</button>
        <h1 style={{ fontSize: 18, margin: 0, flex: 1 }}>任务 {taskId.slice(0, 8)}…</h1>
        <span style={{ fontSize: 12, color: '#6b7280' }}>WS:{wsStatus}</span>
      </div>

      <NodeStrip nodes={nodes} />

      {awaiting?.kind === 'approval' && (
        <ApprovalCard taskId={awaiting.task_id} question={awaiting.q} onDecision={sendDecision} />
      )}
      {awaiting?.kind === 'ask' && (
        <AskCard taskId={awaiting.task_id} question={awaiting.q} onDecision={sendDecision} />
      )}

      <div style={{ minHeight: 200, border: '1px solid #e5e7eb', borderRadius: 8, padding: 12, marginBottom: 16 }}>
        {!loaded && <div style={s.muted}>从数据库重建任务状态…</div>}
        {loaded &&
          taskMsgs.map((m) => {
            if (m.kind === 'agent_message' && typeof m.payload === 'object' && m.payload !== null) {
              return <ArtifactMessage key={m.msg_id} p={m.payload as unknown as AgentMessagePayload} />;
            }
            return <MessageRow key={m.msg_id} msg={m} />;
          })}
        {loaded && taskMsgs.length === 0 && <div style={s.muted}>暂无消息。</div>}
      </div>

      {/* P5 产物面板：跨节点/前端经 /artifacts 下载 */}
      <div style={{ ...s.card, borderLeft: '3px solid #16a34a' }}>
        <div style={{ fontWeight: 600, marginBottom: 8, fontSize: 14 }}>产物</div>
        <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6, marginBottom: 8, cursor: 'pointer' }}>
          <input type="file" hidden onChange={(e) => { const f = e.target.files?.[0]; if (f) void handleUpload(f); e.target.value = ''; }} />
          <span style={s.btnGhost} className={cls.ghost}>上传附件</span>
        </label>
        {artLoading && <div style={s.muted}>加载产物列表…</div>}
        {artError && <div style={{ color: '#dc2626', fontSize: 13, marginBottom: 8 }}>{artError}</div>}
        {!artLoading && artifacts.length === 0 && !artError && (
          <div style={s.muted}>暂无产物。</div>
        )}
        {artifacts.map((key) => {
          const rel = artifactRel(key);
          const canPrev = previewable(rel);
          return (
            <div
              key={key}
              style={{
                display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8,
                padding: '6px 0', borderBottom: '1px solid #f3f4f6', fontSize: 13,
              }}
            >
              <span style={{ color: '#111827', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{rel}</span>
              <span style={{ display: 'flex', gap: 6, flexShrink: 0 }}>
                {canPrev && (
                  <button onClick={() => setPreview({ task_id: taskId, rel })} style={s.btnGhost} className={cls.ghost}>预览</button>
                )}
                <button onClick={() => void downloadArtifact(key)} style={s.btnGhost} className={cls.ghost}>下载</button>
              </span>
            </div>
          );
        })}
      </div>

      {preview && (
        <ArtifactPreview
          taskId={preview.task_id}
          rel={preview.rel}
          onClose={() => setPreview(null)}
          onAuthLost={onAuthLost}
        />
      )}

      <button onClick={handleNewTask} style={s.btnPrimary} className={cls.primary}>
        进入任务台（新建任务）
      </button>
    </div>
  );
}

function NodeStrip({ nodes }: { nodes: Record<string, NodeInfo> }) {
  const entries = Object.entries(nodes);
  if (entries.length === 0) return <div style={s.muted}>尚无工作流节点。</div>;
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 12 }}>
      {entries.map(([name, n]) => {
        const meta = STATUS_META[n.status] ?? STATUS_META.pending;
        return (
          <div key={name} style={{ border: '1px solid #e5e7eb', borderRadius: 6, padding: '4px 10px', fontSize: 13, display: 'flex', gap: 6, alignItems: 'center' }}>
            <span style={{ width: 8, height: 8, borderRadius: '50%', background: meta.color, flexShrink: 0 }} />
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
  taskId: string; question?: string; onDecision: (taskId: string, d: HumanDecision) => void;
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
      <div style={{ fontWeight: 600, marginBottom: 6 }}>审批：{taskId.slice(0, 8)}…</div>
      {question && <div style={{ fontSize: 13, color: '#374151', marginBottom: 8 }}>{question}</div>}
      <input value={comment} onChange={(e) => setComment(e.target.value)} placeholder="意见（可选，驳回时建议填写）" style={{ ...s.input, marginBottom: 8, display: 'block', width: '100%', boxSizing: 'border-box' }} className={cls.input} />
      <div style={{ display: 'flex', gap: 8 }}>
        <button onClick={() => decide(true)} disabled={busy} style={s.btnPrimary} className={cls.primary}>通过</button>
        <button onClick={() => decide(false)} disabled={busy} style={s.btnDanger} className={cls.danger}>驳回</button>
      </div>
    </div>
  );
}

function AskCard({
  taskId, question, onDecision,
}: {
  taskId: string; question?: string; onDecision: (taskId: string, d: HumanDecision) => void;
}) {
  const [text, setText] = useState('');
  const submit = () => {
    if (!text.trim()) return;
    onDecision(taskId, { kind: 'answer', text: text.trim() });
    setText('');
  };
  return (
    <div style={s.card}>
      <div style={{ fontWeight: 600, marginBottom: 6 }}>需补充信息：{taskId.slice(0, 8)}…</div>
      {question && <div style={{ fontSize: 13, color: '#374151', marginBottom: 8 }}>{question}</div>}
      <input value={text} onChange={(e) => setText(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && submit()} placeholder="补充信息" style={{ ...s.input, marginBottom: 8, display: 'block', width: '100%', boxSizing: 'border-box' }} className={cls.input} />
      <button onClick={submit} style={s.btnPrimary} className={cls.primary}>提交</button>
    </div>
  );
}

function ArtifactMessage({ p }: { p: AgentMessagePayload }) {
  return (
    <div style={{ ...s.card, borderLeft: '3px solid #2563eb' }}>
      <div style={{ fontSize: 12, color: '#6b7280', marginBottom: 4 }}>[{p.node_name} · {p.role}]</div>
      <div style={{ whiteSpace: 'pre-wrap', fontSize: 14, color: '#111827' }}>{p.text}</div>
    </div>
  );
}

function MessageRow({ msg }: { msg: WsMessage }) {
  const isNodeKind = ['task_node_update', 'review_event', 'task_update'].includes(msg.kind);
  if (isNodeKind) return null;
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
  card: { ...t.card().style, marginBottom: space[3] },
  input: t.input().style,
  btnPrimary: t.btnPrimary().style,
  btnDanger: {
    padding: `${space[2]}px ${space[4]}px`, borderRadius: 6, border: `1px solid ${colors.dangerLine}`,
    background: colors.white, color: colors.danger, cursor: 'pointer', fontFamily: 'inherit',
  },
  btnGhost: t.btnGhost().style,
  muted: { color: colors.gray, fontSize: 13, lineHeight: 1.5 },
};

/* 交互态类名（对应 tokens.css 的 .zf-* ） */
const cls = {
  primary: 'zf-btn zf-btn-primary',
  ghost: 'zf-btn zf-btn-ghost',
  danger: 'zf-btn zf-btn-ghost',
  input: 'zf-input',
};
