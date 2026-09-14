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

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, UniqueConstraint
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

    # P3-3：归属用户；NULL = 无主（开启鉴权时迁移到 system 账号；AUTH off 时不加约束）
    owner_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # P4-4b：任务优先级 0低/1中/2高（默认中）；DAG 全节点继承
    priority: Mapped[int] = mapped_column(default=1, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=_utcnow
    )

    # 关系
    messages: Mapped[list[Message]] = relationship(back_populates="task")
    nodes: Mapped[list[TaskNode]] = relationship(back_populates="task")
    owner: Mapped[User | None] = relationship()
    shares: Mapped[list[TaskShare]] = relationship(back_populates="task")


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
    # 状态：pending → queued → running → done / failed / blocked
    status: Mapped[str] = mapped_column(String(32), default="pending")

    input: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    output: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ---- P2 持久队列字段（审查修订）----
    depends_on: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)  # DAG 依赖 node_name 列表
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # HITL 人工决策等持久化数据
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 认领执行的 worker
    attempts: Mapped[int] = mapped_column(default=0)  # 执行尝试次数（重试观测/死信判断）
    queued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # 入队时间（用于巡检兜底判断）
    lease: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {worker_id, expire_at} 死任务检测

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


class User(Base):
    """P3-3 用户：email/username 唯一；is_system 标记内置账号（承接无主任务）。"""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_system: Mapped[bool] = mapped_column(default=False)
    # P4-3：角色 admin/user；system 账号=admin
    role: Mapped[str] = mapped_column(String(16), default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    tokens: Mapped[list[UserToken]] = relationship(back_populates="user")


class TaskShare(Base):
    """P4-3 任务协作分享：viewer 只读 / editor 读写；仅 owner 与 admin 可管理。

    二次分享禁止：editor/viewer 无管理分享权（判定在 permissions）。
    """

    __tablename__ = "task_shares"
    __table_args__ = (UniqueConstraint("task_id", "user_id", name="uq_task_share"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16), default="viewer")  # viewer | editor
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    task: Mapped[Task | None] = relationship(back_populates="shares")
    user: Mapped[User | None] = relationship()


class UserToken(Base):
    """P3-3 令牌：DB 只存 sha256(token) 哈希（不可逆）；多 token 并存、可单独撤销。"""

    __tablename__ = "user_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), index=True)  # sha256 hex
    token_prefix: Mapped[str] = mapped_column(String(32))  # zwt_xxxx（日志/排错识别）
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    user: Mapped[User | None] = relationship(back_populates="tokens")
