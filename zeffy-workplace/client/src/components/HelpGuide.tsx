import { colors, space, type as T } from '../theme';
import { Modal } from './ui/Modal';
import { Icon, type IconName } from './ui/Icon';

/* 使用指引（产品功能说明，不含内部实现/接口/配置）。 */
const STEPS: Array<{ icon: IconName; title: string; body: string }> = [
  { icon: 'plus', title: '① 新建任务', body: '点右上角「新任务」，用一句话写下目标，选工作流后开始。系统会自动把目标拆解并交给多个协作 Agent 执行。' },
  { icon: 'task', title: '② 查看执行', body: '在任务详情里可实时看到各节点进度（拆解、设计、实现、评审…）。执行中不需要你守着，完成后会更新。' },
  { icon: 'back', title: '③ 审批与产物', body: '遇到「待审批」节点（如验收）时点「通过」继续；任务完成后可查看该任务产出的文件并下载。' },
  { icon: 'folder', title: '④ 治理面板', body: '首页顶部可看到用量与配额、回收站、审计等；日常无需关注，异常会自动提示。' },
];

export function HelpGuide({ open, onClose }: { open: boolean; onClose: () => void }) {
  return (
    <Modal open={open} onClose={onClose} title="使用指引" maxWidth={520}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: space[4] }}>
        <p style={{ margin: 0, color: colors.gray, fontSize: T.helper, lineHeight: T.lhHelper }}>
          这是一个「一句话发起、多 Agent 协作」的工作台。花一分钟了解怎么用：
        </p>
        {STEPS.map((s) => (
          <div key={s.title} style={{ display: 'flex', gap: space[3], alignItems: 'flex-start' }}>
            <span
              style={{ color: colors.accent, display: 'inline-flex', marginTop: 2, flexShrink: 0 }}
              aria-hidden="true"
            >
              <Icon name={s.icon} size={20} />
            </span>
            <div>
              <div style={{ fontWeight: 600, color: colors.ink, fontSize: T.body }}>{s.title}</div>
              <div style={{ color: colors.gray, fontSize: T.helper, lineHeight: T.lhHelper, marginTop: 2 }}>{s.body}</div>
            </div>
          </div>
        ))}
      </div>
    </Modal>
  );
}