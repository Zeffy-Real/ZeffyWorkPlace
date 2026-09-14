"""P1-3 单 Agent 单元测试：supervisor 拆解（含验收标准）、reviewer 评审、domain 单轮产出。"""

import pytest

from app.agents.domain import DomainAgent
from app.agents.reviewer import ReviewerAgent
from app.agents.supervisor import SupervisorAgent
from app.llm_errors import LLMConfigError, LLMConnectionError
from tests.conftest import FakeLLM, REVIEW_PASS, plan_reply


async def test_supervisor_plan_requires_acceptance_criteria():
    llm = FakeLLM([plan_reply()])
    agent = SupervisorAgent(llm=llm)
    result = await agent.run(task_title="写一个计算器", task_description="支持四则运算")
    assert result.ok
    assert result.decision is not None
    steps = result.decision["substeps"]
    assert len(steps) == 2
    for s in steps:
        assert s["acceptance_criteria"]  # 每个子任务必须有验收标准


async def test_supervisor_rejects_missing_criteria():
    bad = (
        '{"goal":"g","substeps":[{"id":"s1","role":"doer","title":"t",'
        '"description":"d","acceptance_criteria":""}]}',
        {"prompt_tokens": 1, "completion_tokens": 1},
    )
    agent = SupervisorAgent(llm=FakeLLM([bad]))
    result = await agent.run(task_title="x", task_description="")
    assert not result.ok
    assert "acceptance_criteria" in (result.error or "")


async def test_reviewer_pass_verdict():
    agent = ReviewerAgent(llm=FakeLLM([REVIEW_PASS]))
    result = await agent.run(criteria="达标即可", artifact_text="产物内容")
    assert result.ok
    assert result.decision["verdict"] == "pass"


async def test_reviewer_malformed_output_errors():
    bad = ("这不是JSON", {"prompt_tokens": 1, "completion_tokens": 1})
    agent = ReviewerAgent(llm=FakeLLM([bad]))
    result = await agent.run(criteria="c", artifact_text="a")
    assert not result.ok
    assert "解析失败" in (result.error or "")


async def test_domain_single_round_with_tool(tmp_path):
    from app.tools.fs import make_fs_tools
    from app.tools.registry import ToolRegistry

    reg = ToolRegistry()
    for spec in make_fs_tools(tmp_path):
        reg.register(spec)

    agent = DomainAgent(role="coder", tools=reg, llm=FakeLLM([("def add(): return 1",
                                                              {"prompt_tokens": 3, "completion_tokens": 2})]))
    result = await agent.run(task_title="实现", task_id="t1")
    assert result.ok
    assert result.artifact_paths  # 单轮直调 + 落产物（P1 无多步 ReAct）
    assert result.artifact_paths[0].endswith("coder-artifact.md")


async def test_config_error_non_retryable():
    """LLMConfigError 立即上抛，不重试（回到 Runner 置 failed）。"""
    agent = SupervisorAgent(llm=FakeLLM([LLMConfigError("no key")]))
    with pytest.raises(LLMConfigError):
        await agent.run(task_title="x", task_description="")


async def test_retry_exhausted_raises(monkeypatch):
    """可重试异常耗尽仍抛（LLMConnectionError 重试 LLM_MAX_RETRIES 次后上抛）。"""
    import app.agents.base as base

    monkeypatch.setattr(base, "LLM_BASE_DELAY", 0)
    n = 3  # LLM_MAX_RETRIES + 1 次调用最后 raise
    replies = [LLMConnectionError("conn")] * (n + 1)
    agent = SupervisorAgent(llm=FakeLLM(replies))
    with pytest.raises(LLMConnectionError):
        await agent.run(task_title="x", task_description="")