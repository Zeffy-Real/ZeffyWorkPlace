"""P0-2 配置中心 与 LLMClient/异常 的单元测试。"""

import pytest

from app import llm
from app.config import Settings
from app.llm_errors import LLMConfigError


def test_settings_defaults():
    """未配置环境下使用默认值。"""
    s = Settings()
    assert s.PORT == 8787
    assert s.LLM_PROVIDER == "openai"
    assert s.llm_api_key_set is False  # 空 key / 占位 key 视为未配置


def test_settings_env_override(monkeypatch):
    """环境变量覆盖默认值。"""
    monkeypatch.setenv("PORT", "9999")
    monkeypatch.setenv("LLM_MODEL", "deepseek-chat")
    s = Settings()
    assert s.PORT == 9999
    assert s.LLM_MODEL == "deepseek-chat"


class TestLLMConfig:
    """LLM 配置错误分支（不依赖真实网络）。"""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        llm._llm_singleton = None

    @pytest.mark.asyncio
    async def test_missing_key_raises_config_error(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "")
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        client = llm.get_llm()
        with pytest.raises(LLMConfigError):
            await client.agenerate([{"role": "user", "content": "ping"}])

    @pytest.mark.asyncio
    async def test_unsupported_provider_raises_config_error(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        client = llm.get_llm()
        with pytest.raises(LLMConfigError):
            await client.agenerate([{"role": "user", "content": "ping"}])

    @pytest.mark.asyncio
    async def test_self_test_returns_structured_no_key(self, monkeypatch):
        """无 key 时 self-test 返回结构化 config_error 而非崩溃。"""
        monkeypatch.setenv("LLM_API_KEY", "")
        result = await llm.ping_llm()
        assert result["status"] == "config_error"
        assert "error" in result
