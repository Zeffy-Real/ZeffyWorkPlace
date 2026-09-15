import { useEffect, useMemo, useState } from 'react';
import {
  ApiError,
  AuditItemDTO,
  GovAdminStatusDTO,
  GovAlertItemDTO,
  api,
  GovernanceStatsDTO,
  QuotaReportDTO,
  RecycleItemDTO,
} from '../auth';
import { GovTrendChart } from './GovTrendChart';

const GB = 1024 * 1024 * 1024;
const MB = 1024 * 1024;

function fmtB(n: number): string {
  if (n >= GB) return `${(n / GB).toFixed(2)} GB`;
  if (n >= MB) return `${(n / MB).toFixed(1)} MB`;
  return `${Math.round(n)} B`;
}

/** P6/P6-2 治理体验面板：配额进度+趋势预警、冷热占比、回收站、审计列表。
 *  治理未开启（后端 404）/ 未登录 → 静默隐藏；子能力(报表/审计)单独 404 则仅隐藏对应区块。 */
export function GovernancePanel() {
  const [stats, setStats] = useState<GovernanceStatsDTO | null>(null);
  const [recycle, setRecycle] = useState<RecycleItemDTO[]>([]);
  const [report, setReport] = useState<QuotaReportDTO | null>(null);
  const [audit, setAudit] = useState<AuditItemDTO[] | null>(null);
  const [alerts, setAlerts] = useState<GovAlertItemDTO[] | null>(null); // admin-only 告警历史
  const [admin, setAdmin] = useState<GovAdminStatusDTO | null>(null); // admin-only 运维状态（非管理员 404 → null 隐藏）
  const [vis, setVis] = useState(false); // 治理面板是否可用
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

    // 子能力：配额报表（O2） + 审计（O3）——单独 404 只隐藏对应块，不拖垮主面板
    (async () => {
      try {
        const rep = await api.quotaReport();
        if (alive) setReport(rep);
      } catch {
        /* 未开启报表 → 隐藏 */
      }
    })();
    // 治理/系统告警历史（admin-only）：非管理员 404 → null 隐藏
    (async () => {
      try {
        const al = await api.governanceAdminAlerts();
        if (alive) setAlerts(al.items ?? []);
      } catch {
        setAlerts(null);
      }
    })();
    (async () => {
      try {
        const aud = await api.governanceAudit();
        if (alive) setAudit(aud.items);
      } catch {
        /* 未开启审计 → 隐藏 */
      }
    })();
    // 运维状态（admin-only）：非管理员 404 → 静默隐藏
    (async () => {
      try {
        const st = await api.governanceAdminStatus();
        if (alive) setAdmin(st);
      } catch {
        setAdmin(null);
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
  const etaHours = report?.trend?.eta_hours ?? null;
  const trendAlert = report?.trend?.alert ?? null;

  // P6-4-A 健康评分（可解释扣分 + 三色分级）：普通用户=个人维度；admin=全局维度（复用 admin 状态）
  const health = useMemo(() => {
    const drops: Array<{ key: string; label: string; drop: number; reason: string }> = [];
    // 配额使用率：>80 开始扣
    if (ratio !== null && ratio > 80) {
      const drop = Math.min(40, Math.round((ratio - 80) * 1.2));
      drops.push({ key: 'quota', label: '配额使用率', drop, reason: `使用率 ${ratio.toFixed(1)}%，超安全线 80%` });
    }
    const unresolved = (alerts ?? []).filter((a) => a.action === 'governance_alarm_trigger').length;
    if (unresolved > 0) {
      const drop = Math.min(30, unresolved * 10);
      drops.push({ key: 'alarm', label: '未恢复告警', drop, reason: `${unresolved} 条未恢复治警告警` });
    }
    if (admin?.degraded) {
      drops.push({ key: 'degrade', label: '中心化降级', drop: 20, reason: '配置中心 Redis 处于降级状态' });
    }
    if (admin) {
      const bad = Object.values(admin.guardians ?? {}).filter((g) => g.ok === false).length;
      if (bad > 0) drops.push({ key: 'guard', label: '守护失败', drop: Math.min(20, bad * 10), reason: `${bad} 个守护任务运行异常` });
    }
    const score = Math.max(0, 100 - drops.reduce((s, d) => s + d.drop, 0));
    return { score, drops };
  }, [ratio, alerts, admin]);

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
          {etaHours !== null && (
            <div style={{ color: trendAlert === 'high' ? '#dc2626' : '#d97706', marginTop: 4 }}>
              预计 {Math.ceil(etaHours / 24)} 天后耗尽{trendAlert === 'high' ? '（高优先级）' : trendAlert === 'low' ? '（低优先级）' : ''}
            </div>
          )}
        {report && report.cold_eligible && report.cold_eligible.physical_bytes > 0 && (
            <div style={{ marginTop: 4, color: '#9ca3af' }}>
              可冷化释放（物理）：{fmtB(report.cold_eligible.physical_bytes)}
            </div>
          )}
          {report && report.suggested_quota && (
            <div style={{ marginTop: 4, color: '#9ca3af' }}>
              建议配额 {fmtB(report.suggested_quota.quota)}
              {report.suggested_quota.confidence
                ? `（${report.suggested_quota.confidence === 'high' ? '高' : report.suggested_quota.confidence === 'medium' ? '中' : '低'}置信度）`
                : ''}
            </div>
          )}
        </div>
      )}

      {/* P6-4-A 健康评分（可解释扣分 + 三色分级） */}
      {(() => {
        const colr = health.score >= 80 ? '#16a34a' : health.score >= 60 ? '#d97706' : '#dc2626';
        return (
          <div style={{ marginTop: 10 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <span style={{ color: '#9ca3af' }}>治理健康</span>
              <span style={{ color: colr, fontSize: 16, fontWeight: 600 }}>{health.score}</span>
              <span style={{ color: '#6b7280' }}>{health.score >= 80 ? '健康' : health.score >= 60 ? '关注' : '风险'}</span>
            </div>
            {health.drops.length === 0 && <div style={{ color: '#6b7280', fontSize: 12 }}>无扣分项</div>}
            {health.drops.length > 0 && (
              <ul style={{ margin: '4px 0 0', paddingLeft: 18, color: '#d1d5db', fontSize: 12 }}>
                {health.drops.map((d) => (
                  <li key={d.key}>{d.label} -{d.drop}：{d.reason}</li>
                ))}
              </ul>
            )}
          </div>
        );
      })()}

      {/* P6-4-A 分层占比（<5% 并入其他，分母 0 → 空态） */}
      {stats && stats.total_bytes > 0 && (() => {
        const seg: Array<[string, number, string]> = [];
        const push = (label: string, bytes: number, color: string) => {
          if (bytes > 0) seg.push([label, bytes, color]);
        };
        push('热', stats.hot_bytes || 0, '#22c55e');
        push('冷', stats.cold_bytes || 0, '#3b82f6');
        const others = Math.max(0, (stats.total_bytes || 0) - (seg.reduce((s, x) => s + x[1], 0)));
        if (others > 0) seg.push(['其他', others, '#6b7280']);
        // 占比 <5% 的最小显示宽度处理
        const minW = 4;
        const pure = seg.map(([l, b, c]) => [(b / stats.total_bytes) * 100, b, l, c] as [number, number, string, string]);
        const displayed = pure.map(([p]) => (p < 5 && p > 0 ? Math.max(minW, p) : p) as number);
        return (
          <div style={{ marginTop: 10 }}>
            <div style={{ color: '#9ca3af', marginBottom: 4 }}>存储分层（共 {fmtB(stats.total_bytes)}）</div>
            <div style={{ display: 'flex', height: 8, borderRadius: 4, overflow: 'hidden', background: '#2b2f38' }}>
              {displayed.map((w, i) => (
                <div key={pure[i][2]} title={`${pure[i][2]} ${fmtB(pure[i][1])} (${pure[i][0].toFixed(1)}%)`} style={{ width: `${Math.max(0.5, w)}%`, background: pure[i][3] }} />
              ))}
            </div>
            <div style={{ display: 'flex', gap: 12, marginTop: 4, color: '#9ca3af', fontSize: 12 }}>
              {pure.map(([p, b, l, c]) => (
                <span key={l}><i style={{ display: 'inline-block', width: 8, height: 8, borderRadius: 2, background: c as string, marginRight: 4 }} />{l} {fmtB(b)}（{p.toFixed(1)}%）</span>
              ))}
            </div>
          </div>
        );
      })()}

      {/* P6-4-A 配额趋势折线（按实际时间范围；<2 点守卫） */}
      {report && report.history_points && (() => {
        const hp = report.history_points;
        const days = hp.start_ts && hp.end_ts ? Math.max(1, Math.round((hp.end_ts - hp.start_ts) / 86400000)) : 0;
        return (
          <div style={{ marginTop: 10 }}>
            <div style={{ color: '#9ca3af', marginBottom: 4 }}>
              配额趋势{days ? `（近 ${days} 天）` : ''}{hp.downsampled ? ' · 已降采样' : ''}
            </div>
            <GovTrendChart points={hp.points} unit={fmtB} />
            {hp.points.length > 0 && hp.points.length < 2 && (
              <div style={{ color: '#d97706', fontSize: 12, marginTop: 2 }}>
                数据不足，仅 {hp.points.length} 条采样
              </div>
            )}
          </div>
        );
      })()}

      {report && report.suggestions.length > 0 && (
        <div style={{ marginTop: 10 }}>
          <div style={{ color: '#9ca3af', marginBottom: 4 }}>建议清理（{report.suggestions.length} 项）</div>
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {report.suggestions.slice(0, 5).map((sg, i) => (
              <li key={`${sg.task_id}/${sg.rel_path}-${i}`} style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
                <span style={{ color: '#6b7280' }}>{sg.tier === 'cold' ? '冷' : sg.status === 'deleted' ? '回收站' : sg.tier}</span>
                <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{sg.rel_path}</span>
                <span style={{ color: '#9ca3af' }}>{fmtB(sg.size)}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {recycle.length > 0 && (
        <div style={{ marginTop: 10 }}>
          <div style={{ color: '#9ca3af', marginBottom: 4 }}>回收站（{recycle.length} 项，仍占用配额，恢复后释放）</div>
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

      {audit && audit.length > 0 && (
        <div style={{ marginTop: 10 }}>
          <div style={{ color: '#9ca3af', marginBottom: 4 }}>最近治理操作（{audit.length} 条）</div>
          <ul style={{ margin: 0, paddingLeft: 18, maxHeight: 160, overflowY: 'auto' }}>
            {audit.map((a) => (
              <li key={a.id} style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 3 }}>
                <span style={{ color: '#6b7280', flex: '0 0 auto' }}>{a.action}</span>
                <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {a.detail && typeof a.detail.key === 'string' ? a.detail.key : ''}
                </span>
                {a.created_at && <span style={{ color: '#6b7280', marginLeft: 'auto' }}>{new Date(a.created_at).toLocaleTimeString()}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}

      {admin && (
        <div style={{ marginTop: 12, borderTop: '1px solid #2b2f38', paddingTop: 10 }}>
          <div style={{ color: '#9ca3af', marginBottom: 4, display: 'flex', alignItems: 'center', gap: 8 }}>
            <span>运维状态</span>
            {admin.single_instance_only && (
              <span style={{ color: '#d97706', fontSize: 12 }}>单实例（覆盖/灰度仅本实例生效）</span>
            )}
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 6 }}>
            {(admin.features ?? []).map((f) => (
              <span
                key={f.name}
                style={{
                  background: f.effective ? '#1f2937' : '#3a1416',
                  border: `1px solid ${f.effective ? '#374151' : '#7f1d1d'}`,
                  color: f.effective ? '#9fd0a4' : '#f0a4a4',
                  borderRadius: 6, padding: '1px 8px', fontSize: 12,
                }}
                title={`${f.config_attr}=${f.config_default}${f.overridden ? '（已覆盖）' : ''}${f.gray_gated ? ` 灰度(${f.gray_members?.length ?? 0})` : ''}`}
              >
                {f.name}{f.overridden ? ' *' : ''}{f.gray_gated ? ' 灰' : ''}
              </span>
            ))}
          </div>
          <div style={{ color: '#6b7280', fontSize: 12 }}>
            守护：{Object.entries(admin.guardians ?? {}).map(([name, g]) => (
              <span
                key={name}
                title={g.error ? `错误:${g.error}` : ''}
                style={{ marginRight: 10, color: g.ok === false ? '#f0a4a4' : '#9ca3af' }}
              >
                {name}
                <span style={{ marginLeft: 3 }}>
                  {g.ok === true ? '运行正常' : g.ok === false ? '异常' : g.running ? '执行中' : '-'}
                </span>
              </span>
            ))}
          </div>
        </div>
      )}

      {/* P6-4-A 治理/系统告警历史（admin-only；未恢复置顶 + 级别分组） */}
      {alerts && (
        <div style={{ marginTop: 12, borderTop: '1px solid #2b2f38', paddingTop: 10 }}>
          <div style={{ color: '#9ca3af', marginBottom: 4 }}>告警历史（{alerts.length} 条 / 30 天）</div>
          {alerts.length === 0 && <div style={{ color: '#6b7280', fontSize: 12 }}>暂无告警</div>}
          {alerts.length > 0 && (
            <ul style={{ margin: 0, paddingLeft: 16, maxHeight: 200, overflowY: 'auto' }}>
              {[...alerts]
                .sort((a, b) =>
                  (a.action === 'governance_alarm_trigger' ? -1 : 1) -
                  (b.action === 'governance_alarm_trigger' ? -1 : 1))
                .map((a) => {
                  const d = a.detail || {};
                  const trigger = a.action === 'governance_alarm_trigger';
                  return (
                    <li key={a.id} style={{ marginBottom: 6, fontSize: 12 }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                        <span style={{ color: trigger ? '#f0a4a4' : '#9fd0a4' }}>
                          {d.metric || 'alarm'} · {trigger ? '触发' : '恢复'}
                          {d.level ? ` · ${d.level}` : ''}
                        </span>
                        {a.created_at && <span style={{ color: '#6b7280', marginLeft: 'auto' }}>{new Date(a.created_at).toLocaleString()}</span>}
                      </div>
                      {d.current !== undefined && (
                        <div style={{ color: '#9ca3af' }}>当前 {d.current}
                          {d.threshold !== undefined && d.threshold !== null ? `（阈值 ${d.threshold}）` : ''}
                        </div>
                      )}
                    </li>
                  );
                })}
            </ul>
          )}
        </div>
      )}

      {notify && <div style={{ marginTop: 6, color: '#16a34a' }}>{notify}</div>}
    </section>
  );
}