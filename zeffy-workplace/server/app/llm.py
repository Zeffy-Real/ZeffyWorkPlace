"""LLM 薄封装：provider 无关的调用抽象（面向 P1 预埋）。

设计要点：
- 只暴露 LLMClient，内部封装 LangChain ChatOpenAI（兼容 openai/deepseek/本地中转）。
- 接口签名面向 P1：入参为 messages 列表（支持多轮/System），返回 (文本, usage)。
- 运行时参数（temperature/max_tokens/stop）可覆盖，不硬编码进 config 单例。
- 异常统一走 llm_errors 分层，P1 据此重试/降级。
- 未配置 key 时抛 LLMConfigError，便于无 key 演示（自查友好失败，不让流程崩）。
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from app.config import get_settings
from app.llm_errors import LLMConfigError, LLMConnectionError, LLMError, LLMProviderError


class LLMClient:
    """Provider 无关的模型客户端。

    :param model: 模型名；为 None 时使用配置的 LLM_MODEL。
    :param base_url: provider 入口；为 "" 时使用官方默认，可指向中转/自建。
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self._settings = get_settings()
        self._model = model or self._settings.LLM_MODEL
        self._base_url = base_url if base_url is not None else self._settings.LLM_BASE_URL
        self._api_key = api_key if api_key is not None else self._settings.LLM_API_KEY
        # 懒加载
        self._client: Any | None = None

    def _get_client(self) -> Any:
        """懒加载底层 ChatModel 实例。"""
        if self._client is not None:
            return self._client

        if self._settings.LLM_PROVIDER.strip().lower() != "openai":
            raise LLMConfigError(
                f"不支持的 provider：{self._settings.LLM_PROVIDER!r}（当前仅适配 openai 兼容接口）"
            )
        if not self._settings.llm_api_key_set:
            raise LLMConfigError(
                "缺少 LLM_API_KEY，请在 server/.env 配置。无 key 时可运行 "
                "`python -m app.llm --self-test` 验证错误分支，或 `/health` 绕过 LLM。"
            )

        from langchain_openai import ChatOpenAI  # 延迟导入，降低启动开销

        kwargs: dict[str, Any] = {
            "model": self._model,
            "api_key": self._api_key,
            "temperature": self._settings.LLM_TEMPERATURE,
            "max_tokens": self._settings.LLM_MAX_TOKENS,
            "timeout": 60,
        }
        if self._base_url:
            kwargs["base_url"] = self._base_url
        self._client = ChatOpenAI(**kwargs)
        return self._client

    async def agenerate(
        self,
        messages: list[dict],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stop: list[str] | None = None,
    ) -> tuple[str, dict]:
        """一次异步生成。

        :param messages: [{"role": "system|user|assistant", "content": str}, ...]
        :return: (answer_text, usage)
            usage 占位 {"prompt_tokens":0, "completion_tokens":0}，P1 填真实值。
        """
        client = self._get_client()

        lc_messages: list[Any] = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            if role == "system":
                lc_messages.append(SystemMessage(content=content))
            else:
                lc_messages.append(HumanMessage(content=content))

        kwargs: dict[str, Any] = {}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if stop is not None:
            kwargs["stop"] = stop

        try:
            resp = await client.ainvoke(lc_messages, **kwargs)
        except LLMConfigError:
            raise
        except Exception as exc:  # noqa: BLE001
            # 依据异常类型粗分连接/Provider 错误（P1 可细化）。
            err_name = type(exc).__name__
            status = getattr(exc, "status_code", None)
            if any(token in err_name.lower() for token in ("timeout", "connect", "httpclient")):
                raise LLMConnectionError(f"连接/超时错误：{exc}") from exc
            if isinstance(status, int) and 400 <= status < 600:
                raise LLMProviderError(f"provider 返回 {status}：{exc}", status_code=status) from exc
            raise LLMProviderError(f"LLM 调用失败：{err_name}: {exc}") from exc

        # P0：usage 占位，P1 从 resp.usage_metadata 填真实值。
        usage = {
            "prompt_tokens": getattr(getattr(resp, "usage_metadata", None), "input_tokens", 0) or 0,
            "completion_tokens": getattr(
                getattr(resp, "usage_metadata", None), "output_tokens", 0
            )
            or 0,
        }
        return resp.content, usage


_llm_singleton: LLMClient | None = None


def get_llm(model: str | None = None, **kwargs: Any) -> LLMClient:
    """懒加载的单例获取。可选覆盖模型名与参数。"""
    global _llm_singleton
    if model is None:
        if _llm_singleton is None:
            _llm_singleton = LLMClient(**kwargs)
        return _llm_singleton
    return LLMClient(model=model, **kwargs)


async def ping_llm() -> dict:
    """最小自检：返回结构化结果，供 --self-test 与自动化脚本使用。"""
    result: dict[str, Any] = {"status": "ok", "provider": get_settings().LLM_PROVIDER}
    try:
        text, usage = await get_llm().agenerate([{"role": "user", "content": "ping"}])
        result.update({"answer": text, "usage": usage})
    except LLMConfigError as e:
        result.update({"status": "config_error", "error": str(e)})
    except LLMError as e:
        result.update({"status": "llm_error", "error": str(e)})
    return result


def _run_self_test() -> None:
    """CLI 入口：uv run python -m app.llm --self-test。带超时防止卡死。"""
    result = asyncio.run(asyncio.wait_for(ping_llm(), timeout=70))
    import json

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "ok":
        raise SystemExit(2)


if __name__ == "__main__":
    _run_self_test()
