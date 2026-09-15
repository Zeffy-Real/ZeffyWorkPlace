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

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
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
    # P4：trace_id 全链路贯穿（HTTP/WS/worker → 审计），可跨模块追溯
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

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


class ArtifactVersion(Base):
    """P5-1 产物版本：同一 rel_path 每次写入归档一条版本记录。

    - ``status``：pending → available（失败置 failed）；仅 available 参与幂等比对/版本号/列表。
    - 唯一约束 ``(task_id, rel_path, version)``：版本号由 DB 原子递增生成，并发不重复。
    """

    __tablename__ = "artifact_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    rel_path: Mapped[str] = mapped_column(String(512))
    version: Mapped[int] = mapped_column(default=1)
    key: Mapped[str] = mapped_column(String(512))  # 归档 key（_v 空间）
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/available/failed
    size: Mapped[int] = mapped_column(default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    mime: Mapped[str] = mapped_column(String(128), default="application/octet-stream")
    producer_role: Mapped[str] = mapped_column(String(32), default="")
    run_id: Mapped[str] = mapped_column(String(64), default="")
    mode: Mapped[str] = mapped_column(String(16), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )

    __table_args__ = (UniqueConstraint("task_id", "rel_path", "version",
                                       name="uq_artifact_versions_task_rel_ver"),)


class ArtifactVersionSeq(Base):
    """P5-1 版本序列：按 (task_id, rel_path) 维护原子递增版本号。

    版本号生成 = ``UPDATE ... SET next_version=next_version+1 RETURNING next_version``，
    行级原子递增，并发写同一路径版本号唯一不重复（审查🔴2）。
    """

    __tablename__ = "artifact_version_seq"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    rel_path: Mapped[str] = mapped_column(String(512))
    next_version: Mapped[int] = mapped_column(default=0)

    __table_args__ = (UniqueConstraint("task_id", "rel_path",
                                       name="uq_artifact_version_seq_task_rel"),)


class Artifact(Base):
    """P6 产物权威元表：每次写入落一条「当前可用产物快照」。

    - 与 ``artifact_versions`` 关系：versions 是版本链（历史归档），本表是当前可用产物。
      版本化开启时同频（version 关联）；关闭时独立记录、version=0（🔴1 元表权威性）。
    - 查询/配额/计量/分层全部以本表为准，杜绝依赖 list() 列举对账。
    - ``tx_id``：所属事务批次（🔴4 事务原子提交）；批量 commit/rollback 据此更新状态。
    - FKs 均 nullable 语义：owner_id SET NULL；task 删除 CASCADE。
    """

    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    rel_path: Mapped[str] = mapped_column(String(512))
    key: Mapped[str] = mapped_column(String(512))
    version: Mapped[int] = mapped_column(default=0)  # 0=未启用版本化
    owner_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    size: Mapped[int] = mapped_column(default=0)
    backend: Mapped[str] = mapped_column(String(16), default="local")
    tier: Mapped[str] = mapped_column(String(16), default="hot")  # hot/cold（P6 分层）
    sha256: Mapped[str] = mapped_column(String(64), default="")
    content_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)  # P6-2 O4 去重内容引用(sha256, 无外键)
    tier_pinned: Mapped[bool] = mapped_column(default=False)  # P6-3 N1 置顶热：不被冷化
    mime: Mapped[str] = mapped_column(String(128), default="application/octet-stream")
    producer_role: Mapped[str] = mapped_column(String(32), default="")
    # available / archived / failed / pending(事务暂存) / deleted(软删回收站)
    status: Mapped[str] = mapped_column(String(16), default="available")
    tx_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=_utcnow
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    # P6-2 O1 智能分层：热度埋点（仅完整文件读取更新；access_count 仅统计参考不参与冷化判定）
    last_access: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    access_count: Mapped[int] = mapped_column(default=0)

    __table_args__ = (UniqueConstraint("task_id", "rel_path", "version",
                                       name="uq_artifacts_task_rel_ver"),)


class QuotaUsage(Base):
    """P6 用户配额用量：物化为行，写入/删除用原子 +/- 维护 used_bytes。

    设计取舍：不聚合 artifacts 求和（避免大表 COUNT/SUM 拉垮），
    原子 UPDATE 保证「校验+记账」一致性（🔴3 乐观，非强一致，文档明示边界）。
    """

    __tablename__ = "quota_usage"

    owner_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    used_bytes: Mapped[int] = mapped_column(default=0)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=_utcnow
    )


class QuotaHistory(Base):
    """P6-2 O2 配额历史采样：按 owner + 时间点记录 used_bytes。

    支撑线性趋势预测（利用率 ETA）、用量曲线报表、成本核算（hot/cold）。
    由 ``_gc_loop`` 配额采样节按 ``QUOTA_HISTORY_INTERVAL`` 写入，不回溯历史；
    ``recorded_at`` 为采样时刻（index on owner+time 供趋势查询/清理）。
    """

    __tablename__ = "quota_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[str] = mapped_column(String(36), index=False)
    used_bytes: Mapped[int] = mapped_column(default=0)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=False
    )

    __table_args__ = (Index("ix_quota_history_owner_recorded", "owner_id", "recorded_at"),)


class ArtifactContent(Base):
    """P6-2 O4 内容寻址去重表：物理文件引用计数。

    - 物理 key 由 ``sha256`` 确定性推导（``artifacts/_dedup/{sha[:2]}/{sha}``），本表**不存 key**；
    - ``refs`` = 引用该物理的可用 Artifact 行数；refs==0 才可物理删除；
    - ``tier`` = 内容粒度冷热（共享文件唯一 tier，冷化一次同步所有 available 引用）；
    - ``content_ref`` 仅逻辑关联（Artifact.content_ref），**不加外键**避免级联风险。
    """

    __tablename__ = "artifact_content"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    size: Mapped[int] = mapped_column(default=0)
    refs: Mapped[int] = mapped_column(default=0)
    tier: Mapped[str] = mapped_column(String(16), default="hot")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )


class ArtifactTx(Base):
    """P6 事务批次：一次多文件产物原子提交（🔴4）。

    status: pending → committed / failed / rolled_back。
    commit 全部成功 → 逐 key 置 available + 建元表 + bump 配额；
    中途失败 → 回滚已 copy key + 冲正 + 置 rolled_back，不残留半成。
    """

    __tablename__ = "artifact_tx"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    owner_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(16), default="pending")
    reserved_bytes: Mapped[int] = mapped_column(default=0)  # 🔴2 事务预扣配额（open 时占位）
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    committed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
