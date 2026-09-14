"""P0-2 配置中心 与 LLMClient/异常 的单元测试。

注意：这些测试断言「无 key/默认」行为，须与运行环境隔离——用 ``_env_file=None``
禁掉 ``server/.env``（含真实 key），并清空 ``get_settings`` 缓存，避免受本机配置漂移影响。
"""

import pytest

from app import llm
from app.config import Settings
from app.llm_errors import LLMConfigError


@pytest.fixture(autouse=True)
def hermetic_settings(monkeypatch):
    """禁用 .env 文件 + 清缓存 + 重置 LLM 单例，使本测试文件环境无关。"""
    from app import config as cfg

    def _keyless():
        return cfg.Settings(_env_file=None)

    monkeypatch.setattr(cfg, "get_settings", _keyless)
    monkeypatch.setattr(llm, "get_settings", _keyless)
    llm._llm_singleton = None


def test_settings_defaults():
    """未配置环境下使用默认值。"""
    s = Settings(_env_file=None)
    assert s.PORT == 8787
    assert s.LLM_PROVIDER == "openai"
    assert s.llm_api_key_set is False  # 空 key / 占位 key 视为未配置


def test_settings_env_override(monkeypatch):
    """环境变量覆盖默认值。"""
    monkeypatch.setenv("PORT", "9999")
    monkeypatch.setenv("LLM_MODEL", "deepseek-chat")
    s = Settings(_env_file=None)
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
