"""极简仓储层：统一异常包装，禁止直接抛 SQLAlchemy 底层异常到 API 层。

P1 起所有业务数据访问一律走 repo，杜绝裸 SQL / 裸 ORM 查询散落各处。
"""

from __future__ import annotations

from typing import cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, Message, Task, TaskNode
from app.llm_errors import LLMError  # noqa: F401  (占位，说明异常分层思想统一)


class RepositoryError(Exception):
    """仓储层统一业务异常（包装底层 DB 错误）。"""


async def create_task(
    session: AsyncSession, *, title: str, description: str = "", workflow_id: str = "generic"
) -> Task:
    try:
        task = Task(title=title, description=description, workflow_id=workflow_id)
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return task
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"create_task 失败：{exc}") from exc


async def get_task(session: AsyncSession, task_id: str) -> Task | None:
    try:
        result = await session.get(Task, task_id)
        return result
    except SQLAlchemyError as exc:
        raise RepositoryError(f"get_task 失败：{exc}") from exc


async def list_tasks(
    session: AsyncSession, *, status: str | None = None, limit: int = 50
) -> list[Task]:
    try:
        stmt = select(Task)
        if status:
            stmt = stmt.where(Task.status == status)
        stmt = stmt.order_by(Task.created_at.desc()).limit(limit)
        return list((await session.execute(stmt)).scalars().all())
    except SQLAlchemyError as exc:
        raise RepositoryError(f"list_tasks 失败：{exc}") from exc


# ---------------------------------------------------------------------------
# P1-2 工作流：TaskNode + Task 状态（乐观锁）
# ---------------------------------------------------------------------------


async def create_node(session: AsyncSession, *, task_id: str, node_name: str, status: str = "pending") -> TaskNode:
    try:
        node = TaskNode(task_id=task_id, node_name=node_name, status=status)
        session.add(node)
        await session.commit()
        await session.refresh(node)
        return node
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"create_node 失败：{exc}") from exc


async def list_nodes(session: AsyncSession, task_id: str) -> list[TaskNode]:
    try:
        stmt = select(TaskNode).where(TaskNode.task_id == task_id)
        return list((await session.execute(stmt)).scalars().all())
    except SQLAlchemyError as exc:
        raise RepositoryError(f"list_nodes 失败：{exc}") from exc


async def get_node(session: AsyncSession, node_id: str) -> TaskNode | None:
    try:
        return await session.get(TaskNode, node_id)
    except SQLAlchemyError as exc:
        raise RepositoryError(f"get_node 失败：{exc}") from exc


async def set_node_status(
    session: AsyncSession, node_id: str, from_status: str, to_status: str
) -> bool:
    """乐观锁状态迁移：仅当当前=from_status 才更新，返回是否命中。

    命中规则是引擎防并发竞态的关键（UPDATE ... WHERE status=预期旧态）。
    注意：本调用不 commit，由引擎控制事务边界。调用方 commit 前勿重入同 session。
    """
    try:
        stmt = (
            update(TaskNode)
            .where(TaskNode.id == node_id, TaskNode.status == from_status)
            .values(status=to_status)
        )
        result = await session.execute(stmt)
        return cast(CursorResult, result).rowcount == 1
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"set_node_status 失败：{exc}") from exc


async def set_node_output(session: AsyncSession, node_id: str, output: dict | None) -> None:
    try:
        stmt = update(TaskNode).where(TaskNode.id == node_id).values(output=output)
        await session.execute(stmt)
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"set_node_output 失败：{exc}") from exc


async def set_task_status(session: AsyncSession, task_id: str, status: str) -> None:
    try:
        stmt = update(Task).where(Task.id == task_id).values(status=status)
        await session.execute(stmt)
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"set_task_status 失败：{exc}") from exc


# ---------------------------------------------------------------------------
# P1-3 多 Agent：消息、审计、节点错误（Agent 交权所需）
# ---------------------------------------------------------------------------


async def set_node_error(session: AsyncSession, node_id: str, error: str) -> None:
    """写节点错误信息（配合 running→failed 迁移后落库）。"""
    try:
        stmt = update(TaskNode).where(TaskNode.id == node_id).values(error=error)
        await session.execute(stmt)
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"set_node_error 失败：{exc}") from exc


async def write_message(
    session: AsyncSession,
    *,
    task_id: str,
    sender_role: str,
    content: str,
    msg_type: str = "text",
) -> Message:
    """写一条消息流记录（Agent/评审/系统等）。"""
    try:
        m = Message(task_id=task_id, sender_role=sender_role, content=content, msg_type=msg_type)
        session.add(m)
        await session.commit()
        await session.refresh(m)
        return m
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"write_message 失败：{exc}") from exc


async def write_audit(
    session: AsyncSession,
    *,
    task_id: str,
    operator: str,
    action: str,
    detail: dict | None = None,
) -> AuditLog:
    """全链路审计：Agent 入参/输出/usage/异常/决策、工具调用均须落这里。"""
    try:
        entry = AuditLog(task_id=task_id, operator=operator, action=action, detail=detail)
        session.add(entry)
        await session.commit()
        await session.refresh(entry)
        return entry
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"write_audit 失败：{exc}") from exc


async def list_messages(
    session: AsyncSession, task_id: str, *, limit: int | None = None
) -> list[Message]:
    """按时间升序读取任务的消息流（用于构造上下文视图；不删除任何原始记录）。"""
    try:
        stmt = (
            select(Message)
            .where(Message.task_id == task_id)
            .order_by(Message.created_at.asc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return list((await session.execute(stmt)).scalars().all())
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"list_messages 失败：{exc}") from exc
