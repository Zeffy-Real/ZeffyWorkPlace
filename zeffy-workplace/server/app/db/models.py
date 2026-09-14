"""ORM 模型：P0 实现最小字段，P1/P2 扩展字段用注释标注（禁止 P0 写未使用业务字段）。

建表策略警告（P0）：
- 本表结构在用 `metadata.create_all` 建的同时，须注意 create_all 只建不存在的表，
  不会同步字段变更。P0 阶段仅供本地开发。
- P1 起必须切换 alembic 迁移并废弃 create_all；P0 改模型后需清 volume：
  `docker compose -f docker-compose.base.yml down -v`

索引规划（P0 不建，P1 建）：
- Message.task_id, Message.created_at
- TaskNode.task_id
- AuditLog.created_at
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    workflow_id: Mapped[str] = mapped_column(String(64), default="generic")

    # 状态：待分配→执行中→评审中→已完成→已验收 / 失败 / 阻塞
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)

    # P1-待实现：config 存任务级运行时覆盖（如模型选择、自定义参数）
    config: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=_utcnow
    )

    # 关系
    messages: Mapped[list[Message]] = relationship(back_populates="task")
    nodes: Mapped[list[TaskNode]] = relationship(back_populates="task")


class Message(Base):
    """群聊消息流。"""

    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # 角色：user / supervisor / agent / reviewer / system
    sender_role: Mapped[str] = mapped_column(String(32))
    content: Mapped[str] = mapped_column(Text, default="")
    # 类型：text / task_card / artifact_card / approval_card / ask_card
    msg_type: Mapped[str] = mapped_column(String(32), default="text")
    status: Mapped[str] = mapped_column(String(32), default="delivered")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    task: Mapped[Task | None] = relationship(back_populates="messages")


class TaskNode(Base):
    """工作流节点执行记录。P1 核心对象；P0 仅 ORM 壳，不实现流转逻辑。"""

    __tablename__ = "task_nodes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    node_name: Mapped[str] = mapped_column(String(64))
    # 状态：pending → running → done / failed / blocked
    status: Mapped[str] = mapped_column(String(32), default="pending")

    input: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    output: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # P1-待实现：依赖关系（DAG 并行）、重试次数、agent 角色
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=_utcnow
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    task: Mapped[Task] = relationship(back_populates="nodes")


class AuditLog(Base):
    """全链路审计：工具调用 / Agent 动作 / LLM 调用 / 人类决策均须写入。P1 启用。"""

    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    operator: Mapped[str] = mapped_column(String(32))  # agent 角色 / human / system
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )


class EvalRun(Base):
    """评测运行：五维指标 + judge 结果。P3 启用。"""

    __tablename__ = "eval_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # 五维指标
    judge_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # judge 打分

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
