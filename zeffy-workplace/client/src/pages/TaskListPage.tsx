import { useEffect, useState } from 'react';
import { api, ApiError, TaskDTO } from '../auth';
import { GovernancePanel } from '../components/GovernancePanel';
import { colors, radius, t } from '../theme';

const STATUS_LABEL: Record<string, string> = {
  pending: '待处理',
  running: '执行中',
  queued: '排队中',
  done: '已完成',
  failed: '失败',
  blocked: '待人工',
};

export function TaskListPage({ onLoggedOut }: { onLoggedOut: () => void }) {
  const [tasks, setTasks] = useState<TaskDTO[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const reload = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.listTasks();
      setTasks(data.items);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        onLoggedOut();
        return;
      }
      setError(err instanceof Error ? err.message : '加载失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const logout = async () => {
    try {
      await api.logout();
    } catch {
      // ignore
    }
    localStorage.removeItem('zw_token');
    onLoggedOut();
  };

  const page = t.page();
  const muted = t.muted();
  const ghost = t.btnGhost();
  const tagOk = t.tag();
  const tagDef = t.tag();

  return (
    <div style={page.style} className={page.className}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <h1 style={{ fontSize: 20, margin: 0, color: colors.ink, lineHeight: 1.3 }}>我的任务</h1>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <button
            onClick={() => location.hash = '#/'}
            disabled
            style={{ ...ghost.style, opacity: 0.5 }}
            className={ghost.className}
          >
            进入任务台
          </button>
          <button onClick={logout} style={ghost.style} className={ghost.className}>退出登录</button>
        </div>
      </div>

      <GovernancePanel />

      {loading && <div style={muted.style}>加载中…</div>}
      {error && <div style={t.alertError().style} role="alert">{error}</div>}

      {!loading && tasks && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {tasks.length === 0 && <div style={muted.style}>暂无任务。</div>}
          {tasks.map((t2) => {
            const done = t2.status === 'done';
            return (
              <a
                key={t2.id}
                href={`#/tasks/${encodeURIComponent(t2.id)}`}
                className="zf-card"
                style={{
                  display: 'flex', justifyContent: 'space-between', gap: 12, alignItems: 'center',
                  border: `1px solid ${colors.line}`, borderRadius: radius.card, padding: '12px 14px',
                  textDecoration: 'none', color: colors.ink, background: colors.white,
                }}
              >
                <div style={{ minWidth: 0 }}>
                  <div style={{ fontWeight: 600, fontSize: 14, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {t2.title}
                  </div>
                  <div style={{ fontSize: 12, color: colors.gray, marginTop: 2 }}>
                    {t2.id.slice(0, 8)}… · {new Date(t2.created_at).toLocaleString()}
                  </div>
                </div>
                <span
                  style={{
                    ...(done ? tagOk.style : tagDef.style),
                    fontSize: 12, padding: '2px 8px', color: done ? colors.ok : colors.slate,
                    background: done ? '#22c55e1a' : colors.fill,
                  }}
                >
                  {STATUS_LABEL[t2.status] ?? t2.status}
                </span>
              </a>
            );
          })}
        </div>
      )}
    </div>
  );
}