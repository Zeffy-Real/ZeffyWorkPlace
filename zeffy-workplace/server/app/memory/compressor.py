"""上下文压缩器（P1-4）：长对话自动摘要，收尾仍能干活。

- ``should_compress``：**双重条件（轮次阈值 OR token 阈值）任一满足即触发**；
  token 用保守上界估算（见 store.estimate_tokens），不盲信 LLM usage。
- ``compress``：调用 LLM 生成摘要并**保留白名单字段**（task 目标 / 已完成节点 / 评审结论 /
  重大人类决策 / 失败原因）。返回 ``CompressedView`` + token 统计。
- 🔴 压缩失败降级：LLM 摘要失败（限流/报错）**不使任务失败**——降级为「截断丢弃最老
  非关键消息」继续运行；返回 ``degraded=True`` 由调用方写审计标记压缩失败。

本模块是**纯视图层**：不读 DB、不写 DB；输入为全部消息 dict 列表与决策点，
输出为内存视图。调用方负责把视图转成 LLM 输入。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.llm import LLMClient, get_llm
from app.memory.store import CompressedView, estimate_tokens, to_msg_dict

logger = logging.getLogger(__name__)

# 压缩后摘要的期望 token 上限（保守）。
_SUMMARY_MAX_TOKENS = 800

# 兜底降级时保留的最近消息条数（其余折叠丢弃）。
_DEGRADE_KEEP_RECENT = 10

# 白名单字段：摘要必须保留的信息（决策/节点/教训）。
WHITELIST_KEYS = ("goal", "task_target", "completed_nodes", "review_conclusion",
                  "human_decisions", "failure_reason")

_SUMMARY_PROMPT = """你是上下文压缩器。请把以下任务执行历史压缩成一段结构化的上下文摘要，供后续 Agent 继续工作。

必须覆盖（白名单字段）：
- goal / task_target：任务目标
- completed_nodes：已完成的工作流节点与结论
- review_conclusion：评审结论（pass/revise 与关键意见）
- human_decisions：重大人类决策
- failure_reason：失败原因（如有）

要求：
- 结论逐项保留，宁可具体不要泛化；省略寒暄与过程性噪音。
- 只输出摘要正文，不要任何额外解释或围栏。
"""


@dataclass
class CompressorConfig:
    max_rounds: int = 30
    max_tokens: int = 8000


class ContextCompressor:
    def __init__(self, llm: LLMClient | None = None, *, config: CompressorConfig | None = None,
                 recent_kept: int = 0) -> None:
        self.llm = llm or get_llm()
        self.config = config or CompressorConfig()
        # recent_kept：压缩后额外保留的最近未压缩消息条数；0 则全部进摘要。
        self.recent_kept = recent_kept

    def should_compress(self, rounds: int, token_est: int) -> bool:
        """双重条件任一满足即触发：轮次超阈值 OR token 超阈值。"""
        return rounds > self.config.max_rounds or token_est > self.config.max_tokens

    async def compress(
        self,
        messages: list[dict[str, str]],
        decisions: list[dict[str, Any]] | None = None,
        *,
        rounds: int = 0,
        raw_count: int | None = None,
    ) -> CompressedView:
        """压缩消息历史为视图。

        :param messages: 全部消息（LLM 输入形态，role/content）。
        :param decisions: 决策点（supervisor 拆解 / reviewer 结论等）。
        :return: CompressedView。LLM 失败则降级截断，绝不让调用方崩溃。
        """
        raw_count = raw_count if raw_count is not None else len(messages)

        if not messages:
            return CompressedView(summary="", recent=[], token_count=0, raw_count=0)

        try:
            summary, usage = await self.llm.agenerate(
                self._prompt(messages, decisions or []), max_tokens=_SUMMARY_MAX_TOKENS
            )
            summary = (summary or "").strip()
            recent = messages[-self.recent_kept:] if self.recent_kept else []
            dropped = max(0, raw_count - self.recent_kept)
            view_tokens = estimate_tokens(summary) + sum(
                estimate_tokens(m.get("content", "")) for m in recent
            )
            return CompressedView(
                summary=summary, recent=recent, token_count=view_tokens,
                degraded=False, dropped=dropped, raw_count=raw_count,
            )
        except Exception as exc:  # noqa: BLE001 摘要失败不使任务失败 → 降级截断
            logger.warning("上下文压缩失败，降级截断：%s", exc)
            recent = messages[-_DEGRADE_KEEP_RECENT:]
            view_tokens = sum(estimate_tokens(m.get("content", "")) for m in recent)
            return CompressedView(
                summary="（前序上下文已因压缩失败被折叠）", recent=recent,
                token_count=view_tokens, degraded=True, raw_count=raw_count,
                dropped=max(0, raw_count - _DEGRADE_KEEP_RECENT),
            )

    def _prompt(self, messages: list[dict], decisions: list[dict]) -> list[dict]:
        history = "\n".join(
            f"{m.get('role','?')}: {m.get('content','')}" for m in messages
        )
        dec_block = "\n".join(str(d) for d in decisions) if decisions else "（无显式决策点）"
        return [
            {"role": "system", "content": _SUMMARY_PROMPT},
            {"role": "user",
             "content": f"决策 / 工作流信息：\n{dec_block}\n\n任务历史：\n{history}"},
        ]

    # 便捷：输入 ORM messages + 决策 → 视图
    async def compress_messages(
        self, rows: list[Any], decisions: list[dict[str, Any]] | None = None, **kw: Any
    ) -> CompressedView:
        return await self.compress([to_msg_dict(m) for m in rows], decisions, **kw)
