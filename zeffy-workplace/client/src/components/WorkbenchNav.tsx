import { colors, space, t, type as T } from '../theme';
import { Icon } from './ui/Icon';

/* 统一工作台导航壳：品牌 + WS 状态 + 新建任务 + 帮助 + 退出。首页/详情共用。 */
export function WorkbenchNav({
  wsStatus,
  userLabel,
  canCreate,
  onNewTask,
  onHelp,
  onLogout,
}: {
  wsStatus: 'connecting' | 'open' | 'closed';
  userLabel?: string;
  canCreate: boolean;
  onNewTask: () => void;
  onHelp: () => void;
  onLogout: () => void;
}) {
  const ws = {
    connecting: { color: colors.mist, label: '连接中' },
    open: { color: colors.ok, label: '已连接' },
    closed: { color: colors.danger, label: '已断开' },
  }[wsStatus];

  const ghost = t.btnGhost().style;

  return (
    <header
      style={{
        display: 'flex', alignItems: 'center', gap: space[2],
        padding: `0 ${space[6]}px`, height: 56,
        borderBottom: `1px solid ${colors.line}`, background: colors.white,
        position: 'sticky', top: 0, zIndex: 40,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: space[2], marginRight: 'auto' }}>
        <span style={{ color: colors.accent, display: 'inline-flex' }} aria-hidden="true">
          <Icon name="spark" size={20} />
        </span>
        <strong style={{ fontSize: T.body, color: colors.ink }}>Zeffy 工作台</strong>
        <span
          title="Agent 协作实时连接"
          style={{ display: 'inline-flex', alignItems: 'center', gap: 5, color: colors.gray, fontSize: T.hint }}
        >
          <span style={{ width: 8, height: 8, borderRadius: 999, background: ws.color, display: 'inline-block' }} aria-hidden="true" />
          <span>{ws.label}</span>
        </span>
      </div>

      {userLabel && (
        <span style={{ color: colors.gray, fontSize: T.helper, marginRight: space[1], whiteSpace: 'nowrap' }}>
          你好，{userLabel}
        </span>
      )}

      <button
        onClick={onNewTask}
        disabled={!canCreate}
        title={canCreate ? '发起一个新任务' : '请先登录后再新建任务'}
        className="zf-btn zf-btn-primary"
        style={{ ...t.btnPrimary().style, display: 'inline-flex', alignItems: 'center', gap: 6 }}
      >
        <Icon name="plus" size={16} />
        <span>新任务</span>
      </button>
      <button
        onClick={onHelp}
        aria-label="使用指引"
        title="使用指引"
        className="zf-btn zf-btn-ghost"
        style={{ ...ghost, display: 'inline-flex', alignItems: 'center', padding: 8 }}
      >
        <Icon name="help" size={18} />
      </button>
      <button
        onClick={onLogout}
        aria-label="退出登录"
        title="退出登录"
        className="zf-btn zf-btn-ghost"
        style={{ ...ghost, display: 'inline-flex', alignItems: 'center', padding: 8 }}
      >
        <Icon name="logout" size={18} />
      </button>
    </header>
  );
}