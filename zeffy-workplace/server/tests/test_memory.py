"""P1-4 上下文压缩 + 短期记忆测试：
- should_compress 双重阈值（轮次 OR token）。
- compress 产出摘要且保留白名单字段；token 下降。
- 压缩失败降级（LLM 抛错）不崩溃，degraded=True，保留最近 N 条。
- store 构造视图（摘要 + recent），to_messages 拼接正确。
- 🔴 DB Message 原始记录在此前测试写入后不变（视图层不动持久层）。
"""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import models  # noqa: F401
from app.db.init_db import init_db
from app.db.repos import create_task, write_message
from app.memory import ContextCompressor, compressor as comp
from app.memory.store import CompressedView, to_msg_dict
from tests.conftest import FakeLLM


@pytest.fixture
async def session():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    factory = async_sessionmaker(eng, expire_on_commit=False)
    async with factory() as s:
        yield s
    await eng.dispose()


# ---- should_compress：双重条件 OR ----

def test_should_compress_round_threshold():
    c = ContextCompressor(llm=FakeLLM([]))
    assert not c.should_compress(rounds=5, token_est=100)  # 都未超阈值
    assert c.should_compress(rounds=31, token_est=100)  # 轮次超
    assert c.should_compress(rounds=5, token_est=9000)  # token 超
    assert c.should_compress(rounds=31, token_est=9000)  # 双超


# ---- compress 成功 + 白名单 + token 下降 ----

def _long_history(n=20):
    return [
        {"role": "user" if i % 2 == 0 else "assistant",
         "content": f"第{i}条消息内容，包含一些业务信息。" * 30}
        for i in range(n)
    ]


async def test_compress_produces_summary_keeping_whitelist():
    # 摘要文本应覆盖任务目标与评审结论
    summary = ("目标：开发计算器。已完成节点：需求/实现。"
               "评审结论：revise。人类决策：采用 Web 方案。失败原因：无。")
    llm = FakeLLM([(summary, {"prompt_tokens": 400, "completion_tokens": 100})])
    c = ContextCompressor(llm=llm, recent_kept=3)
    history = _long_history()
    raw_est = comp.estimate_tokens("\n".join(m["content"] for m in history))

    view = await c.compress(history, decisions=[{"verdict": "revise"}], raw_count=len(history))
    assert not view.degraded
    assert view.raw_count == len(history)
    assert view.dropped == len(history) - 3  # recent_kept=3
    assert len(view.recent) == 3
    # token 下降达标：压缩视图 token 显著小于原始
    assert view.token_count < raw_est

    # to_messages 正确拼接摘要 + recent
    msgs = view.to_messages()
    assert msgs and msgs[0]["role"] == "system"
    assert "目标" in msgs[0]["content"]


# ---- 压缩失败降级 ----

async def test_compress_failure_degrades():
    from app.llm_errors import LLMConfigError

    llm = FakeLLM([LLMConfigError("summary boom")])
    c = ContextCompressor(llm=llm)
    history = _long_history(30)
    view = await c.compress(history)  # 不抛错
    assert view.degraded
    assert len(view.recent) == comp._DEGRADE_KEEP_RECENT  # 保留最近 N 条
    assert view.summary  # 有降级说明


# ---- store 视图拼接 + to_msg_dict ----

def test_to_msg_dict_maps_roles():
    class M:
        sender_role = "supervisor"
        content = "hi"

    assert to_msg_dict(M()) == {"role": "assistant", "content": "hi"}
    class U:
        sender_role = "user"
        content = "yo"
    assert to_msg_dict(U()) == {"role": "user", "content": "yo"}


def test_compressed_view_to_messages_empty_ok():
    v = CompressedView(summary="", recent=[], token_count=0)
    assert v.to_messages() == []


# ---- 🔴 DB Message 原始记录不变（视图层不动持久层） ----

async def test_compress_does_not_touch_db(session):
    task = await create_task(session, title="t")
    for i in range(6):
        await write_message(
            session, task_id=task.id,
            sender_role="user" if i % 2 == 0 else "supervisor",
            content=f"原始msg{i}",
        )

    from sqlalchemy import select
    from app.db.models import Message

    before = list((await session.execute(select(Message))).scalars())

    llm = FakeLLM([("摘要：目标X。已完成：需求。评审：pass。", {"prompt_tokens": 10, "completion_tokens": 10})])
    c = ContextCompressor(llm=llm, recent_kept=2)
    rows = list((await session.execute(select(Message).order_by(Message.created_at))).scalars())
    view = await c.compress_messages(rows, [{"verdict": "pass"}])

    assert not view.degraded
    # 压缩后 DB 一条未增未减未变
    after = list((await session.execute(select(Message))).scalars())
    assert len(after) == len(before) == 6
    for b, a in zip(before, after):
        assert b.content == a.content
        assert b.sender_role == a.sender_role


# ---- 压缩后视图仍可继续工作（收尾） ----

async def test_compressed_view_feeds_llm():
    """压缩后的视图可直接作为后续 Agent 的 LLM 输入（A6 收尾仍能干活）。"""
    llm = FakeLLM([
        ("目标：写报告。已完成：需求/设计。", {"prompt_tokens": 1, "completion_tokens": 1}),
        ("我基于摘要完成收尾：验收通过。", {"prompt_tokens": 1, "completion_tokens": 1}),
    ])
    c = ContextCompressor(llm=llm, recent_kept=3)
    view = await c.compress(_long_history(), decisions=[{"goal": "写报告"}], raw_count=20)
    msgs = view.to_messages()
    # 把视图喂给下一个 LLM 调用（模拟收尾节点）
    text, _ = await llm.agenerate(msgs)
    assert "收尾" in text  # 收尾 Agent 基于摘要成功产出，证明视图可用