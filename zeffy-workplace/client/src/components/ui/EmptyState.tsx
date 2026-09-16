import { colors, radius, space, type as TypeTokens } from '../../theme';
import { Icon, type IconName } from './Icon';

/* 空状态：图标 + 标题 + 说明 + 动作。用于 任务列表/产物区/回收站 等无数据场景。 */
export function EmptyState({
  icon,
  title,
  description,
  action,
}: {
  icon: IconName;
  title: string;
  description: string;
  action?: React.ReactNode;
}) {
  return (
    <div
      role="status"
      style={{
        border: `1px dashed ${colors.line}`, borderRadius: radius.card,
        padding: `${space[8]}px ${space[5]}px`, display: 'flex', flexDirection: 'column',
        alignItems: 'center', gap: space[2], textAlign: 'center', background: colors.bg,
      }}
    >
      <span style={{ color: colors.mist, display: 'inline-flex' }} aria-hidden="true">
        <Icon name={icon} size={28} />
      </span>
      <div style={{ fontWeight: 600, fontSize: TypeTokens.body, color: colors.ink }}>{title}</div>
      <div style={{ fontSize: TypeTokens.helper, color: colors.gray, lineHeight: TypeTokens.lhHelper, maxWidth: 320 }}>
        {description}
      </div>
      {action && <div style={{ marginTop: space[2] }}>{action}</div>}
    </div>
  );
}