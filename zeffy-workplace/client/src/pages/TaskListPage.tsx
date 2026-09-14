import { useEffect, useState } from 'react';
import { api, ApiError, TaskDTO } from '../auth';

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

  return (
    <div style={{ maxWidth: 720, margin: '0 auto', padding: 24 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <h1 style={{ fontSize: 20, margin: 0 }}>我的任务</h1>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <button
            onClick={() => location.hash = '#/'}
            disabled
            style={{ ...s.btnGhost, opacity: 0.5 }}
          >
            进入任务台
          </button>
          <button onClick={logout} style={s.btnGhost}>退出登录</button>
        </div>
      </div>

      {loading && <div style={s.muted}>加载中…</div>}
      {error && <div style={{ color: '#dc2626', fontSize: 13, marginBottom: 12 }}>{error}</div>}

      {!loading && tasks && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {tasks.length === 0 && <div style={s.muted}>暂无任务。</div>}
          {tasks.map((t) => (
            <a
              key={t.id}
              href={`#/tasks/${encodeURIComponent(t.id)}`}
              style={{
                display: 'flex', justifyContent: 'space-between', gap: 12, alignItems: 'center',
                border: '1px solid #e5e7eb', borderRadius: 8, padding: '12px 14px',
                textDecoration: 'none', color: '#111827', background: '#fff',
              }}
            >
              <div style={{ minWidth: 0 }}>
                <div style={{ fontWeight: 600, fontSize: 14, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {t.title}
                </div>
                <div style={{ fontSize: 12, color: '#6b7280', marginTop: 2 }}>
                  {t.id.slice(0, 8)}… · {new Date(t.created_at).toLocaleString()}
                </div>
              </div>
              <span
                style={{
                  fontSize: 12, padding: '2px 8px', borderRadius: 999,
                  background: t.status === 'done' ? '#22c55e19' : '#e5e7eb',
                  color: t.status === 'done' ? '#16a34a' : '#374151', whiteSpace: 'nowrap',
                }}
              >
                {STATUS_LABEL[t.status] ?? t.status}
              </span>
            </a>
          ))}
        </div>
      )}
    </div>
  );
}

const s: Record<string, React.CSSProperties> = {
  muted: { color: '#6b7280', fontSize: 13 },
  btnGhost: { padding: '6px 12px', borderRadius: 6, border: '1px solid #d1d5db', background: '#fff', color: '#374151', cursor: 'pointer', fontSize: 13 },
};