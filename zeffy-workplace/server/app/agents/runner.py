"""AgentRunner（P1-3）：按工作流节点把执行权交给对应 Agent，回写结果并推进下一个节点。

职责：
- 按模板顺序推进：对每个 auto 节点，依据 ``node.role`` 派发对应 Agent；执行、落审计、
  写消息、触发 ``engine.advance`` 把节点置 done 并激活下一个节点。
- ``human`` / ``hitl`` 节点：派发 stop，写入人工介入/审批消息，返回中断标记（P1-5 接审批流程）。
- 异常分层（🔴）：``LLMConfigError``（不可恢复）与重试耗尽的可恢复异常均视为该节点失败——
  把节点 running→failed + 写 error + 审计，停止流转；**禁止静默吞异常**。
- 所有 Agent 执行（入参 / 输出 / usage / 决策 / 异常）完整写入 AuditLog。

⚠️ 本组件只读 ``engine.advance`` 驱动 TaskNode 流转，不自行改写状态——
**TaskNode 状态权威仍在 WorkflowEngine（唯一真相源）。**
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.agents.base import AgentResult, BaseAgent
from app.agents.domain import DomainAgent
from app.agents.reviewer import ReviewerAgent
from app.agents.supervisor import SupervisorAgent
from app.db import repos
from app.db.models import Task, TaskNode
from app.llm_errors import LLMError
from app.workflow.state_machine import FAILED, RUNNING, WorkflowStateError
from app.workflow.templates import NODE_HITL, NODE_HUMAN, WorkflowNodeSpec, get_template

logger = logging.getLogger(__name__)

# 每个任务最大推进步数（防死循环；另有模板 max_rounds 兜底在 P1-5 强制）。
RUNNER_MAX_STEPS = 64

# 事件推送回调：kind + payload（由 WS 层实现并发往前端）。
EmitCb = Callable[[str, dict[str, Any]], Awaitable[None]]


class AgentRunner:
    def __init__(self) -> None:
        # 每次 run 前由 build_runner 注入工具注册表。
        self.registry: Any = None
        self.llm: Any = None  # 注入同步 mock，None 则各 Agent 走全局 get_llm()
        self._own_registry = False

    # ---- 对外入口 ----
    async def run(self, session, task_id: str, *, emit: EmitCb | None = None) -> dict:
        """执行一段工作流直到完成 / 遇到 human/hitl 中断 / 出错。"""
        task = await self._require_task(session, task_id)
        tpl = get_template(task.workflow_id)

        if not await repos.list_nodes(session, task_id):
            from app.workflow import engine

            await engine.start(session, task)

        context: dict[str, Any] = {"task": task, "plan": None, "round": 0}
        for _ in range(RUNNER_MAX_STEPS):
            ordered = await self._ordered(session, task, tpl)
            if all(n.status == "done" for _, n in ordered):
                return {"status": "done", "node": None}

            active = next((n for _, n in ordered if n.status == RUNNING), None)
            if active is None:
                return {"status": "idle", "node": None}  # 理论不可达，防御

            spec = next(s for s, _ in ordered if s.name == active.node_name)

            # human / hitl 节点：中断，等人工
            if spec.type in {NODE_HUMAN, NODE_HITL}:
                await repos.write_message(
                    session, task_id=task_id, sender_role="system",
                    content=f"节点「{active.node_name}」需人工介入/审批（type={spec.type}），已挂起。",
                    msg_type="approval_card",
                )
                await self._audit(session, task_id, "supervisor", "interrupt",
                                  {"node": active.node_name, "type": spec.type})
                if emit:
                    await emit("task_node_update",
                               {"task_id": task_id, "node_id": active.id,
                                "node_name": active.node_name, "status": "interrupt",
                                "reason": f"type={spec.type}"})
                return {"status": "interrupt", "node": active.node_name}

            # 派发 Agent（异常统一转 failed，不静默、不丢上下文）
            try:
                result = await self._dispatch(session, spec, active, context)
            except LLMError as exc:
                await self._audit(session, task_id, spec.role, "llm_error",
                                  {"node": active.node_name, "error": str(exc),
                                   "type": type(exc).__name__})
                await self._fail_node(session, task_id, active, spec.role, str(exc))
                if emit:
                    await emit("task_node_update",
                               {"task_id": task_id, "node_id": active.id,
                                "node_name": active.node_name, "status": "failed",
                                "error": str(exc)})
                return {"status": "error", "node": active.node_name, "error": str(exc)}
            output = self._result_to_output(spec, result)

            # 审计（入参/输出/usage/决策/异常 全量）
            await self._audit(session, task_id, spec.role, "agent_run",
                              {"node": active.node_name, "input": self._node_input(spec, context),
                               "result": result.to_log()})
            if not result.ok:
                await self._fail_node(session, task_id, active, spec.role, result.error)
                if emit:
                    await emit("task_node_update",
                               {"task_id": task_id, "node_id": active.id,
                                "node_name": active.node_name, "status": "failed",
                                "error": result.error})
                return {"status": "error", "node": active.node_name, "error": result.error}

            # 落消息 + 推进
            await repos.write_message(session, task_id=task_id, sender_role=spec.role,
                                      content=result.text, msg_type="text")
            if emit:
                await emit("agent_message", {"task_id": task_id, "node_id": active.id,
                                             "node_name": active.node_name, "role": spec.role,
                                             "text": result.text})
            nxt = await self._advance(session, task_id, active.id, output)
            context[f"output.{active.node_name}"] = output
            context["last_output"] = output
            if spec.role == "supervisor" and result.decision:
                context["plan"] = result.decision
            if emit:
                await emit("task_node_update", {"task_id": task_id, "node_id": active.id,
                                                "node_name": active.node_name, "status": "done",
                                                "next": nxt.node_name if nxt else None})

            if nxt is None:
                return {"status": "done", "node": active.node_name}

        raise RuntimeError(f"任务 {task_id} 超过最大推进步数 {RUNNER_MAX_STEPS}")

    # ---- 派发 ----
    async def _dispatch(self, session, spec: WorkflowNodeSpec, node: TaskNode,
                        context: dict) -> AgentResult:
        agent = self._make_agent(spec.role)
        task: Task = context["task"]
        plan = context.get("plan") or {}
        if spec.role == "supervisor":
            return await agent.run(task_title=task.title, task_description=task.description)
        if spec.role == "reviewer":
            criteria = json.dumps(plan, ensure_ascii=False)[:2000] or "（未提供计划）"
            artifact = str(context.get("last_output", {}).get("text", ""))[:8000]
            return await agent.run(criteria=criteria, artifact_text=artifact, task_title=task.title)
        return await agent.run(
            task_title=task.title, plan_summary=json.dumps(plan, ensure_ascii=False)[:2000],
            criteria=json.dumps(plan, ensure_ascii=False)[:800],
            task_id=task.id, run_id=context.get("round", 0),
        )

    def _make_agent(self, role: str) -> BaseAgent:
        if role == "supervisor":
            return SupervisorAgent(role=role, llm=self.llm)
        if role == "reviewer":
            return ReviewerAgent(role=role, llm=self.llm)
        return DomainAgent(role=role, tools=self.registry, llm=self.llm)

    # ---- 帮助方法 ----
    def _result_to_output(self, spec: WorkflowNodeSpec, result: AgentResult) -> dict:
        return {"role": spec.role, "text": result.text, "decision": result.decision,
                "artifact_paths": result.artifact_paths}

    def _node_input(self, spec: WorkflowNodeSpec, context: dict) -> dict:
        return {"title": context.get("task").title, "plan": context.get("plan")}

    async def _advance(self, session, task_id, node_id, output):
        from app.workflow import engine

        return await engine.advance(session, task_id, node_id, output)

    async def _fail_node(self, session, task_id, node: TaskNode, role: str, reason: str | None):
        from app.workflow import engine  # noqa: F401 保持引用

        if not await repos.set_node_status(session, node.id, RUNNING, FAILED):
            logger.warning("节点 %s 非 running，无法置 failed（并发/已在途）", node.id)
        await repos.set_node_error(session, node.id, reason or "未知错误")
        await repos.set_task_status(session, task_id, FAILED)
        await session.commit()
        await self._audit(session, task_id, role, "node_failed", {"node": node.node_name,
                                                                 "reason": reason})

    async def _audit(self, session, task_id, operator, action, detail):
        try:
            await repos.write_audit(session, task_id=task_id, operator=operator,
                                    action=action, detail=detail)
        except Exception as exc:  # noqa: BLE001 审计失败不影响主流转，仅告警
            logger.warning("审计写入失败 task=%s action=%s：%s", task_id, action, exc)

    async def _require_task(self, session, task_id: str) -> Task:
        task = await repos.get_task(session, task_id)
        if task is None:
            raise WorkflowStateError(f"任务不存在：{task_id}")
        return task

    async def _ordered(self, session, task: Task, tpl) -> list[tuple[WorkflowNodeSpec, TaskNode]]:
        nodes = {n.node_name: n for n in await repos.list_nodes(session, task.id)}
        return [(spec, nodes[spec.name]) for spec in tpl.nodes]


# 进程级默认 runner（registry 由 build_agent_runner 注入）
_runner: AgentRunner | None = None


def get_agent_runner() -> AgentRunner:
    global _runner
    if _runner is None:
        _runner = AgentRunner()
    return _runner


def build_agent_runner(registry: Any, *, replace_global: bool = True) -> AgentRunner:
    """用注入的工具注册表构建 runner；默认替换全局单例供 WS 使用。"""
    runner = get_agent_runner()
    runner.registry = registry
    return runner