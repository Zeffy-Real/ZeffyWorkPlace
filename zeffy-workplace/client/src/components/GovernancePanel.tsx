import { useEffect, useMemo, useState } from 'react';
import {
  ApiError,
  api,
  GovernanceStatsDTO,
  RecycleItemDTO,
} from '../auth';

const GB = 1024 * 1024 * 1024;
const MB = 1024 * 1024;

function fmtB(n: number): string {
  if (n >= GB) return `${(n / GB).toFixed(2)} GB`;
  if (n >= MB) return `${(n / MB).toFixed(1)} MB`;
  return `${Math.round(n)} B`;
}

/** P6 治理体验面板：配额使用进度 + 分级预警、冷热占比、回收站恢复。
 *  治理未开启（后端 404）/ 未登录 → 静默隐藏，零打扰。 */
export function GovernancePanel() {
  const [stats, setStats] = useState<GovernanceStatsDTO | null>(null);
  const [recycle, setRecycle] = useState<RecycleItemDTO[]>([]);
  const [vis, setVis] = useState(false); // 治理是否可用（探测过）
  const [notify, setNotify] = useState('');
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const [st, rec] = await Promise.all([
          api.governanceStats(),
          api.recycleList(),
        ]);
        if (!alive) return;
        setStats(st);
        setRecycle(rec.items);
        setVis(true);
      } catch (e) {
        if (!alive) return;
        if (e instanceof ApiError && e.status === 404) return; // 治理未开启 → 隐藏
        // 其他错误静默（匿名无 owner 等）
      }
    })();
    return () => {
      alive = false;
    };
  }, [reloadKey]);

  const ratio = useMemo(() => {
    if (!stats) return null;
    const total = stats.quota_total;
    if (!total || total <= 0) return null;
    return (stats.quota_used / total) * 100;
  }, [stats]);
  const warn = ratio === null ? null : ratio > 90 ? 'danger' : ratio > 70 ? 'warn' : 'ok';

  if (!vis) return null;

  return (
    <section style={{ border: '1px solid #3a3f4b', borderRadius: 10, padding: '12px 14px', marginBottom: 16, fontSize: 13 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
        <strong style={{ color: '#e5e7eb' }}>产物治理</strong>
        {stats && (
          <span style={{ color: '#9ca3af' }}>
            用量 {fmtB(stats.total_bytes)}（{stats.count} 项，热 {fmtB(stats.hot_bytes)} · 冷 {fmtB(stats.cold_bytes)}）
          </span>
        )}
      </div>
      {ratio !== null && (
        <div style={{ marginTop: 8 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
            <span>配额使用率</span>
            <span style={{ color: warn === 'danger' ? '#dc2626' : warn === 'warn' ? '#d97706' : '#16a34a' }}>
              {ratio.toFixed(1)}%
            </span>
          </div>
          <div style={{ height: 8, background: '#2b2f38', borderRadius: 4, overflow: 'hidden' }}>
            <div
              style={{
                width: `${Math.min(100, ratio)}%`,
                height: '100%',
                background: warn === 'danger' ? '#dc2626' : warn === 'warn' ? '#d97706' : '#22c55e',
              }}
            />
          </div>
          {warn === 'danger' && <div style={{ color: '#dc2626', marginTop: 4 }}>配额占用超 90%，请尽快清理或扩容</div>}
          {warn === 'warn' && <div style={{ color: '#d97706', marginTop: 4 }}>配额占用超 70%，建议清理低频冷数据</div>}
        </div>
      )}

      {recycle.length > 0 && (
        <div style={{ marginTop: 10 }}>
          <div style={{ color: '#9ca3af', marginBottom: 4 }}>回收站（{recycle.length} 项，仍占用配额，恢复/删除后释放）</div>
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {recycle.map((r) => (
              <li key={r.id} style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
                <span style={{ color: '#6b7280' }}>{r.tier}</span>
                <span>{r.rel_path}</span>
                <span style={{ color: '#9ca3af' }}>{fmtB(r.size)}</span>
                <button
                  onClick={async () => {
                    try {
                      await api.recyclePost('restore', r.task_id || '', r.rel_path);
                      setNotify('已恢复 ' + r.rel_path);
                    } catch (e) {
                      setNotify('恢复失败：' + (e instanceof ApiError ? e.message : String(e)));
                    }
                    setReloadKey((k) => k + 1);
                  }}
                  style={{ marginLeft: 'auto', background: '#1f2937', border: '1px solid #374151', color: '#e5e7eb', borderRadius: 6, padding: '2px 8px', cursor: 'pointer' }}
                >
                  恢复
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
      {notify && <div style={{ marginTop: 6, color: '#16a34a' }}>{notify}</div>}
    </section>
  );
}