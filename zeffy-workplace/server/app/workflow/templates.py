"""工作流模板（P1-2）。

模板 = 节点序列 + 模板级 ``max_revision`` / ``max_rounds``。
- ``max_revision``：评审最大修订次数（防 A2 死循环，P1-5 使用）。
- ``max_rounds``：任务全局最大 Agent 轮次（防 LLM 死循环 / 成本爆炸，P1-5 使用）。
- P1 只做**顺序流转**；DAG 并行依赖 / 多节点并行属 P2。

节点类型（P1-5 解释给运行层）：
- ``auto``：Agent 自动执行。
- ``human``：人工介入（信息补充等）。
- ``hitl``：需审批（human-in-the-loop，走 interrupt）。
"""

from __future__ import annotations

from dataclasses import dataclass

# 节点类型
NODE_AUTO = "auto"
NODE_HUMAN = "human"
NODE_HITL = "hitl"


@dataclass(frozen=True)
class WorkflowNodeSpec:
    name: str
    role: str
    type: str = NODE_AUTO
    # P2 DAG：显式依赖 node_name 列表；None = 依赖模板前一节点（串行默认）
    depends_on: list[str] | None = None


@dataclass(frozen=True)
class WorkflowTemplate:
    key: str
    max_revision: int
    max_rounds: int
    nodes: list[WorkflowNodeSpec]


GENERIC = WorkflowTemplate(
    key="generic",
    max_revision=2,
    max_rounds=10,
    nodes=[
        WorkflowNodeSpec("需求分析", "supervisor"),
        WorkflowNodeSpec("文档", "documenter"),
        WorkflowNodeSpec("设计", "designer"),
        WorkflowNodeSpec("实现", "coder"),
        WorkflowNodeSpec("评审", "reviewer"),
        WorkflowNodeSpec("验收", "human", type=NODE_HITL),
    ],
)

LIGHTWEIGHT = WorkflowTemplate(
    key="lightweight",
    max_revision=1,
    max_rounds=6,
    nodes=[
        WorkflowNodeSpec("需求", "supervisor"),
        WorkflowNodeSpec("拆解", "planner"),
        WorkflowNodeSpec("执行", "doer"),
        WorkflowNodeSpec("验收", "human", type=NODE_HITL),
    ],
)

TEMPLATES: dict[str, WorkflowTemplate] = {
    GENERIC.key: GENERIC,
    LIGHTWEIGHT.key: LIGHTWEIGHT,
}


def get_template(key: str) -> WorkflowTemplate:
    try:
        return TEMPLATES[key]
    except KeyError:
        raise ValueError(f"未知工作流模板：{key!r}") from None
