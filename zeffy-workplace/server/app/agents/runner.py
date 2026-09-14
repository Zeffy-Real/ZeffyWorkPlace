"""AgentRunner（P1-3 → P1-5）：按工作流节点把执行权交给对应 Agent，回写结果并推进下一个节点。

P1-5 新增能力（端到端）：
- **上下文重建**：从 DB 节点 output 重建 plan/last_output（PG TaskNode 唯一真相源；
  内存态不驱动业务，重启/中断后可恢复继续）。
- **评审回流（A2）**：reviewer 返回 revise → 在 runner 内部重跑前置领域节点 → 再评审；
  修订次数 > 模板 ``max_revision`` 则终止（节点 failed + 停止流转）。
- **HITL 审批（A4）**：命中 human/HITL 节点 → 中断返回；人工 ``run_resume`` 批准则
  推进该节点（任务收尾），驳回则 ``engine.rewind`` 回退上一节点重做。
- **追问（A5）**：supervisor 返回 ``need_info`` → 中断挂起；人工补充后 ``run_resume``
  追加消息并 re-run（supervisor 节点保持 running，重入时带 followup）。
- **压缩注入（A6）**：任务轮次/消息超阈值时用 ``ContextCompressor`` 生成摘要视图，
  注入到 supervisor/domain/reviewer 的 prompt（纯视图层，DB Message 不动）。
- 异常分层 & 审计落库（P1-3，保留）：LLM 配置/可恢复异常耗尽 → 节点 failed + 停止。

⚠️ 本组件只读 ``engine.advance/rewind`` + repo 驱动 TaskNode 流转，
**TaskNode 状态权威仍在 WorkflowEngine（唯一真相源）。**
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.agents.base import AgentResult
from app.agents.domain import DomainAgent
from app.agents.reviewer import ReviewerAgent
from app.agents.supervisor import SupervisorAgent
from app.config import get_settings
from app.db import repos
from app.db.models import Task, TaskNode
from app.llm_errors import LLMError
from app.memory.compressor import CompressorConfig, ContextCompressor
from app.workflow.state_machine import FAILED, RUNNING, WorkflowStateError
from app.workflow.templates import NODE_HITL, NODE_HUMAN, WorkflowNodeSpec, get_template

logger = logging.getLogger(__name__)

# 每个任务最大推进步数（防死循环）。
RUNNER_MAX_STEPS = 64

# 事件推送回调：kind + payload（由 WS 层实现并发往前端）。
EmitCb = Callable[[str, dict[str, Any]], Awaitable[None]]


class AgentRunner:
    def __init__(self) -> None:
        self.registry: Any = None  # ToolRegistry
        self.llm: Any = None  # 注入 mock；None 则各 Agent 走全局 get_llm()
        # 压缩触发阈值（测试可调低）；None 用配置默认。
        self.compress_threshold: int | None = None

    # ------------------------------------------------------------------ 入口
    async def run(self, session, task_id: str, *, emit: EmitCb | None = None) -> dict:
        """执行一段工作流直到完成 / 遇到 human/hitl 中断 / 需要追问 / 出错。"""
        task = await self._require_task(session, task_id)
        tpl = get_template(task.workflow_id)

        if not await repos.list_nodes(session, task_id):
            from app.workflow import engine

            await engine.start(session, task)

        context = await self._load_context(session, task, tpl)
        # A6：整个 run 只压缩一次（进入时超阈值才触发），注入 context 供各 Agent 复用
        ctx_sum = await self._maybe_compress(session, task_id, tpl)
        context["ctx_sum"] = ctx_sum

        for _ in range(RUNNER_MAX_STEPS):
            ordered = await self._ordered(session, task, tpl)
            if all(n.status == "done" for _, n in ordered):
                return {"status": "done", "node": None}

            active = next((n for _, n in ordered if n.status == RUNNING), None)
            if active is None:
                return {"status": "idle", "node": None}  # 防御：理论不可达

            spec = next(s for s, _ in ordered if s.name == active.node_name)

            # human / HITL：审批中断（A4）
            if spec.type in {NODE_HUMAN, NODE_HITL}:
                await self._interrupt_approval(session, task_id, active, spec, emit)
                return {"status": "interrupt", "reason": "approval", "node": active.node_name}

            try:
                out = await self._run_dispatch(session, spec, active, context, task, tpl,
                                               emit)
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

            if out["action"] == "need_info":
                # A5：追问中断（supervisor 节点保持 running，等待人工补充后 re-run）
                await self._interrupt_ask(session, task_id, active, spec, out["result"], emit)
                return {"status": "interrupt", "reason": "ask",
                        "node": active.node_name, "question": out["result"].text}

            if out["action"] == "error":
                await self._fail_node(session, task_id, active, spec.role,
                                      out["result"].error)
                if emit:
                    await emit("task_node_update",
                               {"task_id": task_id, "node_id": active.id,
                                "node_name": active.node_name, "status": "failed",
                                "error": out["result"].error})
                return {"status": "error", "node": active.node_name,
                        "error": out["result"].error}

            if out["action"] == "max_revision":
                await self._fail_node(session, task_id, active, spec.role,
                                      "评审超最大修订次数终止")
                if emit:
                    await emit("task_node_update",
                               {"task_id": task_id, "node_id": active.id,
                                "node_name": active.node_name, "status": "failed",
                                "error": "评审超最大修订次数终止"})
                return {"status": "error", "node": active.node_name,
                        "error": "评审超最大修订次数终止"}

            result = out["result"]

            # 审计（入参/输出/usage/决策/异常 全量）
            await self._audit(session, task_id, spec.role, "agent_run",
                              {"node": active.node_name, "input": self._node_input(spec, context),
                               "result": result.to_log()})

            # 落消息 + 推进
            await repos.write_message(session, task_id=task_id, sender_role=spec.role,
                                      content=result.text, msg_type="text")
            if emit:
                await emit("agent_message", {"task_id": task_id, "node_id": active.id,
                                             "node_name": active.node_name, "role": spec.role,
                                             "text": result.text})

            output = self._result_to_output(spec, result)
            nxt = await self._advance(session, task_id, active.id, output)
            context[f"output.{active.node_name}"] = output
            context["last_output"] = output
            if spec.role == "supervisor" and result.decision:
                context["plan"] = result.decision
            if emit:
                await emit("task_node_update", {"task_id": task_id, "node_id": active.id,
                                                "node_name": active.node_name, "status": "done",
                                                "next": nxt.node_name if nxt else None})

            # 不在此处凭 nxt None 判断 done：可能仍有预激活的 HITL 节点待处理（驳回回退场景）。
            # 由循环顶部的 all-done 判断收敛；HITL 运行节点会在下一轮触发中断。
            # （nxt None 仅表示无新的 pending 被激活，不代表任务必然收尾。）

        raise RuntimeError(f"任务 {task_id} 超过最大推进步数 {RUNNER_MAX_STEPS}")

    async def run_resume(self, session, task_id: str, decision: dict, *,
                         emit: EmitCb | None = None) -> dict:
        """人工对中断任务给出决策后继续（A4 审批 / A5 追问补充）。"""
        task = await self._require_task(session, task_id)
        tpl = get_template(task.workflow_id)
        ordered = await self._ordered(session, task, tpl)
        kind = decision.get("kind")

        if kind == "approval":
            hitl = next((n for _, n in ordered if n.status == RUNNING), None)
            if hitl is None:
                raise WorkflowStateError("无进行中的审批节点")
            if decision.get("approved"):
                await repos.write_message(session, task_id=task_id, sender_role="user",
                                          content=f"审批通过：{decision.get('comment','')}",
                                          msg_type="approval_card")
                nxt = await self._advance(session, task_id, hitl.id,
                                          {"human_decision": decision})
                if emit:
                    await emit("review_event",
                               {"task_id": task_id, "node_id": hitl.id,
                                "node_name": hitl.node_name, "status": "approved"})
                return {"status": "done" if nxt is None else "continue", "node": hitl.node_name}
            # 驳回 → 回退上一节点重做
            prev_idx = self._index(hitl.node_name, ordered)
            prev_node = ordered[prev_idx - 1][1]
            from app.workflow import engine

            await engine.rewind(session, task_id, prev_node.id,
                                f"人工驳回：{decision.get('comment','')}")
            await repos.write_message(session, task_id=task_id, sender_role="user",
                                      content=f"驳回：{decision.get('comment','')}，请修正重做。",
                                      msg_type="approval_card")
            returned = await self.run(session, task_id, emit=emit)
            return returned

        if kind == "answer":
            text = (decision.get("text") or "").strip()
            if not text:
                raise WorkflowStateError("追问补充不能为空")
            await repos.write_message(session, task_id=task_id, sender_role="user",
                                      content=text, msg_type="text")
            return await self.run(session, task_id, emit=emit)

        raise WorkflowStateError(f"未知恢复决策 kind：{kind!r}")

    # ------------------------------------------------------------------ 内部
    async def _run_dispatch(self, session, spec, active, context, task, tpl, emit) -> dict:
        """派发 Agent 并处理评审回流；返回 {action, result}。

        action ∈ advance / error / need_info / max_revision。
        """
        plan = context.get("plan") or {}
        ctx_sum = context.get("ctx_sum", "")
        if spec.role == "supervisor":
            followup = await self._ask_history(session, task.id)
            result = await SupervisorAgent(role="supervisor", llm=self.llm).run(
                task_title=task.title, task_description=task.description,
                followup=followup, history_summary=ctx_sum or "")
            if result.status == "need_info":
                return {"action": "need_info", "result": result}
            if not result.ok:
                return {"action": "error", "result": result}
            return {"action": "advance", "result": result}

        if spec.role == "reviewer":
            return await self._run_review_loop(session, spec, active, context, task, tpl,
                                               ctx_sum, plan, emit)

        return await self._run_domain(spec, active, context, task, plan, ctx_sum,
                                      followup="")

    async def _run_domain(self, spec, active, context, task, plan, ctx_sum,
                          followup: str = "") -> dict:
        agent = DomainAgent(role=spec.role, tools=self.registry, llm=self.llm)
        result = await agent.run(
            task_title=task.title,
            plan_summary=json.dumps(plan, ensure_ascii=False)[:2000],
            criteria=json.dumps(plan, ensure_ascii=False)[:800],
            history_summary=ctx_sum or "",
            followup=followup,
            task_id=task.id, run_id=context.get("round", 0),
        )
        if not result.ok:
            return {"action": "error", "result": result}
        return {"action": "advance", "result": result}

    async def _run_review_loop(self, session, spec, active, context, task, tpl,
                               ctx_sum, plan, emit) -> dict:
        """评审 + 修订循环（A2）。内部重跑前置领域节点直到 pass 或超 max_revision。"""
        ordered = await self._ordered(session, task, tpl)
        prev_spec = ordered[self._index(spec.name, ordered) - 1][0]
        prev_node = ordered[self._index(spec.name, ordered) - 1][1]
        prev_agent = DomainAgent(role=prev_spec.role, tools=self.registry, llm=self.llm)
        reviewer = ReviewerAgent(role="reviewer", llm=self.llm)

        criteria = json.dumps(plan, ensure_ascii=False)[:2000] or "（未提供计划）"
        artifact = str(context.get("last_output", {}).get("text", ""))[:8000]

        revisions = context.get("revisions", 0)
        while True:
            result = await reviewer.run(criteria=criteria, artifact_text=artifact,
                                        task_title=task.title, history_summary=ctx_sum or "")
            if not result.ok:
                return {"action": "error", "result": result}

            verdict = (result.decision or {}).get("verdict", "revise")
            if emit:
                await emit("review_event", {"task_id": task.id, "node_id": active.id,
                                            "node_name": spec.name, "verdict": verdict,
                                            "comments": (result.decision or {}).get("comments", [])})

            if verdict == "pass":
                context["revisions"] = revisions
                # 把最终通过产物回写领域节点 output，保证 PG 一致
                await repos.set_node_output(session, prev_node.id,
                                            {"text": artifact, "role": prev_spec.role,
                                             "reviewed": {"verdict": verdict, "comments": (result.decision or {}).get("comments", [])}})
                return {"action": "advance", "result": result}

            revisions += 1
            if revisions > tpl.max_revision:
                return {"action": "max_revision", "result": result}

            # revise → 用评审意见重做前置领域节点
            comments = "\n".join((result.decision or {}).get("comments", []))
            redo = await prev_agent.run(
                task_title=task.title, plan_summary=json.dumps(plan, ensure_ascii=False)[:2000],
                criteria=criteria, artifact=artifact, history_summary=ctx_sum or "",
                followup=f"评审意见（第{revisions}轮修订）：\n{comments}",
                task_id=task.id, run_id=revisions,
            )
            if not redo.ok:
                return {"action": "error", "result": redo}
            artifact = redo.text
            context["revisions"] = revisions

    async def _interrupt_approval(self, session, task_id, node, spec, emit):
        await repos.write_message(
            session, task_id=task_id, sender_role="system",
            content=f"节点「{node.node_name}」需人工审批（type={spec.type}），已挂起。",
            msg_type="approval_card")
        await self._audit(session, task_id, "supervisor", "interrupt",
                          {"node": node.node_name, "type": spec.type, "reason": "approval"})
        if emit:
            await emit("review_event", {"task_id": task_id, "node_id": node.id,
                                        "node_name": node.node_name, "status": "awaiting_approval"})
        if emit:
            await emit("task_node_update",
                       {"task_id": task_id, "node_id": node.id,
                        "node_name": node.node_name, "status": "interrupt",
                        "reason": "approval"})

    async def _interrupt_ask(self, session, task_id, node, spec, result, emit):
        await repos.write_message(
            session, task_id=task_id, sender_role="system",
            content=f"需补充信息：{result.text}", msg_type="ask_card")
        await self._audit(session, task_id, spec.role, "interrupt",
                          {"node": node.node_name, "reason": "ask", "question": result.text})
        if emit:
            await emit("review_event", {"task_id": task_id, "node_id": node.id,
                                        "node_name": node.node_name, "status": "asking",
                                        "question": result.text})
        if emit:
            await emit("task_node_update",
                       {"task_id": task_id, "node_id": node.id,
                        "node_name": node.node_name, "status": "interrupt",
                        "reason": "ask", "question": result.text})

    async def _maybe_compress(self, session, task_id: str, tpl) -> str:
        """A6：超过阈值则用 ContextCompressor 生成摘要视图，返回摘要文本（供 prompt 注入）。

        纯视图层，不修改 DB。压缩失败降级为空（不阻断任务）。
        """
        msgs = await repos.list_messages(session, task_id)
        if not msgs:
            return ""
        rounds = len(msgs)
        threshold = self.compress_threshold if self.compress_threshold is not None else \
            get_settings().CONTEXT_MAX_ROUNDS
        if rounds <= threshold:
            return ""
        try:
            comp = ContextCompressor(llm=self.llm, config=CompressorConfig(max_rounds=threshold))
            view = await comp.compress_messages(msgs, decisions=[], rounds=rounds)
            if view.degraded:
                await self._audit(session, task_id, "system", "compress_degraded",
                                  {"reason": "压缩失败降级截断"})
            return view.summary or ""
        except Exception as exc:  # noqa: BLE001 压缩失败不使任务失败
            logger.warning("压缩注入失败（跳过）：%s", exc)
            return ""

    async def _ask_history(self, session, task_id: str) -> str:
        """把此前的人工补充（user 消息）拼成 followup，供 supervisor 重入时使用（A5）。"""
        msgs = await repos.list_messages(session, task_id)
        parts = []
        for m in msgs[-8:]:
            if getattr(m, "sender_role", "") == "user":
                parts.append(str(getattr(m, "content", "")))
        return "\n".join(parts)

    # ------------------------------------------------------------------ 他
    def _result_to_output(self, spec: WorkflowNodeSpec, result: AgentResult) -> dict:
        return {"role": spec.role, "text": result.text, "decision": result.decision,
                "artifact_paths": result.artifact_paths}

    def _node_input(self, spec: WorkflowNodeSpec, context: dict) -> dict:
        return {"title": context.get("task").title, "plan": context.get("plan")}

    async def _load_context(self, session, task: Task, tpl) -> dict:
        """从 DB 节点 output 重建 context（PG 唯一真相源；重启/中断后可续）。"""
        context: dict[str, Any] = {"task": task, "plan": None, "round": 0}
        nodes = {n.node_name: n for n in await repos.list_nodes(session, task.id)}
        # plan：supervisor 节点 output.decision
        if "需求分析" in nodes and nodes["需求分析"].output:
            context["plan"] = (nodes["需求分析"].output or {}).get("decision")
        elif "需求" in nodes and nodes["需求"].output:
            context["plan"] = (nodes["需求"].output or {}).get("decision")
        # last_output：最后一个 done 节点
        for spec, node in await self._ordered(session, task, tpl):
            if node.status == "done" and node.output:
                context["last_output"] = node.output
                context[f"output.{spec.name}"] = node.output
        return context

    async def _advance(self, session, task_id, node_id, output):
        from app.workflow import engine

        return await engine.advance(session, task_id, node_id, output)

    async def _fail_node(self, session, task_id, node: TaskNode, role: str, reason: str | None):
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

    @staticmethod
    def _index(name: str, ordered: list) -> int:
        for i, (s, _) in enumerate(ordered):
            if s.name == name:
                return i
        raise KeyError(name)


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
