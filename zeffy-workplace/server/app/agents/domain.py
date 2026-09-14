"""Domain Agent（P1-3）：按工作流节点角色实例化的业务执行者。

- 覆盖：``planner / doer / coder / documenter / designer``。
- 行为范式：**P1 只做单轮（直调 LLM 产出 + 可选调用一次工具写文件）**，
  完整多步 ReAct 循环放 P2（文档标注，防超范围开发）。
- 可选用注入的 ``ToolRegistry`` 调用 ``fs_write`` 落产物；工具调用与结果均进事件流。
"""

from __future__ import annotations

from typing import Any

from app.agents.base import AgentResult, BaseAgent

_DEFAULT_TASK = {
    "planner": "你是拆解执行者。请把需求细化为可执行的实施计划。",
    "doer": "你是执行者。请根据任务产出可直接使用的结果/交付物。",
    "coder": "你是实现者。请产出高质量、可直接运行的代码。",
    "documenter": "你是文档工程师。请产出结构清晰、准确的专业文档。",
    "designer": "你是设计者。请产出合理、可行的技术/产品设计。",
    "reviewer": "你是评审者。请审查并给出结论。",  # 兜底，正式评审走 ReviewerAgent
}


class DomainAgent(BaseAgent):
    def __init__(self, *, role: str = "doer", tools: Any = None, **kwargs: Any) -> None:
        super().__init__(role=role, **kwargs)
        self.tools = tools  # ToolRegistry | None
        self.system = _DEFAULT_TASK.get(role, _DEFAULT_TASK["doer"])

    async def run(self, *, task_title: str, plan_summary: str = "", criteria: str = "",
                  artifact: str = "", history_summary: str = "", followup: str = "",
                  **ctx: Any) -> AgentResult:
        """单轮产出。可附带 plan / criteria / 前序产物 / 压缩上下文 / 修订反馈供上下文。"""
        await self._emit("gen_start", role=self.role)
        user_parts = [f"任务：{task_title}"]
        if history_summary:
            user_parts.append(f"[已压缩的历史上下文]\n{history_summary}")
        if plan_summary:
            user_parts.append(f"实施计划：{plan_summary}")
        if criteria:
            user_parts.append(f"验收标准：{criteria}")
        if followup:
            user_parts.append(f"[修订反馈/人工要求]\n{followup}")
        if artifact:
            user_parts.append(f"参考/待处理内容：\n{artifact}")
        user = "\n\n".join(user_parts)

        text, usage = await self._call(
            [{"role": "system", "content": self.system}, {"role": "user", "content": user}]
        )
        await self._emit("gen_done", role=self.role, text_preview=text[:200])

        paths: list[str] = []
        # 可选：落一份产物文件（仅当提供 tools 且产物非空且不是纯加工失败）。
        if self.tools is not None and text.strip():
            res = await self.tools.run(
                "fs_write",
                task_id=ctx.get("task_id", "unspecified"),
                path=f"{self.role}-artifact.md",
                content=text,
                mode="no_overwrite",
                run_id=ctx.get("run_id", ""),
            )
            await self._emit("tool", name="fs_write", ok=res.ok, error=res.error, data=res.data)
            if res.ok and res.data:
                paths.append(str(res.data.get("abs_path", "")))

        return AgentResult(status="ok", text=text, usage=usage, artifact_paths=paths)
