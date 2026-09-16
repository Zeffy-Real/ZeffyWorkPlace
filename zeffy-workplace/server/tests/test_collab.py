"""P7-D2 多 Agent 协作 · 单元+端到端测试。

覆盖：
1. context：敏感字段负面清单脱敏 + SharedContext 跨任务/跨 owner 隔离
2. audit：协作审计记录/查询 + JSON 落盘
3. parallel：run_parallel 成功合并 / 任一失败整批回滚(cancel) / 并发预算
4. split_substeps 从计划提取子步骤
5. ReviewGate：迭代上限 / 死循环检测 / 超时 / 人工裁决
6. runner：_collab_enabled 总闸、_plansafe 脱敏、parallel 模板端到端（并行执行收敛回主节点）
"""
from __future__ import annotations

import asyncio

from app.agents.runner import AgentRunner
from app.collab import parallel as par
from app.collab.audit import CollabAudit
from app.collab.context import SharedContext, desensitize, is_sensitive
from app.collab.parallel import SubStep, split_substeps
from app.collab.review import ReviewGate
from app.config import get_settings
from app.db.repos import create_task, list_nodes
from app.tools.fs import make_fs_tools
from app.tools.registry import ToolRegistry
from app.workflow.templates import NODE_COLLAB, get_template
from tests.conftest import REVIEW_PASS, FakeLLM, plan_reply

# ---- context ----

def test_desensitize_negative_list():
    d = desensitize({"api_key": "secret", "plan": {"authorization": "tok", "goal": "build",
                                                   "meta": {"private_key": "x"}}})
    assert d["api_key"].startswith("***")
    assert d["plan"]["authorization"].startswith("***")
    assert d["plan"]["meta"]["private_key"].startswith("***")
    assert d["plan"]["goal"] == "build"


def test_is_sensitive_variants():
    for k in ("api_key", "API-KEY", "access_token", "token", "password", "audit", "private_key"):
        assert is_sensitive(k), k
    assert not is_sensitive("goal")
    assert not is_sensitive("title")


def test_shared_context_cross_owner_isolated():
    sc = SharedContext(task_id="task-1", owner="u1")
    sc.put("plan", {"a": 1})
    assert sc.get("plan", requester_task="task-1", requester_owner="u1") == {"a": 1}
    assert sc.get("plan", requester_task="task-2") is None  # 跨任务
    assert sc.get("plan", requester_task="task-1", requester_owner="u2") is None  # 跨 owner
    assert sc.snapshot(owner="u2") == {}  # 跨 owner 快照为空


# ---- audit ----

def test_audit_record_and_query(tmp_path):
    a = CollabAudit(str(tmp_path / "collab"), capacity=10)
    a.record("parallel_exec", task_id="t1", total=2, ok=True)
    a.record("review_abort", task_id="t2", ok=False)
    assert len(a.for_task("t1")) == 1
    assert a.for_task("t1")[0]["event"] == "parallel_exec"
    # 落盘可重载
    a2 = CollabAudit(str(tmp_path / "collab"), capacity=10)
    assert len(a2.for_task("t1")) == 1


# ---- parallel ----

async def _echo(step: SubStep, index: int) -> str:
    return f"{step.id}:{step.title}"


async def test_run_parallel_merges_ok():
    steps = [SubStep(id="s1", role="doer", title="A", description=""), SubStep(id="s2", role="doer", title="B", description="")]
    res = await par.run_parallel(steps, _echo, pool=2)
    assert res.ok
    assert "s1:A" in res.text and "s2:B" in res.text
    assert [s.id for s in res.subs] == ["s1", "s2"]


async def test_run_parallel_failure_rolls_back_all():
    steps = [SubStep(id="s1", role="doer", title="A", description=""),
             SubStep(id="s2", role="doer", title="B", description="")]
    calls: list[str] = []

    async def flaky(step, index):
        calls.append(step.id)
        if step.id == "s2":
            raise RuntimeError("boom")
        return step.id

    res = await par.run_parallel(steps, flaky, pool=2)
    assert not res.ok
    assert "boom" in res.error
    assert {s.id for s in res.subs if not s.ok} >= {"s2"}


async def test_run_parallel_respects_pool_max_concurrency():
    top = 0
    cur = 0
    lock = asyncio.Lock()
    steps = [SubStep(id=f"s{i}", role="doer", title="", description="") for i in range(6)]

    async def slow(step, index):
        nonlocal top, cur
        async with lock:
            cur += 1
            top = max(top, cur)
        await asyncio.sleep(0.01)
        async with lock:
            cur -= 1
        return step.id

    res = await par.run_parallel(steps, slow, pool=2)
    assert res.ok
    assert top <= 2


def test_split_substeps():
    plan = {"goal": "g", "substeps": [
        {"id": "s1", "role": "coder", "title": "A", "acceptance_criteria": "c"},
        {"id": "s2", "role": "designer", "title": "B", "acceptance_criteria": "c"},
        {"nope": True},  # 被跳过
    ]}
    subs = split_substeps(plan)
    assert [s.id for s in subs] == ["s1", "s2"]
    assert subs[1].role == "designer"
    assert split_substeps(None) == []


# ---- ReviewGate ----

def test_review_gate_iter_limit():
    g = ReviewGate(max_iter=2, timeout=999, loop_penalty=99)
    assert g.check(1, ["a"]) is None
    assert g.check(2, ["a"]) is None
    assert g.check(3, ["a"]) == "too_many_iter"


def test_review_gate_loop_detection():
    g = ReviewGate(max_iter=99, timeout=999, loop_penalty=2)
    g.check(1, ["缺少格式"])
    assert g.check(2, ["缺少格式"]) == "loop_detected"


def test_review_gate_timeout():
    g = ReviewGate(max_iter=99, timeout=0, loop_penalty=99)
    assert g.check(1, ["a"]) == "timeout"


def test_review_gate_human_override():
    g = ReviewGate(max_iter=1, timeout=999, loop_penalty=99)
    g.human_override(by="admin")
    assert g.check(1, ["a"]) == "human_override"


# ---- runner ----

def _runner(tmp_path, replies) -> AgentRunner:
    reg = ToolRegistry()
    for spec in make_fs_tools(tmp_path):
        reg.register(spec)
    r = AgentRunner()
    r.registry = reg
    r.llm = FakeLLM(replies)
    return r


def test_collab_enabled_gate(monkeypatch):
    r = AgentRunner()
    monkeypatch.setattr(get_settings(), "AGENT_COLLAB_ENABLED", True)
    monkeypatch.setattr(get_settings(), "ARTIFACT_META_ENABLED", True)
    assert r._collab_enabled() is True
    monkeypatch.setattr(get_settings(), "ARTIFACT_META_ENABLED", False)
    assert r._collab_enabled() is False
    r.collab_override = True
    assert r._collab_enabled() is True


def test_plansafe_desensitizes_when_collab(monkeypatch):
    r = AgentRunner()
    r.collab_override = True
    plan = {"goal": "g", "substeps": [{"id": "s1", "api_key": "SECRET"}]}
    safe = r._plansafe(plan)
    assert safe["substeps"][0]["api_key"].startswith("***")
    # 关闭时不改
    r.collab_override = False
    assert r._plansafe(plan) == plan


def test_parallel_template_has_collab_node():
    tpl = get_template("parallel")
    assert tpl.key == "parallel"
    collab_node = [n for n in tpl.nodes if n.type == NODE_COLLAB]
    assert collab_node and collab_node[0].name == "并行执行"


def _parallel_replies():
    return [
        plan_reply(),  # supervisor 拆解 s1/s2
        ("产物：子步骤A完成", {"prompt_tokens": 4, "completion_tokens": 4}),  # collab s1
        ("产物：子步骤B完成", {"prompt_tokens": 4, "completion_tokens": 4}),  # collab s2
        REVIEW_PASS,  # reviewer
    ]


async def test_runner_parallel_e2e_syncs_to_main_node(session, tmp_path, monkeypatch):
    """并行执行收敛回主 TaskNode，且协作审计记录，最终 interrupt 于验收。"""
    # 隔离协作审计到临时目录
    audit = CollabAudit(str(tmp_path / "collab-audit"))
    monkeypatch.setattr("app.collab.audit.get_collab_audit", lambda: audit)

    task = await create_task(session, title="做一个并行任务", workflow_id="parallel")
    runner = _runner(tmp_path, _parallel_replies())
    runner.collab_override = True

    events = []
    async def emit(kind, payload):
        events.append((kind, payload))

    outcome = await runner.run(session, task.id, emit=emit)
    assert outcome["status"] == "interrupt"
    assert outcome["node"] == "验收"

    nodes = {n.node_name: n for n in await list_nodes(session, task.id)}
    # 并行执行节点已完成且含合并产物（两块子步文本都被并进去）
    assert nodes["并行执行"].status == "done"
    out_text = (nodes["并行执行"].output or {}).get("text", "")
    assert "子步骤A完成" in out_text and "子步骤B完成" in out_text
    # 评审也已完成（走到验收挂起）
    assert nodes["评审"].status == "done"
    # 协作审计已记录
    assert any(e["event"] == "parallel_exec" for e in audit.for_task(task.id))
    assert any(k == "collab_event" for k, _ in events)


async def test_runner_parallel_review_loop_detection_fails(session, tmp_path, monkeypatch):
    """评审意见死循环 → 收敛门闸终止（action=error，节点 failed）。"""
    audit = CollabAudit(str(tmp_path / "collab-audit2"))
    monkeypatch.setattr("app.collab.audit.get_collab_audit", lambda: audit)
    monkeypatch.setattr(get_settings(), "AGENT_COLLAB_REVIEW_MAX_ITER", 3)
    monkeypatch.setattr(get_settings(), "AGENT_COLLAB_REVIEW_TIMEOUT", 999)

    loop_comment = ('''{"verdict":"revise","comments":["缺少格式"],"summary":"x"}''',
                    {"prompt_tokens": 2, "completion_tokens": 2})
    replies = [
        plan_reply(),
        ("产物A", {"prompt_tokens": 2, "completion_tokens": 2}),
        ("产物B", {"prompt_tokens": 2, "completion_tokens": 2}),
        loop_comment,  # reviewer 1 → revise, 记录 "缺少格式"
        ("产物A2", {"prompt_tokens": 2, "completion_tokens": 2}),  # 修订重做
        loop_comment,  # reviewer 2 → revise, 同意见命中 loop_penalty=2
        ("产物A3", {"prompt_tokens": 2, "completion_tokens": 2}),  # 修订重做
        loop_comment,  # reviewer 3 → revise, 3 次
    ]
    task = await create_task(session, title="死循环评审", workflow_id="parallel")
    runner = _runner(tmp_path, replies)
    runner.collab_override = True
    outcome = await runner.run(session, task.id)
    # 通道三：loop_detected 在下一次 check 触发 → error；若超 max_iter 也 error
    assert outcome["status"] == "error"
    nodes = {n.node_name: n for n in await list_nodes(session, task.id)}
    assert nodes["评审"].status == "failed"
    assert any(e["event"] == "review_abort" for e in audit.for_task(task.id))
