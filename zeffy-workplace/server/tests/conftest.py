"""共享测试夹具：内存 SQLite session + 可编排的 FakeLLM。"""

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import models  # noqa: F401 确保注册进 metadata
from app.db.init_db import init_db


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


class FakeLLM:
    """按顺序返回预设回复的假 LLM（含异常注入）。"""

    def __init__(self, replies):
        # replies: list of (text, usage) 或 (exc,)
        self._replies = list(replies)
        self._calls = []

    async def agenerate(self, messages, *, temperature=None, max_tokens=None, stop=None, **kw):
        self._calls.append(messages)
        item = self._replies.pop(0)
        if isinstance(item, BaseException):
            raise item
        text, usage = item
        return text, usage

    @property
    def calls(self):
        return self._calls


def plan_reply(goal="写一个计算器"):
    """supervisor 拆解回复（均带 acceptance_criteria）。"""
    return (
        '{"goal": "%s", "substeps": ['
        '{"id":"s1","role":"planner","title":"拆分需求","description":"d","acceptance_criteria":"含3个以上子步骤"},'
        '{"id":"s2","role":"doer","title":"实现","description":"d","acceptance_criteria":"可运行无报错"}'
        "]}",
        {"prompt_tokens": 20, "completion_tokens": 10},
    )


REVIEW_PASS = (
    '{"verdict":"pass","comments":["全部达标"],"summary":"ok"}',
    {"prompt_tokens": 5, "completion_tokens": 5},
)


@pytest.fixture
def fake_llm():
    """无参 fixture：返回一个可配置 replies 的 FakeLLM 工厂。"""

    def make(replies):
        return FakeLLM(replies)

    return make


@pytest.fixture
def run_in_event_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()
