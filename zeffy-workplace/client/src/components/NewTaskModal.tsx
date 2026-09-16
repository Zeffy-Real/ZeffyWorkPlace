import { useState } from 'react';
import { colors, radius, space, t, type as T } from '../theme';
import { Modal } from './ui/Modal';
import { Icon } from './ui/Icon';

export type WorkflowChoice = 'generic' | 'lightweight';
export const WORKFLOW_OPTIONS: Array<{ value: WorkflowChoice; name: string; desc: string; disabled?: boolean }> = [
  { value: 'lightweight', name: '轻量快捷', desc: '拆解 → 执行 → 验收，更快' },
  { value: 'generic', name: '完整协作', desc: '拆解 → 设计 → 实现 → 评审 → 验收', disabled: true },
];

/* 新建任务：输入目标 + 选工作流模板 → 开始（提交中禁用防重复）。 */
export function NewTaskModal({
  open,
  onClose,
  onCreate,
}: {
  open: boolean;
  onClose: () => void;
  onCreate: (prompt: string) => Promise<void>;
}) {
  const [prompt, setPrompt] = useState('');
  const [workflow, setWorkflow] = useState<WorkflowChoice>('lightweight');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const input = t.input().style;
  const primary = t.btnPrimary().style;
  const ghost = t.btnGhost().style;

  const submit = async () => {
    const text = prompt.trim();
    if (!text) {
      setError('请先输入任务目标，例如「帮我写一份周报草稿」。');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await onCreate(text);
      setPrompt('');
    } catch (err) {
      setError(err instanceof Error ? err.message : '提交失败，请重试');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="新建一个任务"
      maxWidth={520}
      footer={
        <>
          <button onClick={onClose} className="zf-btn zf-btn-ghost" style={ghost}>
            取消
          </button>
          <button
            onClick={() => void submit()}
            disabled={busy}
            className="zf-btn zf-btn-primary"
            style={{ ...primary, display: 'inline-flex', alignItems: 'center', gap: 6 }}
          >
            {busy && <Icon name="spinner" size={15} className="zf-spin" />}
            {busy ? '正在提交…' : '开始'}
          </button>
        </>
      }
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: space[4] }}>
        <div>
          <label
            htmlFor="new-task-prompt"
            style={{ display: 'block', fontWeight: 600, fontSize: T.helper, color: colors.ink, marginBottom: space[2] }}
          >
            任务目标
          </label>
          <textarea
            id="new-task-prompt"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            rows={4}
            placeholder="用一句话说清你要达成什么。例如：帮我写一份项目进展汇报，并保存成 Markdown。"
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? 'new-task-prompt-hint' : undefined}
            className="zf-input"
            style={{ ...input, width: '100%', resize: 'vertical', lineHeight: T.lhBody }}
          />
          {error ? (
            <div id="new-task-prompt-hint" role="alert" style={{ color: colors.danger, fontSize: T.helper, marginTop: space[1] }}>
              {error}
            </div>
          ) : (
            <div id="new-task-prompt-hint" style={{ color: colors.gray, fontSize: T.hint, marginTop: space[1] }}>
              提交后会自动交给协作 Agent 拆解并开始执行。
            </div>
          )}
        </div>

        <fieldset style={{ border: 'none', padding: 0, margin: 0, display: 'flex', flexDirection: 'column', gap: space[2] }}>
          <legend style={{ fontWeight: 600, fontSize: T.helper, color: colors.ink, marginBottom: space[1] }}>
            工作流模板
          </legend>
          <div style={{ display: 'flex', gap: space[2], flexWrap: 'wrap' }}>
            {WORKFLOW_OPTIONS.map((opt) => {
              const active = workflow === opt.value;
              return (
                <label
                  key={opt.value}
                  aria-disabled={opt.disabled}
                  style={{
                    flex: '1 1 180px', border: `1px solid ${active ? colors.accent : colors.line}`,
                    borderRadius: radius.card, padding: `${space[2]}px ${space[3]}px`,
                    cursor: opt.disabled ? 'not-allowed' : 'pointer',
                    background: active ? colors.accent10 : colors.white,
                    transition: 'border-color 0.15s ease-out', opacity: opt.disabled ? 0.55 : 1,
                  }}
                >
                  <span style={{ display: 'flex', alignItems: 'center', gap: space[2] }}>
                    <input
                      type="radio"
                      name="workflow"
                      value={opt.value}
                      checked={active}
                      disabled={opt.disabled}
                      onChange={() => setWorkflow(opt.value)}
                      style={{ accentColor: colors.accent }}
                    />
                    <span style={{ fontWeight: 600, color: active ? colors.accent : colors.ink }}>
                      {opt.name}
                      {opt.disabled && <span style={{ color: colors.gray, fontWeight: 400 }}>（即将支持）</span>}
                    </span>
                  </span>
                  <span style={{ display: 'block', fontSize: T.hint, color: colors.gray, marginTop: 2, paddingLeft: 26 }}>
                    {opt.desc}
                  </span>
                </label>
              );
            })}
          </div>
        </fieldset>
      </div>
    </Modal>
  );
}