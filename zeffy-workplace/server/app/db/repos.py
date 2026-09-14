"""极简仓储层：统一异常包装，禁止直接抛 SQLAlchemy 底层异常到 API 层。

P1 起所有业务数据访问一律走 repo，杜绝裸 SQL / 裸 ORM 查询散落各处。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    ArtifactVersion,
    ArtifactVersionSeq,
    AuditLog,
    Message,
    Task,
    TaskNode,
    TaskShare,
    User,
    UserToken,
)
from app.llm_errors import LLMError  # noqa: F401  (占位，说明异常分层思想统一)


class RepositoryError(Exception):
    """仓储层统一业务异常（包装底层 DB 错误）。"""


async def create_task(
    session: AsyncSession, *, title: str, description: str = "", workflow_id: str = "generic",
    owner_id: str | None = None, priority: int = 1,
) -> Task:
    try:
        task = Task(title=title, description=description, workflow_id=workflow_id,
                    owner_id=owner_id, priority=priority)
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


async def create_node(
    session: AsyncSession, *, task_id: str, node_name: str, status: str = "pending",
    depends_on: list[str] | None = None, payload: dict | None = None,
) -> TaskNode:
    try:
        node = TaskNode(task_id=task_id, node_name=node_name, status=status,
                        depends_on=depends_on, payload=payload)
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
    trace_id: str | None = None,
) -> AuditLog:
    """全链路审计：Agent 入参/输出/usage/异常/决策、工具调用均须落这里。

    P4：``trace_id`` 默认取当前链路 trace（HTTP/WS/worker 经 tracing.contextvar 注入），
    保证跨模块可追溯；显式传入则优先。
    """
    from app.tracing import get_trace_id

    trace_id = trace_id or get_trace_id()
    try:
        entry = AuditLog(task_id=task_id, operator=operator, action=action,
                         detail=detail, trace_id=trace_id)
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


# ---------------------------------------------------------------------------
# P2 持久队列：入队 / 原子认领(lease+attempts) / 释放 / 巡检扫描
# ---------------------------------------------------------------------------


async def set_node_queued(session: AsyncSession, node_id: str, *, worker_at: datetime | None = None) -> bool:
    """pending → queued（DB 优先入队的原子更新）；失败返回 False（并发冲突）。"""
    worker_at = worker_at or datetime.now(UTC)
    try:
        stmt = (
            update(TaskNode)
            .where(TaskNode.id == node_id, TaskNode.status == "pending")
            .values(status="queued", queued_at=worker_at)
        )
        result = await session.execute(stmt)
        return cast(CursorResult, result).rowcount == 1
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"set_node_queued 失败：{exc}") from exc


async def claim_node(
    session: AsyncSession, node_id: str, *, worker_id: str, lease_expire_at: datetime
) -> bool:
    """原子认领：queued → running（写 worker_id + attempts++ + lease）。

    🔴 幂等两层之一：`UPDATE ... WHERE status='queued'` 原子乐观锁，同一节点同时只有一个 worker 能 claim 成功。
    不 commit，由调用方控制事务边界。
    """
    try:
        stmt = (
            update(TaskNode)
            .where(TaskNode.id == node_id, TaskNode.status == "queued")
            .values(
                status="running", worker_id=worker_id,
                attempts=TaskNode.attempts + 1,
                lease={"worker_id": worker_id, "expire_at": lease_expire_at.isoformat()},
            )
        )
        result = await session.execute(stmt)
        return cast(CursorResult, result).rowcount == 1
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"claim_node 失败：{exc}") from exc


async def release_node(session: AsyncSession, node_id: str, *, to_status: str,
                       error: str | None = None) -> bool:
    """running → to_status（done/failed/blocked），清 lease。不 commit。"""
    try:
        stmt = (
            update(TaskNode)
            .where(TaskNode.id == node_id, TaskNode.status == "running")
            .values(status=to_status, lease=None, error=error)
        )
        result = await session.execute(stmt)
        return cast(CursorResult, result).rowcount == 1
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"release_node 失败：{exc}") from exc


async def scan_nodes_by_status(
    session: AsyncSession, *, statuses: tuple[str, ...]
) -> list[TaskNode]:
    """按状态集扫描节点（断点恢复白名单 / lease 巡检用）。"""
    try:
        stmt = select(TaskNode).where(TaskNode.status.in_(statuses))
        return list((await session.execute(stmt)).scalars().all())
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"scan_nodes_by_status 失败：{exc}") from exc


async def set_node_payload(session: AsyncSession, node_id: str, payload: dict) -> None:
    """写节点 payload（如 HITL 人工决策持久化）。"""
    try:
        stmt = update(TaskNode).where(TaskNode.id == node_id).values(payload=payload)
        await session.execute(stmt)
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"set_node_payload 失败：{exc}") from exc


async def renew_node_lease(
    session: AsyncSession, node_id: str, *, worker_id: str, expire_at: datetime
) -> bool:
    """续约 running 节点的 lease（🔴 死任务检测前提：每个节点 running 期间都有 lease）。

    仅当节点仍是 running 时命中（防对已流转节点写脏 lease）。不 commit。
    """
    try:
        stmt = (
            update(TaskNode)
            .where(TaskNode.id == node_id, TaskNode.status == "running")
            .values(lease={"worker_id": worker_id, "expire_at": expire_at.isoformat()},
                    worker_id=worker_id)
        )
        result = await session.execute(stmt)
        return cast(CursorResult, result).rowcount == 1
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"renew_node_lease 失败：{exc}") from exc


async def dead_letter_node(
    session: AsyncSession, node_id: str, *, reason: str
) -> bool:
    """死信：queued/running → failed（attempts 超限终止重试）。不 commit。"""
    try:
        stmt = (
            update(TaskNode)
            .where(TaskNode.id == node_id, TaskNode.status.in_(("queued", "running")))
            .values(status="failed", lease=None, error=reason)
        )
        result = await session.execute(stmt)
        return cast(CursorResult, result).rowcount == 1
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"dead_letter_node 失败：{exc}") from exc


# ---------------------------------------------------------------------------
# P3-2 监控：一次性采集快照（后台定时缓存，/metrics 读缓存避免高频查库）
# ---------------------------------------------------------------------------


async def count_by_status(
    session: AsyncSession, *, model, status_col, since: datetime | None = None
) -> dict[str, int]:
    """按 status 分组计数某模型（Task / TaskNode）。返回 {status: count}。"""
    stmt = select(status_col, func.count()).group_by(status_col)
    if since is not None:
        stmt = stmt.where(model.created_at >= since)
    try:
        rows = (await session.execute(stmt)).all()
        return {r[0]: int(r[1]) for r in rows}
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"count_by_status 失败：{exc}") from exc


async def node_failure_rate(session: AsyncSession, since: datetime) -> float:
    """统计窗口内已完成节点失败率：failed / (done + failed)。窗口内无样本返回 0。"""
    try:
        done = await session.scalar(
            select(func.count()).where(TaskNode.status == "done",
                                       TaskNode.created_at >= since)
        )
        failed = await session.scalar(
            select(func.count()).where(TaskNode.status == "failed",
                                       TaskNode.created_at >= since)
        )
        done, failed = int(done or 0), int(failed or 0)
        total = done + failed
        return failed / total if total else 0.0
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"node_failure_rate 失败：{exc}") from exc


async def count_nodes_created_since(session: AsyncSession, since: datetime) -> int:
    """统计窗口内创建节点数（吞吐代理）。"""
    stmt = select(func.count()).where(TaskNode.created_at >= since)
    try:
        return int((await session.scalar(stmt)) or 0)
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"count_nodes_created_since 失败：{exc}") from exc


async def list_old_tasks(
    session: AsyncSession, *, status: str, updated_before: datetime, limit: int = 200,
) -> list[Task]:
    """P5 生命周期清理：列出指定状态且 updated_at 早于阈值的任务（产物回收）。"""
    stmt = (
        select(Task)
        .where(Task.status == status, Task.updated_at < updated_before)
        .order_by(Task.updated_at.asc())
        .limit(limit)
    )
    try:
        return list((await session.execute(stmt)).scalars().all())
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"list_old_tasks 失败：{exc}") from exc


async def list_audit(
    session: AsyncSession, *, action: str | None = None, operator: str | None = None,
    limit: int = 50,
) -> list[AuditLog]:
    """按 action/operator 筛审计（监控告警断言用）。"""
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    if operator is not None:
        stmt = stmt.where(AuditLog.operator == operator)
    try:
        return list((await session.execute(stmt)).scalars().all())
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"list_audit 失败：{exc}") from exc


async def queued_depth_age(
    session: AsyncSession, *, task_ids: list[str] | None = None
) -> tuple[int, float]:
    """返回 (queued 节点数, 队首滞留平均秒数)。queued 且 queued_at 非空时算年龄。"""
    try:
        stmt = select(TaskNode).where(TaskNode.status == "queued")
        if task_ids:
            stmt = stmt.where(TaskNode.task_id.in_(task_ids))
        nodes = list((await session.execute(stmt)).scalars().all())
        now = datetime.now(UTC)
        ages: list[float] = []
        for n in nodes:
            qa = n.queued_at
            if qa is None:
                continue
            if qa.tzinfo is None:
                qa = qa.replace(tzinfo=UTC)
            ages.append(max(0.0, (now - qa).total_seconds()))
        avg = sum(ages) / len(ages) if ages else 0.0
        return len(nodes), avg
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"queued_depth_age 失败：{exc}") from exc


# ---------------------------------------------------------------------------
# P3-3 用户 / token（Auth）
# ---------------------------------------------------------------------------


async def create_user(
    session: AsyncSession, *, email: str, username: str, password_hash: str,
    is_system: bool = False,
) -> User:
    try:
        u = User(email=email, username=username, password_hash=password_hash,
                 is_system=is_system)
        session.add(u)
        await session.commit()
        await session.refresh(u)
        return u
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"create_user 失败（可能邮箱/用户名已存在）：{exc}") from exc


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    try:
        stmt = select(User).where(User.email == email)
        return (await session.execute(stmt)).scalar_one_or_none()
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"get_user_by_email 失败：{exc}") from exc


async def get_user_by_username(session: AsyncSession, username: str) -> User | None:
    try:
        stmt = select(User).where(User.username == username)
        return (await session.execute(stmt)).scalar_one_or_none()
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"get_user_by_username 失败：{exc}") from exc


async def get_user_by_id(session: AsyncSession, user_id: str) -> User | None:
    try:
        return await session.get(User, user_id)
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"get_user_by_id 失败：{exc}") from exc


async def create_user_token(
    session: AsyncSession, *, user_id: str, token_hash: str, token_prefix: str,
    expires_at: datetime,
) -> UserToken:
    try:
        t = UserToken(user_id=user_id, token_hash=token_hash,
                      token_prefix=token_prefix, expires_at=expires_at)
        session.add(t)
        await session.commit()
        await session.refresh(t)
        return t
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"create_user_token 失败：{exc}") from exc


async def get_user_by_token(session: AsyncSession, token_hash: str) -> User | None:
    """按 token 哈希找用户；校验到期。"""
    try:
        stmt = (
            select(User)
            .join(UserToken, UserToken.user_id == User.id)
            .where(UserToken.token_hash == token_hash)
        )
        user = (await session.execute(stmt)).scalar_one_or_none()
        if user is None:
            return None
        tt = (
            select(UserToken)
            .where(UserToken.token_hash == token_hash)
        )
        row = (await session.execute(tt)).scalar_one_or_none()
        from app.auth.tokens import is_expired
        if row is None or is_expired(row.expires_at):
            return None
        return user
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"get_user_by_token 失败：{exc}") from exc


async def revoke_token(session: AsyncSession, *, user_id: str, token_hash: str) -> bool:
    """撤销单条 token（登出）。"""
    try:
        from sqlalchemy import delete
        stmt = (
            delete(UserToken)
            .where(UserToken.user_id == user_id, UserToken.token_hash == token_hash)
        )
        result = await session.execute(stmt)
        await session.commit()
        return cast(CursorResult, result).rowcount == 1
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"revoke_token 失败：{exc}") from exc


async def revoke_all_user_tokens(session: AsyncSession, *, user_id: str) -> int:
    """改密/强制下线：撤销用户全部历史 token。"""
    try:
        result = await session.execute(select(UserToken.id).where(UserToken.user_id == user_id))
        ids = [r[0] for r in result.all()]
        if ids:
            from sqlalchemy import delete
            await session.execute(delete(UserToken).where(UserToken.user_id == user_id))
            await session.commit()
        return len(ids)
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"revoke_all_user_tokens 失败：{exc}") from exc


async def set_task_owner(session: AsyncSession, task_id: str, owner_id: str) -> None:
    try:
        stmt = update(Task).where(Task.id == task_id).values(owner_id=owner_id)
        await session.execute(stmt)
        await session.commit()
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"set_task_owner 失败：{exc}") from exc


async def list_tasks_owned(
    session: AsyncSession, *, owner_id: str, status: str | None = None, limit: int = 50
) -> list[Task]:
    """开启鉴权后按 owner 过滤任务列表。"""
    try:
        stmt = select(Task).where(Task.owner_id == owner_id)
        if status:
            stmt = stmt.where(Task.status == status)
        stmt = stmt.order_by(Task.created_at.desc()).limit(limit)
        return list((await session.execute(stmt)).scalars().all())
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"list_tasks_owned 失败：{exc}") from exc


async def list_tasks_accessible(
    session: AsyncSession, *, user_id: str, status: str | None = None, limit: int = 50
) -> list[Task]:
    """开启鉴权后按「owner ∪ 分享」过滤任务列表（P4-3 协作可见）。"""
    from sqlalchemy import or_

    try:
        shared_sub = select(TaskShare.task_id).where(TaskShare.user_id == user_id)
        stmt = select(Task).where(
            or_(Task.owner_id == user_id, Task.id.in_(shared_sub))
        )
        if status:
            stmt = stmt.where(Task.status == status)
        stmt = stmt.order_by(Task.created_at.desc()).limit(limit)
        return list((await session.execute(stmt)).scalars().all())
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"list_tasks_accessible 失败：{exc}") from exc


async def get_owner_or_none(session: AsyncSession, task_id: str) -> str | None:
    """返回任务 owner_id（供事件/WS 过滤）。"""
    try:
        stmt = select(Task.owner_id).where(Task.id == task_id)
        return (await session.execute(stmt)).scalar_one_or_none()
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"get_owner_or_none 失败：{exc}") from exc


# ---------------------------------------------------------------------------
# P4-3 分享（task_shares）
# ---------------------------------------------------------------------------


async def get_share(session: AsyncSession, task_id: str, user_id: str) -> TaskShare | None:
    try:
        stmt = select(TaskShare).where(TaskShare.task_id == task_id,
                                       TaskShare.user_id == user_id)
        return (await session.execute(stmt)).scalar_one_or_none()
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"get_share 失败：{exc}") from exc


async def upsert_share(session: AsyncSession, *, task_id: str, user_id: str,
                       role: str) -> TaskShare:
    """新增/覆盖分享（幂等）。调用方须先通过 can_manage_share。"""
    try:
        share = await get_share(session, task_id, user_id)
        if share is None:
            share = TaskShare(task_id=task_id, user_id=user_id, role=role)
            session.add(share)
        else:
            share.role = role
        await session.commit()
        await session.refresh(share)
        return share
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"upsert_share 失败：{exc}") from exc


async def remove_share(session: AsyncSession, *, task_id: str, user_id: str) -> bool:
    """删除分享。调用方须先通过 can_manage_share。"""
    try:
        from sqlalchemy import delete

        stmt = delete(TaskShare).where(TaskShare.task_id == task_id,
                                       TaskShare.user_id == user_id)
        result = await session.execute(stmt)
        await session.commit()
        return cast(CursorResult, result).rowcount == 1
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"remove_share 失败：{exc}") from exc


async def list_shares(session: AsyncSession, task_id: str) -> list[TaskShare]:
    try:
        stmt = select(TaskShare).where(TaskShare.task_id == task_id)
        return list((await session.execute(stmt)).scalars().all())
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"list_shares 失败：{exc}") from exc


# ---------------------------------------------------------------------------
# P4-4 成本聚合：近窗口 agent_run 审计 → (task, model) token 汇总
# ---------------------------------------------------------------------------


async def usage_rows(
    session: AsyncSession, *, since: datetime, user_id: str | None = None
) -> list[dict]:
    """取窗口内 agent_run 审计，抽取 {task_id, model, prompt_tokens, completion_tokens}。

    依赖 (created_at, action) 联合索引（P4-4 迁移）避免全表扫描。
    ``user_id`` 给定（非 admin）→ 仅统计该用户归属任务。缺失 usage 记 0。
    """
    from sqlalchemy import text

    try:
        params: dict = {"since": since, "action": "agent_run"}
        where_parts = ["a.action = :action", "a.created_at >= :since"]
        if user_id is not None:
            where_parts.append(
                "a.task_id IN (SELECT t.id FROM tasks t WHERE t.owner_id = :uid)"
            )
            params["uid"] = user_id
        sql = text(
            "SELECT a.task_id, a.detail FROM audit_logs a WHERE " + " AND ".join(where_parts)
        )
        rows = list((await session.execute(sql, params)).all())
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"usage_rows 失败：{exc}") from exc

    out: list[dict] = []
    for r in rows:
        detail = _as_dict(r[1])
        result = detail.get("result") or {}
        usage = result.get("usage") or {}
        if not isinstance(result, dict):
            continue
        prompt = _to_int(usage.get("prompt_tokens")) if isinstance(usage, dict) else 0
        completion = _to_int(usage.get("completion_tokens")) if isinstance(usage, dict) else 0
        out.append({
            "task_id": r[0] or "",  # 任务可能已删（task_id SET NULL）→ 空串聚合容
            "model": detail.get("model") or "",
            "prompt_tokens": prompt,
            "completion_tokens": completion,
        })
    return out


def _as_dict(v) -> dict:
    """JSON 列经原始 SQL 在 sqlite 回字符串、PG 回 dict → 统一为 dict，容错。"""
    import json as _json

    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            d = _json.loads(v)
            return d if isinstance(d, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _to_int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# P5-1 产物版本：DB 状态机（pending→available/failed）+ 原子版本号 + 级联删除
# 约束（审查🔴）：版本号经 (task_id, rel_path) 序列表原子递增，并发唯一不重复。
# ---------------------------------------------------------------------------

AVAILABLE = "available"
PENDING = "pending"
FAILED = "failed"


async def ensure_version_seq(session: AsyncSession, *, task_id: str, rel_path: str) -> None:
    """幂等确保序列行存在（next_version 从 0 起）。并发下唯一约束兜底，冲突由调用方重试。"""
    try:
        exists = await session.scalar(
            select(ArtifactVersionSeq.id).where(
                ArtifactVersionSeq.task_id == task_id,
                ArtifactVersionSeq.rel_path == rel_path,
            )
        )
        if exists is None:
            session.add(ArtifactVersionSeq(task_id=task_id, rel_path=rel_path, next_version=0))
            await session.commit()
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"ensure_version_seq 失败：{exc}") from exc


async def next_version(session: AsyncSession, *, task_id: str, rel_path: str) -> int:
    """行级原子递增版本号（UPDATE ... RETURNING）；首写返回 1（🔴2 并发唯一）。"""
    await ensure_version_seq(session, task_id=task_id, rel_path=rel_path)
    try:
        result = await session.execute(
            update(ArtifactVersionSeq)
            .where(ArtifactVersionSeq.task_id == task_id,
                   ArtifactVersionSeq.rel_path == rel_path)
            .values(next_version=ArtifactVersionSeq.next_version + 1)
            .returning(ArtifactVersionSeq.next_version)
        )
        v = result.scalar_one()
        await session.commit()
        return int(v)
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"next_version 失败：{exc}") from exc


async def create_version_record(
    session: AsyncSession, *, task_id: str, rel_path: str, version: int, key: str,
    producer_role: str = "", run_id: str = "", mode: str = "overwrite",
) -> ArtifactVersion:
    """插入 pending 版本记录（🔴1：先 DB pending，再存储归档，最后 available）。"""
    try:
        rec = ArtifactVersion(task_id=task_id, rel_path=rel_path, version=version,
                              key=key, status=PENDING, producer_role=producer_role,
                              run_id=run_id, mode=mode)
        session.add(rec)
        await session.commit()
        await session.refresh(rec)
        return rec
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"create_version_record 失败：{exc}") from exc


async def update_version_status(
    session: AsyncSession, *, record_id: str, status: str, size: int = 0,
    sha256: str = "", mime: str = "",
) -> None:
    """记录状态流转：pending→available（成功）或 →failed（失败）。"""
    try:
        await session.execute(
            update(ArtifactVersion)
            .where(ArtifactVersion.id == record_id)
            .values(status=status, size=size, sha256=sha256, mime=mime)
        )
        await session.commit()
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"update_version_status 失败：{exc}") from exc


async def latest_available_version(
    session: AsyncSession, *, task_id: str, rel_path: str,
) -> ArtifactVersion | None:
    """最新 available 版本记录（幂等比对/淘汰依据；忽略 pending/failed，🔴1）。"""
    try:
        return await session.scalar(
            select(ArtifactVersion)
            .where(ArtifactVersion.task_id == task_id,
                   ArtifactVersion.rel_path == rel_path,
                   ArtifactVersion.status == AVAILABLE)
            .order_by(ArtifactVersion.version.desc())
            .limit(1)
        )
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"latest_available_version 失败：{exc}") from exc


async def get_version(
    session: AsyncSession, *, task_id: str, rel_path: str, version: int,
) -> ArtifactVersion | None:
    try:
        return await session.scalar(
            select(ArtifactVersion).where(
                ArtifactVersion.task_id == task_id,
                ArtifactVersion.rel_path == rel_path,
                ArtifactVersion.version == version,
                ArtifactVersion.status == AVAILABLE,
            )
        )
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"get_version 失败：{exc}") from exc


async def list_versions(
    session: AsyncSession, *, task_id: str, rel_path: str,
    page: int = 1, page_size: int = 50,
) -> tuple[list[ArtifactVersion], int, int]:
    """版本列表（仅 available，按 version 倒序）+ 总数 + 总字节（⭐2 分页）。"""
    try:
        base = (ArtifactVersion.task_id == task_id,
                ArtifactVersion.rel_path == rel_path,
                ArtifactVersion.status == AVAILABLE)
        total = await session.scalar(
            select(func.count()).where(*base)
        )
        total_bytes = await session.scalar(
            select(func.coalesce(func.sum(ArtifactVersion.size), 0)).where(*base)
        )
        stmt = (select(ArtifactVersion).where(*base)
                .order_by(ArtifactVersion.version.desc())
                .offset((max(1, page) - 1) * page_size)
                .limit(page_size))
        items = list((await session.execute(stmt)).scalars().all())
        return items, int(total or 0), int(total_bytes or 0)
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"list_versions 失败：{exc}") from exc


async def all_version_records(
    session: AsyncSession, *, task_id: str, rel_path: str,
) -> list[ArtifactVersion]:
    """某 rel_path 全部状态版本记录（级联删除/巡检用，含 pending/failed）。"""
    try:
        stmt = (select(ArtifactVersion)
                .where(ArtifactVersion.task_id == task_id,
                       ArtifactVersion.rel_path == rel_path)
                .order_by(ArtifactVersion.version.asc()))
        return list((await session.execute(stmt)).scalars().all())
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"all_version_records 失败：{exc}") from exc


async def delete_version_record(session: AsyncSession, *, record_id: str) -> bool:
    try:
        from sqlalchemy import delete

        result = await session.execute(
            delete(ArtifactVersion).where(ArtifactVersion.id == record_id)
        )
        await session.commit()
        return cast(CursorResult, result).rowcount == 1
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"delete_version_record 失败：{exc}") from exc


async def delete_version_records_by_key(session: AsyncSession, *, key: str) -> int:
    """级联：按归档 key 删除记录（删除主 key/淘汰时同步）。"""
    try:
        from sqlalchemy import delete

        result = await session.execute(
            delete(ArtifactVersion).where(ArtifactVersion.key == key)
        )
        await session.commit()
        return int(cast(CursorResult, result).rowcount or 0)
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"delete_version_records_by_key 失败：{exc}") from exc


async def delete_version_records_by_rel(session: AsyncSession, *, task_id: str, rel_path: str) -> int:
    """级联：删除某 rel_path 全部版本记录（删除主 key 时调用，🔴5）。"""
    try:
        from sqlalchemy import delete

        result = await session.execute(
            delete(ArtifactVersion).where(
                ArtifactVersion.task_id == task_id,
                ArtifactVersion.rel_path == rel_path,
            )
        )
        await session.commit()
        return int(cast(CursorResult, result).rowcount or 0)
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"delete_version_records_by_rel 失败：{exc}") from exc


async def delete_version_records_by_task(session: AsyncSession, *, task_id: str) -> int:
    """级联：任务删除/过期清理全部版本记录（🔴5）。"""
    try:
        from sqlalchemy import delete

        result = await session.execute(
            delete(ArtifactVersion).where(ArtifactVersion.task_id == task_id)
        )
        await session.commit()
        return int(cast(CursorResult, result).rowcount or 0)
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"delete_version_records_by_task 失败：{exc}") from exc


async def prune_versions(
    session: AsyncSession, *, task_id: str, rel_path: str, keep_max: int,
) -> list[str]:
    """超上限淘汰最旧 available 版本，返回被删记录的归档 key（供删除存储，🔴1）。"""
    try:
        from sqlalchemy import delete

        stmt = (
            select(ArtifactVersion)
            .where(ArtifactVersion.task_id == task_id,
                   ArtifactVersion.rel_path == rel_path,
                   ArtifactVersion.status == AVAILABLE)
            .order_by(ArtifactVersion.version.asc())
        )
        rows = list((await session.execute(stmt)).scalars().all())
        victims = rows[:-keep_max] if keep_max > 0 else rows
        keys = [v.key for v in victims if v.key]
        if victims:
            ids = [v.id for v in victims]
            await session.execute(
                delete(ArtifactVersion).where(ArtifactVersion.id.in_(ids))
            )
            await session.commit()
        return keys
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"prune_versions 失败：{exc}") from exc


async def stale_version_records(
    session: AsyncSession, *, status: str, older_than: datetime, limit: int = 200,
) -> list[ArtifactVersion]:
    """巡检：pending 超时 / failed 记录（🔴1 半状态清理）。"""
    try:
        stmt = (
            select(ArtifactVersion)
            .where(ArtifactVersion.status == status,
                   ArtifactVersion.created_at < older_than)
            .order_by(ArtifactVersion.created_at.asc())
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars().all())
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"stale_version_records 失败：{exc}") from exc


async def version_stats(session: AsyncSession, *, task_id: str | None = None) -> dict:
    """版本统计（⭐6 指标/对账）：总数、available/failed/pending、总字节。"""
    try:
        cond = [ArtifactVersion.task_id == task_id] if task_id else []
        total = await session.scalar(
            select(func.count()).select_from(ArtifactVersion).where(*cond)
        )
        by_status: dict[str, int] = {}
        for row in (await session.execute(
            select(ArtifactVersion.status, func.count())
            .where(*cond).group_by(ArtifactVersion.status)
        )).all():
            by_status[str(row[0])] = int(row[1])
        total_bytes = await session.scalar(
            select(func.coalesce(func.sum(ArtifactVersion.size), 0))
            .select_from(ArtifactVersion).where(*cond)
        )
        return {"total": int(total or 0), "by_status": by_status,
                "total_bytes": int(total_bytes or 0)}
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RepositoryError(f"version_stats 失败：{exc}") from exc
