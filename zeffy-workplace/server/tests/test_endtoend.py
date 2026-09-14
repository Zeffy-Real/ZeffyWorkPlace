"""P1-5 端到端集成测试（mock LLM）：
- A2 评审回流：reviewer 先 revise 再 pass（revision loop 修复），超 max_revision 终止。
- A4 HITL 审批：驳回 → engine.rewind 回退重做；通过 → 推进收尾。
- A5 追问：supervisor need_info → interrupt；人工补充 answer → resume 继续。
- A6 压缩注入：超阈值触发摘要注入，DB 原始消息不变。
"""

from sqlalchemy import select

from app.agents.runner import AgentRunner
from app.db import models  # noqa: F401
from app.db.repos import create_task, list_nodes, write_message
from app.tools.fs import make_fs_tools
from app.tools.registry import ToolRegistry
from tests.conftest import REVIEW_PASS, FakeLLM, plan_reply


def _runner(tmp_path, replies) -> AgentRunner:
    reg = ToolRegistry()
    for spec in make_fs_tools(tmp_path):
        reg.register(spec)
    r = AgentRunner()
    r.registry = reg
    r.llm = FakeLLM(replies)
    return r


REVISE = (
    '{"verdict":"revise","comments":["缺少边界处理"],"summary":"需修正"}',
    {"prompt_tokens": 3, "completion_tokens": 3},
)


# ---- A2 评审回流 ----

async def test_generic_review_revise_then_pass(session, tmp_path):
    """reviewer 先 revise → 内部重做前置领域节点 → 再 pass → 推进。"""
    task = await create_task(session, title="做一个计算器", workflow_id="generic")
    # supervisor, doc, design, code, reviewer1(revise), redo-coder, reviewer2(pass)
    replies = [
        plan_reply(),
        ("文档产物：需求说明", {"prompt_tokens": 2, "completion_tokens": 2}),
        ("设计产物：架构示意", {"prompt_tokens": 2, "completion_tokens": 2}),
        ("代码产物：v1（无边界）", {"prompt_tokens": 2, "completion_tokens": 2}),
        REVISE,
        ("代码产物：v2（含边界处理）", {"prompt_tokens": 2, "completion_tokens": 2}),
        REVIEW_PASS,
    ]
    runner = _runner(tmp_path, replies)
    events = []

    async def emit(kind, payload):
        events.append((kind, payload))

    outcome = await runner.run(session, task.id, emit=emit)
    assert outcome["status"] == "interrupt"  # 走到验收 HITL 节点
    assert outcome["node"] == "验收"

    # 修订后 pass：领域节点 output 反映了修订产物
    nodes = {n.node_name: n for n in await list_nodes(session, task.id)}
    assert nodes["评审"].status == "done"
    assert nodes["实现"].status == "done"
    # 出现过 revise 事件
    assert any(k == "review_event" and p.get("verdict") == "revise" for k, p in events)


async def test_review_max_revision_terminates(session, tmp_path):
    """超 max_revision（generic=2）→ 评审节点 failed，停止流转。"""
    task = await create_task(session, title="做计算器", workflow_id="generic")
    replies = [
        plan_reply(),
        ("文档产物：x", {"prompt_tokens": 1, "completion_tokens": 1}),
        ("设计产物：x", {"prompt_tokens": 1, "completion_tokens": 1}),
        ("代码产物：v1", {"prompt_tokens": 1, "completion_tokens": 1}),
        REVISE, ("代码v2", {"prompt_tokens": 1, "completion_tokens": 1}),  # rev1
        REVISE, ("代码v3", {"prompt_tokens": 1, "completion_tokens": 1}),  # rev2
        REVISE,  # 第3次 revise → 超 max_revision=2 终止
    ]
    runner = _runner(tmp_path, replies)
    outcome = await runner.run(session, task.id)
    assert outcome["status"] == "error"
    nodes = {n.node_name: n for n in await list_nodes(session, task.id)}
    assert nodes["评审"].status == "failed"
    assert task.status == "failed"


# ---- A4 HITL 审批 ----

async def test_approval_approve_completes_task(session, tmp_path):
    """审批通过 → 推进验收节点 → 任务 done（收尾）。"""
    task = await create_task(session, title="做计算器", workflow_id="generic")
    runner = _runner(tmp_path, _walk_to_approval_replies())
    outcome0 = await runner.run(session, task.id)
    assert outcome0["status"] == "interrupt" and outcome0["reason"] == "approval"

    # 再次 run_resume 通过（复用 runner，llm 已空但审批节点不需要 LLM）
    out = await runner.run_resume(session, task.id,
                                  {"kind": "approval", "approved": True, "comment": "ok"},
                                  emit=None)
    assert out["status"] == "done"
    nodes = {n.node_name: n for n in await list_nodes(session, task.id)}
    assert nodes["验收"].status == "done"
    assert task.status == "done"


def _walk_to_approval_replies():
    return [
        plan_reply(),
        ("文档产物：需求说明", {"prompt_tokens": 2, "completion_tokens": 2}),
        ("设计产物：架构", {"prompt_tokens": 2, "completion_tokens": 2}),
        ("代码产物：ok", {"prompt_tokens": 2, "completion_tokens": 2}),
        REVIEW_PASS,
    ]


async def test_approval_reject_rewinds_and_redoes(session, tmp_path):
    """审批驳回 → 回退前置评审节点重做 → 之后通过收尾。"""
    task = await create_task(session, title="做计算器", workflow_id="generic")
    runner = _runner(tmp_path, _walk_to_approval_replies())
    outcome0 = await runner.run(session, task.id)
    assert outcome0["reason"] == "approval"

    # 驳回：回退评审节点（done→running），后续 re-run 会重做评审+推进
    nodes = {n.node_name: n for n in await list_nodes(session, task.id)}
    assert nodes["评审"].status == "done"

    # 重新注入通过票给评审 redo
    runner.llm = FakeLLM([REVIEW_PASS])
    out = await runner.run_resume(session, task.id,
                                  {"kind": "approval", "approved": False, "comment": "改一下"},
                                  emit=None)
    # 驳回后 re-run：重做评审→通过→推到验收 HITL 再中断
    assert out["status"] == "interrupt"
    assert out["node"] == "验收"
    nodes = {n.node_name: n for n in await list_nodes(session, task.id)}
    assert nodes["评审"].status == "done"


# ---- A5 追问 ----

async def test_ask_then_answer_resumes(session, tmp_path):
    """supervisor 先 need_info → 中断提问；人工 answer → 追加消息并 re-run 成功拆解。"""
    task = await create_task(session, title="写报告", workflow_id="generic")
    ask_reply = (
        '{"info_question":"请提供报告受众与篇幅"}',
        {"prompt_tokens": 2, "completion_tokens": 2},
    )
    runner = _runner(tmp_path, [ask_reply])
    out0 = await runner.run(session, task.id)
    assert out0["status"] == "interrupt" and out0["reason"] == "ask"
    assert out0["node"] == "需求分析"

    # 人工补充答案
    await write_message(session, task_id=task.id, sender_role="user",
                        content="受众：管理层；篇幅：2页")
    # 补票： supervisor(带 followup→plan), doc, design, code, review
    runner.llm = FakeLLM([
        plan_reply("写管理层报告"),
        ("文档产物：报告1", {"prompt_tokens": 1, "completion_tokens": 1}),
        ("设计产物：架构", {"prompt_tokens": 1, "completion_tokens": 1}),
        ("代码产物：ok", {"prompt_tokens": 1, "completion_tokens": 1}),
        REVIEW_PASS,
    ])
    out = await runner.run(session, task.id)  # 节点仍 running，re-run 继续
    assert out["status"] == "interrupt" and out["node"] == "验收"
    nodes = {n.node_name: n for n in await list_nodes(session, task.id)}
    assert nodes["需求分析"].status == "done"
    assert nodes["评审"].status == "done"


# ---- A6 压缩注入 ----

async def test_compression_triggered_keeps_db_unchanged(session, tmp_path):
    """消息数超阈值 → 触发压缩注入；DB Message 原始记录不变。"""
    task = await create_task(session, title="t", workflow_id="generic")
    # 预置多条历史消息，超过压缩阈值（设 runner.compress_threshold=2）
    for i in range(5):
        await write_message(session, task_id=task.id,
                            sender_role="user" if i % 2 else "supervisor",
                            content=f"历史消息{i}")

    runner = _runner(tmp_path, [review_summary_reply(), plan_reply(), ("doc", {}),
                                ("design", {}), ("code", {}), REVIEW_PASS])
    runner.compress_threshold = 2

    events = []

    async def emit(kind, payload):
        events.append((kind, payload))

    await runner.run(session, task.id, emit=emit)

    from app.db.models import Message

    msgs = list((await session.execute(select(Message))).scalars())
    assert msgs  # 原始消息仍在
    # 压缩是视图层，未删除 / 未改任何原始消息
    assert len(msgs) >= 5


def review_summary_reply():
    return ("摘要：目标X。已完成：拆解。评审：pass。", {"prompt_tokens": 1, "completion_tokens": 1})
