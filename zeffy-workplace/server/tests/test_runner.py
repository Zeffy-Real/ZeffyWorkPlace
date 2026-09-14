"""P1-3 AgentRunner 编排测试：
- mock LLM 走通 generic 到「评审」节点（reached review）并最终 interrupt 于「验收」。
- WS push 契约：emit 收到 agent_message / task_node_update。
- 异常分层：可恢复耗尽与不可恢复 → 节点 failed + 审计落库 + 停止流转。
"""

from sqlalchemy import select

from app.agents.runner import AgentRunner
from app.db import models  # noqa: F401
from app.db.repos import create_task, list_nodes
from app.llm_errors import LLMConfigError
from app.tools.fs import make_fs_tools
from app.tools.registry import ToolRegistry
from tests.conftest import REVIEW_PASS, FakeLLM, plan_reply


def _generic_replies():
    """generic 模板：supervisor, documenter, designer, coder, reviewer 各一次。"""
    return [
        plan_reply(),
        ("文档产物：需求说明", {"prompt_tokens": 4, "completion_tokens": 4}),
        ("设计产物：架构示意", {"prompt_tokens": 4, "completion_tokens": 4}),
        ("代码产物：def main(): pass", {"prompt_tokens": 4, "completion_tokens": 4}),
        REVIEW_PASS,
    ]


def _runner(tmp_path, replies, raise_first=False) -> AgentRunner:
    reg = ToolRegistry()
    for spec in make_fs_tools(tmp_path):
        reg.register(spec)
    r = AgentRunner()
    r.registry = reg
    r.llm = FakeLLM(replies)
    return r


async def test_runner_walks_generic_to_review_skipping_hitl(session, tmp_path):
    task = await create_task(session, title="做一个计算器", workflow_id="generic")
    runner = _runner(tmp_path, _generic_replies())

    events = []

    async def emit(kind, payload):
        events.append((kind, payload))

    outcome = await runner.run(session, task.id, emit=emit)
    # generic 走到评审后，下一节点「验收」为 human/HITL → 中断挂起
    assert outcome["status"] == "interrupt"
    assert outcome["node"] == "验收"

    nodes = {n.node_name: n for n in await list_nodes(session, task.id)}
    # 已走过 需求分析/文档/设计/实现/评审（评审已完成 = 到达评审节点）
    for name in ["需求分析", "文档", "设计", "实现", "评审"]:
        assert nodes[name].status == "done", f"{name} 应已 done"
    # 验收挂起（running 由 HITL 激活）
    assert nodes["验收"].status == "running"

    # WS push 契约
    kinds = {k for k, _ in events}
    assert "agent_message" in kinds
    assert "task_node_update" in kinds


async def test_runner_writes_audits_and_messages(session, tmp_path):
    task = await create_task(session, title="写奖金计算", description="复杂一些", workflow_id="generic")
    runner = _runner(tmp_path, _generic_replies())
    await runner.run(session, task.id)

    from app.db.models import AuditLog, Message

    audits = list((await session.execute(select(AuditLog))).scalars())
    msgs = list((await session.execute(select(Message))).scalars())
    assert any(a.action == "agent_run" for a in audits)
    assert any(a.action == "interrupt" for a in audits)
    assert any(m.sender_role == "supervisor" for m in msgs)


async def test_runner_fatal_config_error_fails_node_and_stops(session, tmp_path, monkeypatch):
    import app.agents.base as base

    monkeypatch.setattr(base, "LLM_BASE_DELAY", 0)
    task = await create_task(session, title="t", workflow_id="lightweight")
    # supervisor 首调用即 LLMConfigError（不可恢复）
    runner = _runner(tmp_path, [LLMConfigError("no api key")])
    outcome = await runner.run(session, task.id)

    assert outcome["status"] == "error"
    nodes = {n.node_name: n for n in await list_nodes(session, task.id)}
    assert nodes["需求"].status == "failed"
    assert task.status == "failed"

    from app.db.models import AuditLog

    audits = list((await session.execute(select(AuditLog))).scalars())
    assert any(a.action == "node_failed" for a in audits)
    assert any(a.action == "llm_error" for a in audits)  # 不可恢复：记录 LLM 错误并落审计


async def test_runner_recoverable_exhausted_fails_node(session, tmp_path, monkeypatch):
    import app.agents.base as base

    monkeypatch.setattr(base, "LLM_BASE_DELAY", 0)
    task = await create_task(session, title="t", workflow_id="lightweight")
    runner = _runner(tmp_path, [base.LLMConnectionError("conn lost")] * 4)
    outcome = await runner.run(session, task.id)
    assert outcome["status"] == "error"
    nodes = {n.node_name: n for n in await list_nodes(session, task.id)}
    assert nodes["需求"].status == "failed"
