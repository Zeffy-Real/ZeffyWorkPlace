"""P2 DAG：依赖建模 + 拓扑校验 + 就绪激活。"""

import pytest

from app.db import models  # noqa: F401
from app.db.repos import create_task, list_nodes
from app.workflow import engine
from app.workflow.state_machine import WorkflowStateError
from app.workflow.templates import TEMPLATES, WorkflowNodeSpec, WorkflowTemplate


def _tpl(nodes, key="t") -> WorkflowTemplate:
    return WorkflowTemplate(key=key, max_revision=2, max_rounds=5, nodes=nodes)


def test_topology_rejects_unknown_dep():
    t = _tpl([WorkflowNodeSpec("A", "doer"), WorkflowNodeSpec("B", "doer", depends_on=["X"])])
    with pytest.raises(WorkflowStateError):
        engine._validate_dag(t)


def test_topology_rejects_self_dep():
    t = _tpl([WorkflowNodeSpec("A", "doer", depends_on=["A"])])
    with pytest.raises(WorkflowStateError):
        engine._validate_dag(t)


def test_topology_rejects_cycle():
    t = _tpl([
        WorkflowNodeSpec("A", "doer", depends_on=["C"]),
        WorkflowNodeSpec("B", "doer", depends_on=["A"]),
        WorkflowNodeSpec("C", "doer", depends_on=["B"]),
    ])
    with pytest.raises(WorkflowStateError):
        engine._validate_dag(t)


def test_topology_accepts_valid_dag():
    t = _tpl([
        WorkflowNodeSpec("A", "doer"),
        WorkflowNodeSpec("B", "doer", depends_on=["A"]),
        WorkflowNodeSpec("C", "doer", depends_on=["A"]),
    ])
    engine._validate_dag(t)


def test_duplicate_node_names_rejected():
    t = _tpl([WorkflowNodeSpec("A", "doer"), WorkflowNodeSpec("A", "doer")])
    with pytest.raises(WorkflowStateError):
        engine._validate_dag(t)


async def test_prepare_stores_depends_on(session):
    task = await create_task(session, title="t", workflow_id="generic")
    await engine.prepare(session, task)
    nodes = {n.node_name: n for n in await list_nodes(session, task.id)}
    assert nodes["需求分析"].depends_on in (None, [])


async def test_activate_respects_depends_on(session, monkeypatch):
    """只有依赖 done 的 pending 节点才会被激活（保持单活）。"""
    from app.workflow.templates import get_template

    spec_tpl = WorkflowTemplate(
        key="dag_x", max_revision=2, max_rounds=5,
        nodes=[WorkflowNodeSpec("A", "doer"),
               WorkflowNodeSpec("B", "doer", depends_on=["A"])],
    )
    monkeypatch.setitem(TEMPLATES, "dag_x", spec_tpl)
    task = await create_task(session, title="t", workflow_id="dag_x")
    await engine.prepare(session, task)

    act = await engine._activate_next(session, task, get_template("dag_x"))
    assert act is not None and act.node_name == "A"
    # B 依赖 A 未 done，不应再激活其它节点（单活）
    act2 = await engine._activate_next(session, task, get_template("dag_x"))
    assert act2 is None
