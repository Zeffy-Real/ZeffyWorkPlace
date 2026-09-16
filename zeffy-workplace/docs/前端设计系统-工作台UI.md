# 前端设计系统 · 工作台 UI（补充说明）

> 本文档沉淀「工作台 UI 改造」新增的令牌与 UI 基元，供后续页面复用，避免重复造轮子。
> 设计约束：克制扁平、卡片=边框**或**轻阴影二选一、唯一强调色 `#2563eb`、间距 8pt、圆角 4/6/8/12、WCAG-AA、内联 SVG 图标（禁 emoji）。

## 1. 令牌新增（`src/theme.ts`）
| 令牌 | 值/说明 |
|---|---|
| `space` | 新增 `0:0`、`5:20`、`8:32`（沿用 8pt 栅格） |
| `motion` | `fast:0.15s ease-out`、`slow:0.22s ease-out` |
| `shadow` | `lift`/`overlay`/`modal`，仅用于浮层与提升，**不做装饰阴影** |
| `type` | 新增 `titleM:24` |

新增 `t.*` 工厂：`t.stat()`（总览统计卡）、`t.tab(active)`（分段筛选）、`t.skeleton()`。
`tokens.css` 新增类：`.zf-stat`/`.zf-tab`（含 hover/focus/active/[aria-pressed]/disabled）、`.zf-skeleton`、`.zf-spin`、`.zf-toast-region`，均于 `prefers-reduced-motion` 下停用动画。

## 2. UI 基元（`src/components/ui/`）
| 组件 | 用途 | 关键点 |
|---|---|---|
| `Icon` | 统一内联 SVG 图标 | `name` 枚举；`stroke=currentColor`；装饰用 `aria-hidden` |
| `Modal` | 可访问弹窗 | Esc/遮罩关闭、焦点 trap、`role=dialog`+`aria-modal`、`shadow.modal` |
| `EmptyState` | 空状态 | 图标+标题+说明+动作；用于任务/产物/回收站无数据与错误态 |
| `Skeleton` | 骨架屏 | 加载占位，脉冲动画，reduced-motion 降级 |
| `Toast` | 轻提示 | 模块级 `toast(text,'ok'/'error'/'info')`；`<ToastRegion/>` 挂 App；`aria-live=polite` |

## 3. 业务组件（`src/components/`）
- `WorkbenchNav`：统一导航壳（品牌 + WS 状态 + 新任务 + 帮助 + 退出），首页/详情共用。
- `NewTaskModal`：新建任务（输入目标 + 模板选择），提交走 WS `/user_message`。
- `HelpGuide`：使用指引（产品说明，不含内部实现/接口）。
- 页面：`TaskListPage`（工作台首页：欢迎头 + 统计行 + 筛选 Tab + 列表 + 空态引导 + 治理面板）、`TaskDetailPage`（节点步骤条 + 审批/追问卡 + 产物）。

## 4. 复用规范
- 新页面一律优先复用上述基元与 `t.*` 工厂、`.zf-*` 类；**颜色/间距/圆角/阴影引用令牌，禁字面量硬编码**。
- 图标用 `Icon`（内联 SVG），不新增 emoji。
- 交互组件覆盖 default/hover/focus-visible/active/disabled/loading/error 七态。

## 5. 真机回归
`server/scripts/full_flow_test.py` 覆盖：登录 → 工作台 → 新建任务 → 详情步骤条 → UI 审批 → 产物下载 → 零 console/pageerror。