"""Reviewer Agent（P1-3）：评审产物 → pass/revise + 意见。

行为范式：Reflection。基于验收标准（acceptance_criteria）审查产物，给出结构化结论。
输出的 ``decision`` 含 ``verdict``（pass|revise|req_change）与 ``comments``，供后续节点/人手决策。
"""

from __future__ import annotations

from typing import Any

from app.agents.base import AgentResult, BaseAgent

_REVIEW_PROMPT = """你是严格且专业的评审者。请依据「验收标准」审查给定产物，并给出结论。

必须返回严格 JSON（不要 markdown 围栏）：
{{
  "verdict": "pass | revise",
  "comments": ["意见1", "意见2"],
  "summary": "评审总结（一两句）"
}}

准则：
- verdict=pass 仅在产物**完全满足**全部验收标准时使用；有任何不达标即 revise。
- comments 必须具体、可操作，指出改进方向。
"""


class ReviewerAgent(BaseAgent):
    def __init__(self, *, role: str = "reviewer", **kwargs: Any) -> None:
        super().__init__(role=role, **kwargs)

    async def run(self, *, criteria: str, artifact_text: str, task_title: str = "",
                  history_summary: str = "") -> AgentResult:
        await self._emit("review_start")
        parts = [f"待审任务：{task_title or '（未命名）'}", f"验收标准：\n{criteria or '（未提供）'}",
                 f"产物内容：\n{artifact_text}"]
        if history_summary:
            parts.insert(1, f"[已压缩的历史上下文]\n{history_summary}")
        user = "\n\n".join(parts)
        text, usage = await self._call(
            [{"role": "system", "content": _REVIEW_PROMPT}, {"role": "user", "content": user}]
        )
        try:
            data = self._extract_json(text)
        except (ValueError, KeyError) as exc:
            await self._emit("review_error", error=str(exc))
            return AgentResult(status="error", error=f"评审输出解析失败：{exc}", usage=usage)

        verdict = data.get("verdict", "revise")
        decision = {
            "verdict": verdict,
            "comments": data.get("comments", []),
            "summary": data.get("summary", ""),
        }
        await self._emit("review_done", decision=decision)
        ok = verdict in {"pass", "revise"}  # 产出本身有效；pass/revise 是评审结论而非执行成败
        return AgentResult(status="ok" if ok else "error", text=text, usage=usage,
                           decision=decision)