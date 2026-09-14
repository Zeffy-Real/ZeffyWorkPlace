"""短期会话记忆（P1-4）：任务级上下文视图 = 压缩摘要 + 最近 N 条未压缩消息。

🔴 关键约束（审查强制）：**本模块只做「LLM 输入视图层变换」，绝不修改 / 删除 DB Message 表数据。**
- 原始消息完整保留在 DB；
- 压缩是对「传给模型的上下文视图」的变换；
- ``compress`` 只在内存构造 ``CompressedView``，持久层原样不动。

视图构成：
- ``summary``：由 compressor 生成的摘要（含决策/节点等白名单信息）。
- ``recent``：最近 N 条未压缩消息（按时间升序）。
二者拼接即完整 LLM 输入视图。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CompressedView:
    """一次压缩后的上下文视图（运行时内存对象，非持久态）。"""

    summary: str = ""  # 摘要文本
    recent: list[dict[str, Any]] = field(default_factory=list)  # 最近 N 条未压缩消息
    token_count: int = 0  # 视图总 token 估算（保守上界）
    degraded: bool = False  # 是否走了降级路径（LLM 摘要失败被截断替代）
    dropped: int = 0  # 被折叠进摘要而不再出现在 recent 的原始条数
    raw_count: int = 0  # 压缩前原始消息总数

    def to_messages(self) -> list[dict[str, str]]:
        """转成 LLM 输入 messages：摘要作为首条 system，随后是 recent 消息。"""
        msgs: list[dict[str, str]] = []
        if self.summary:
            msgs.append({"role": "system", "content": f"[上下文摘要] {self.summary}"})
        msgs.extend(self.recent)
        return msgs


def to_msg_dict(m: Any) -> dict[str, str]:
    """把 Message ORM 行转成 LLM messages 字典（不丢失 role/content）。"""
    role = m.sender_role if isinstance(m.sender_role, str) else "user"
    # supervisor/agent/reviewer/system → assistant；user → user
    lc_role = "user" if role == "user" else "assistant"
    return {"role": lc_role, "content": getattr(m, "content", "") or ""}


def estimate_tokens(text: str | None, chars_per_token: float = 3.5) -> int:
    """保守上界 token 估算：对中国/英文混合按每 token 3.5 字符计（偏保守）。

    不盲信 LLM usage；用于触发压缩的判定阈值。
    """
    if not text:
        return 0
    # 中文字符权重更高 → 直接按字符数除 3.5，已是保守上界
    return max(1, int(len(text) / chars_per_token))
