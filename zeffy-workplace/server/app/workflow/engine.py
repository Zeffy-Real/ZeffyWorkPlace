"""工作流引擎（P1-2）。

业务真相源 = 自研状态机 + PG ``TaskNode`` 表；本 engine 是任务流转的**唯一驱动者**。
约束（强制）：**禁止 LangGraph 直接修改 TaskNode / 驱动业务流转**——
LangGraph 仅作 Agent 内部执行图（其 checkpoint 只是 Agent 单次执行快照）。

职责：
- ``start``：按模板创建全部 TaskNode，并激活首节点（pending→running）。
- ``advance``：把指定节点从 pending→running→done（乐观锁过渡），写 output，
  再定位下一 pending 节点置 running；全部完成则任务收尾（Task → done）。

并发/非法防护：节点状态更新一律走**行级乐观锁**
（``UPDATE ... WHERE id=? AND status=预期旧态``），冲突/非法抛 ``WorkflowStateError``。
"""

from __future__ import annotations

from app.db import repos
from app.db.models import Task, TaskNode
from app.workflow.state_machine import DONE, PENDING, RUNNING, WorkflowStateError
from app.workflow.templates import WorkflowNodeSpec, WorkflowTemplate, get_template


class WorkflowEngine:
    async def start(self, session, task: Task) -> TaskNode:
        """按模板初始化节点并激活首节点。已初始化则幂等重建激活。"""
        tpl = get_template(task.workflow_id)
        existing = await repos.list_nodes(session, task.id)
        if not existing:
            for spec in tpl.nodes:
                await repos.create_node(session, task_id=task.id, node_name=spec.name)
        await repos.set_task_status(session, task.id, RUNNING)
        await session.commit()
        activated = await self._activate_next(session, task, tpl)
        if activated is None:
            raise WorkflowStateError(f"工作流初始化失败：任务 {task.id} 无可激活节点")
        return activated

    async def advance(self, session, task_id: str, node_id: str, result: dict | None = None):
        """推进指定节点至 done，并激活下一节点。

        返回下一 running 节点；全部完成则返回 None（任务已收尾）。
        """
        task = await self._require_task(session, task_id)
        tpl = get_template(task.workflow_id)
        ordered = await self._ordered_nodes(session, task, tpl)
        target = next((n for _, n in ordered if n.id == node_id), None)
        if target is None:
            raise WorkflowStateError(f"节点不存在于任务 {task_id}：{node_id}")

        # 顺序约束：前置节点必须全部 done，禁止越序推进。
        idx = next(i for i, (_, n) in enumerate(ordered) if n.id == node_id)
        for _, node in ordered[:idx]:
            if node.status != DONE:
                raise WorkflowStateError(
                    f"前置节点未完成：{node.node_name}={node.status}，禁止越序推进 {target.node_name}"
                )

        # 1) pending → running（若仍是 pending；并发下可能已被他人激活）
        if target.status == PENDING:
            if not await repos.set_node_status(session, target.id, PENDING, RUNNING):
                raise WorkflowStateError(f"并发冲突：节点 {target.id} 非 pending")
        elif target.status != RUNNING:
            raise WorkflowStateError(f"节点状态不可推进：{target.id} = {target.status}")

        # 2) 写 output + running → done（乐观锁）
        await repos.set_node_output(session, target.id, result)
        if not await repos.set_node_status(session, target.id, RUNNING, DONE):
            raise WorkflowStateError(f"并发冲突：节点 {target.id} 无法置 done")
        await session.commit()

        # 3) 激活下一 pending 节点；若无则任务收尾
        nxt = await self._activate_next(session, task, tpl)
        if nxt is None:
            await repos.set_task_status(session, task.id, DONE)
            await session.commit()
        return nxt

    async def _activate_next(self, session, task: Task, tpl: WorkflowTemplate) -> TaskNode | None:
        """把第一个 pending 节点置 running（按模板顺序）。无则返回 None。"""
        for _spec, node in await self._ordered_nodes(session, task, tpl):
            if node.status == PENDING and await repos.set_node_status(
                session, node.id, PENDING, RUNNING
            ):
                await session.commit()
                return node
        return None

    async def _require_task(self, session, task_id: str) -> Task:
        task = await repos.get_task(session, task_id)
        if task is None:
            raise WorkflowStateError(f"任务不存在：{task_id}")
        return task

    async def _ordered_nodes(
        self, session, task: Task, tpl: WorkflowTemplate
    ) -> list[tuple[WorkflowNodeSpec, TaskNode]]:
        """按模板节点顺序返回 (spec, node) 列表（顺序权威来自模板，非 created_at）。"""
        nodes = {n.node_name: n for n in await repos.list_nodes(session, task.id)}
        return [(spec, nodes[spec.name]) for spec in tpl.nodes]


engine = WorkflowEngine()
