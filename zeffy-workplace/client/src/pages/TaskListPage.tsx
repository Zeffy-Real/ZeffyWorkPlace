import { useEffect, useMemo, useState } from 'react';
import { ApiError, api, TaskDTO } from '../auth';
import { GovernancePanel } from '../components/GovernancePanel';
import { EmptyState } from '../components/ui/EmptyState';
import { Skeleton } from '../components/ui/Skeleton';
import { Icon } from '../components/ui/Icon';
import { colors, radius, space, t, type as T } from '../theme';

type Filter = 'all' | 'running' | 'pending' | 'done' | 'failed';
const FILTERS: Array<{ value: Filter; label: string }> = [
  { value: 'all', label: '全部' },
  { value: 'running', label: '进行中' },
  { value: 'pending', label: '待处理' },
  { value: 'done', label: '已完成' },
  { value: 'failed', label: '失败' },
];

const STATUS_LABEL: Record<string, string> = {
  pending: '待处理', running: '执行中', queued: '排队中', done: '已完成',
  failed: '失败', blocked: '待审批', interrupt: '待审批',
};

const ONBOARD_KEY = 'zf_onboard_dismiss';

function inStatus(t: TaskDTO): Filter[] {
  if (t.status === 'running' || t.status === 'queued') return ['running'];
  if (t.status === 'blocked' || t.status === 'interrupt') return ['pending'];
  if (t.status === 'done') return ['done'];
  if (t.status === 'failed') return ['failed'];
  return ['pending'];
}

export function TaskListPage({
  canCreate,
  onNewTask,
  onAuthLost,
}: {
  canCreate: boolean;
  onNewTask: () => void;
  onAuthLost: () => void;
}) {
  const [tasks, setTasks] = useState<TaskDTO[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState<Filter>('all');
  const [onboardDismissed, setOnboardDismissed] = useState(
    () => localStorage.getItem(ONBOARD_KEY) === '1',
  );

  const reload = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.listTasks();
      setTasks(data.items);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        onAuthLost();
        return;
      }
      setError(err instanceof Error ? err.message : '任务加载失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void reload();
  }, []);

  const dismissOnboard = () => {
    localStorage.setItem(ONBOARD_KEY, '1');
    setOnboardDismissed(true);
  };

  // 总览统计：由单次 /tasks 聚合，不额外发请求
  const stats = useMemo(() => {
    const list = tasks ?? [];
    const count = (pred: (t: TaskDTO) => boolean) => list.filter(pred).length;
    return {
      running: count((t) => inStatus(t).includes('running')),
      pending: count((t) => inStatus(t).includes('pending')),
      done: count((t) => inStatus(t).includes('done')),
      failed: count((t) => inStatus(t).includes('failed')),
    };
  }, [tasks]);

  const filtered = useMemo(() => {
    const list = tasks ?? [];
    if (filter === 'all') return list;
    return list.filter((t) => inStatus(t).includes(filter));
  }, [tasks, filter]);

  const statStyle = t.stat();
  const primary = t.btnPrimary().style;

  return (
    <div style={{ maxWidth: 860, margin: '0 auto', padding: `${space[6]}px ${space[5]}px` }}>
      {/* 欢迎头 */}
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: space[3], marginBottom: space[6], flexWrap: 'wrap' }}>
        <span style={{ color: colors.accent, display: 'inline-flex', marginTop: 2 }} aria-hidden="true">
          <Icon name="spark" size={30} />
        </span>
        <div style={{ flex: 1, minWidth: 220 }}>
          <h1 style={{ margin: 0, fontSize: T.titleM, fontWeight: 600, color: colors.ink, lineHeight: T.lhTitle }}>
            让 Agent 帮你干活
          </h1>
          <p style={{ margin: `${space[1]}px 0 0`, color: colors.gray, fontSize: T.body, lineHeight: T.lhBody, maxWidth: 520 }}>
            一句话发起任务，系统自动拆解并交给多个协作 Agent 执行、评审，完成后你来审批、下载成果。
          </p>
        </div>
      </div>

      {/* 总览统计行（单次 /tasks 聚合） */}
      {!loading && tasks && (
        <div
          style={{
            display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))',
            gap: space[2], marginBottom: space[6],
          }}
        >
          {(
              [
                { key: 'running' as Filter, label: '进行中', value: stats.running, color: colors.accent },
                { key: 'pending' as Filter, label: '待处理', value: stats.pending, color: colors.mist },
                { key: 'done' as Filter, label: '已完成', value: stats.done, color: colors.ok },
                { key: 'failed' as Filter, label: '失败', value: stats.failed, color: colors.danger },
              ]
            ).map((s) => {
            const active = filter === s.key;
            return (
              <button
                key={s.key}
                onClick={() => setFilter(active ? 'all' : s.key)}
                aria-pressed={active}
                className={statStyle.className}
                style={{ ...statStyle.style, display: 'flex', alignItems: 'baseline', gap: space[2] }}
              >
                <span style={{ fontSize: 26, fontWeight: 600, color: s.color, lineHeight: 1 }}>{s.value}</span>
                <span style={{ color: colors.gray, fontSize: T.helper }}>{s.label}</span>
              </button>
            );
          })}
        </div>
      )}

      {/* 筛选 Tab */}
      <div role="tablist" aria-label="按状态筛选任务" style={{ display: 'flex', gap: space[2], flexWrap: 'wrap', marginBottom: space[4] }}>
        {FILTERS.map((f) => {
          const tab = t.tab(filter === f.value);
          return (
            <button
              key={f.value}
              role="tab"
              aria-selected={filter === f.value}
              onClick={() => setFilter(f.value)}
              disabled={loading}
              className={tab.className}
              style={tab.style}
            >
              {f.label}
            </button>
          );
        })}
      </div>

      {/* 加载 / 错误 / 空 / 列表 */}
      {loading && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: space[3] }}>
          <Skeleton lines={2} />
        </div>
      )}

      {!loading && error && (
        <EmptyState
          icon="warning"
          title="任务加载失败"
          description={error}
          action={
            <button onClick={() => void reload()} className="zf-btn zf-btn-ghost" style={t.btnGhost().style}>
              重试
            </button>
          }
        />
      )}

      {!loading && !error && tasks && filtered.length === 0 && (
        filter === 'all' && !onboardDismissed ? (
          <EmptyState
            icon="spark"
            title="从一个小任务开始"
            description="还没有任务。点「新建任务」，用一句话描述你想要的成果（如一份报告、一段代码、一篇总结），剩下的交给 Agent。分三步：新建 → 查看执行 → 审批并下载产物。"
            action={
              <button
                onClick={canCreate ? onNewTask : undefined}
                disabled={!canCreate}
                className="zf-btn zf-btn-primary"
                style={primary}
              >
                {canCreate ? '新建第一个任务' : '登录后即可新建任务'}
              </button>
            }
          />
        ) : (
          <EmptyState
            icon="folder"
            title="没有匹配的任务"
            description="当前筛选下没有任务，换个状态看看。"
            action={
              <button onClick={() => setFilter('all')} className="zf-btn zf-btn-ghost" style={t.btnGhost().style}>
                查看全部
              </button>
            }
          />
        )
      )}

      {!loading && !error && tasks && filtered.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: space[2] }}>
          {filter === 'all' && !onboardDismissed && tasks.length > 0 && (
            <button
              onClick={dismissOnboard}
              style={{ textAlign: 'right', border: 'none', background: 'none', color: colors.gray, fontSize: T.hint, cursor: 'pointer' }}
            >
              知道了，不再显示上手引导
            </button>
          )}
          {filtered.map((t2) => {
            const done = t2.status === 'done';
            return (
              <a
                key={t2.id}
                href={`#/tasks/${encodeURIComponent(t2.id)}`}
                className="zf-card"
                style={{
                  display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: space[3],
                  border: `1px solid ${colors.line}`, borderRadius: radius.card, padding: `${space[3]}px ${space[4]}px`,
                  textDecoration: 'none', color: colors.ink, background: colors.white,
                }}
              >
                <div style={{ minWidth: 0, display: 'flex', flexDirection: 'column', gap: 2, flex: 1 }}>
                  <span style={{ fontWeight: 600, fontSize: T.body, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {t2.title || '（未命名任务）'}
                  </span>
                  <span style={{ fontSize: T.hint, color: colors.gray }}>
                    {t2.id.slice(0, 8)}… · {new Date(t2.created_at).toLocaleString()}
                  </span>
                </div>
                <span
                  style={{
                    fontSize: T.helper, padding: '2px 8px', borderRadius: 6, whiteSpace: 'nowrap',
                    color: done ? colors.ok : colors.slate, background: done ? `${colors.ok}1a` : colors.fill,
                  }}
                >
                  {STATUS_LABEL[t2.status] ?? t2.status}
                </span>
              </a>
            );
          })}
        </div>
      )}

      {/* 治理面板 */}
      <div style={{ marginTop: space[6] }}>
        <GovernancePanel />
      </div>
    </div>
  );
}