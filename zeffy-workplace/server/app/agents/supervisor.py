"""Supervisor Agent：理解任务 → 拆解为子步骤 → 分配角色（P1-3）。

行为范式：Plan-and-Execute（此处只做 Plan 阶段）。
拆解输出**强制附带每个子任务的验收标准**（``acceptance_criteria``，prompt + schema 双重保证），
否则后续评审 Agent 无依据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agents.base import AgentResult, BaseAgent

_PLAN_PROMPT = """你是资深任务规划者（supervisor）。请把用户目标拆解为 ≤2 层的子任务，并为每个子任务分配 Agent 角色。

必须返回严格的 JSON（不要用 markdown 围栏，不要夹带多余文字），结构如下：
{{
  "goal": "一句话目标",
  "substeps": [
    {{
      "id": "s1",
      "role": "planner|doer|coder|documenter|designer",
      "title": "子任务标题",
      "description": "要做什么、边界与约束",
      "acceptance_criteria": "本子任务可被验收的具体标准，必须可判断量化"
    }}
  ]
}}

准则：
- 每个子任务的 acceptance_criteria 必须具体、可判定（不可写含糊的“完成即可”）。
- 角色只能取列表内允许值；层级最多两层（父 sub 与子 sub 的关系放在 description 描述即可）。
"""


@dataclass
class SubStep:
    id: str
    role: str
    title: str
    description: str
    acceptance_criteria: str


@dataclass
class PlanResult:
    goal: str
    substeps: list[SubStep] = field(default_factory=list)

    def to_decision(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "substeps": [s.__dict__ for s in self.substeps],
        }


class SupervisorAgent(BaseAgent):
    def __init__(self, *, role: str = "supervisor", **kwargs: Any) -> None:
        super().__init__(role=role, **kwargs)

    async def run(self, *, task_title: str, task_description: str) -> AgentResult:
        await self._emit("plan_start", task_title=task_title)
        user = (
            f"目标：{task_title}\n"
            f"补充说明：{task_description or '（无）'}\n"
            "请输出拆解 JSON。"
        )
        text, usage = await self._call(
            [{"role": "system", "content": _PLAN_PROMPT}, {"role": "user", "content": user}]
        )
        try:
            data = self._extract_json(text)
        except (ValueError, KeyError) as exc:
            await self._emit("plan_error", error=str(exc))
            return AgentResult(status="error", error=f"拆解输出解析失败：{exc}", usage=usage)

        substeps: list[SubStep] = []
        for s in data.get("substeps", []):
            if not s.get("acceptance_criteria"):
                await self._emit("plan_error", error="子任务缺 acceptance_criteria")
                return AgentResult(
                    status="error",
                    error="拆解结果缺失 acceptance_criteria（验收标准强制要求）",
                    usage=usage,
                    decision={"raw": data},
                )
            substeps.append(SubStep(
                id=s.get("id", ""),
                role=s.get("role", "doer"),
                title=s.get("title", ""),
                description=s.get("description", ""),
                acceptance_criteria=s["acceptance_criteria"],
            ))

        plan = PlanResult(goal=data.get("goal", ""), substeps=substeps)
        await self._emit("plan_done", goal=plan.goal, substeps=[s.__dict__ for s in substeps])
        return AgentResult(status="ok", text=plan.goal, usage=usage, decision=plan.to_decision())