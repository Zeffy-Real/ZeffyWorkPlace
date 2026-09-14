"""Agent 基座（P1-3）。

- ``AgentResult``：统一 Agent 输出。含 status / text / usage / artifact_paths / decision / error，
  **ul保持完整**（错误与决策都要带上），供评审与审计查询；不静默丢异常。
- ``BaseAgent``：统一元数据（role）、标准输入输出、状态回调（``on_event``）、LLM 调用封装。
  - LLM 调用**必须**先 acquire ``get_llm_semaphore()`` 背压信号量（防 429 堆积）。
  - 异常分层：``LLMConfigError`` 不可重试直接抛；``LLMConnectionError`` /
    ``LLMProviderError``（429/5xx）按指数退避有限次重试。
  - 工具经注入的 ``ToolRegistry`` 调用（不在 Base 内 new，便于测试替换）。
- ``on_event``：供调用方（AgentRunner）收集 Agent 阶段事件（plan / decide / tool / review），
  转成 WS 推送或审计，不在 Agent 内硬编码推送逻辑。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.appstate import get_llm_semaphore
from app.llm import LLMClient, get_llm
from app.llm_errors import LLMConfigError, LLMConnectionError, LLMError, LLMProviderError

logger = logging.getLogger(__name__)

# 可重试的 provider 错误码（HTTP Status）。
_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}

# 单次 LLM 调用最大重试次数与基础退避（秒）。
LLM_MAX_RETRIES = 3
LLM_BASE_DELAY = 0.4


@dataclass
class AgentResult:
    """Agent 一次执行的标准化结果。"""

    status: str  # ok / error
    text: str = ""  # 输出文本（LLM 原样或产物摘要）
    usage: dict[str, int] = field(default_factory=dict)  # {prompt_tokens, completion_tokens}
    artifact_paths: list[str] = field(default_factory=list)
    decision: dict[str, Any] | None = None  # 结构化决策（supervisor: 拆解、reviewer: pass/revise）
    error: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)  # 阶段事件（已 flush 一般清空）

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_log(self) -> dict[str, Any]:
        """审计用的紧凑字典（不丢决策/错误）。"""
        return {
            "status": self.status,
            "text": self.text,
            "usage": self.usage,
            "artifact_paths": self.artifact_paths,
            "decision": self.decision,
            "error": self.error,
        }


EventCb = Callable[[dict[str, Any]], Awaitable[None]]


class BaseAgent:
    """Agent 基类。子类实现 ``_build_messages`` 与 ``run``。"""

    def __init__(self, *, role: str, llm: LLMClient | None = None,
                 on_event: EventCb | None = None) -> None:
        self.role = role
        self.llm = llm or get_llm()
        self.on_event = on_event

    # ---- 事件回调（供 AgentRunner 收集） ----
    async def _emit(self, event: str, **payload: Any) -> None:
        if self.on_event is None:
            return
        await self.on_event({"agent": self.role, "event": event, **payload})

    # ---- LLM 封装 ----
    async def _call(self, messages: list[dict], *, temperature: float | None = None,
                    max_tokens: int | None = None) -> tuple[str, dict]:
        """带背压信号量 + 异常分层重试的 LLM 调用。"""
        sem = get_llm_semaphore()
        for attempt in range(LLM_MAX_RETRIES + 1):
            async with sem:
                try:
                    text, usage = await self.llm.agenerate(
                        messages, temperature=temperature, max_tokens=max_tokens
                    )
                    return text, usage
                except LLMConfigError:
                    raise  # 不可重试，直接上抛
                except (LLMConnectionError, LLMProviderError) as exc:
                    if attempt < LLM_MAX_RETRIES and self._is_retryable(exc):
                        delay = LLM_BASE_DELAY * (2**attempt)
                        logger.warning(
                            "LLM 调用重试(%s/%s) agent=%s delay=%s：%s",
                            attempt + 1, LLM_MAX_RETRIES, self.role, delay, exc,
                        )
                        await asyncio.sleep(delay)
                    else:
                        raise
        raise LLMError("unreachable")  # pragma: no cover

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        status = getattr(exc, "status_code", None)
        if isinstance(exc, LLMConnectionError):
            return True
        if isinstance(exc, LLMProviderError):
            return status is None or status in _RETRYABLE_STATUS
        return False

    # ---- JSON 抽取 ----
    @staticmethod
    def _extract_json(text: str) -> dict[str, Any]:
        """从 LLM 输出中抽取 JSON 对象（容忍 ```json 围栏与杂散文本）。"""
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:]
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end == -1 or end < start:
            raise ValueError(f"LLM 输出不含 JSON 对象：{text[:200]!r}")
        return json.loads(cleaned[start : end + 1])