"""HTTP API 请求/响应 Pydantic schema（不裸 dict）。P1 在此扩展字段。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=10000)
    workflow_id: str = Field(default="generic")


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    description: str
    workflow_id: str
    status: str
    created_at: datetime


class TaskListOut(BaseModel):
    items: list[TaskOut]
    total: int


# --- P1-2 调试接口（仅本地开发；P1-5 后须走网关开关禁用） -------------------

class NodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_id: str
    node_name: str
    status: str
    input: dict | None = None
    output: dict | None = None
    error: str | None = None
    created_at: datetime
    # P2：节点类型（auto/human/hitl），供前端按 blocked+type 重建审批/追问卡
    node_type: str = "auto"


class NodeListOut(BaseModel):
    items: list[NodeOut]
    total: int


class AdvanceRequest(BaseModel):
    node_id: str
    result: dict | None = Field(default=None)


# ---


class HealthOut(BaseModel):
    """P3-2 健康分级：healthy 全好；degraded 仅 Redis 挂但核心可用；unhealthy DB 挂。"""

    status: Literal["healthy", "degraded", "unhealthy"]
    db: bool
    redis: bool | None = None
    version: str
    metrics_status: str = "unknown"  # last metrics collect status: ok / db：... / not_collected


class MetricsOut(BaseModel):
    """P3-2 /metrics：后台采集缓存快照，禁止请求时实时查库。"""

    model_config = ConfigDict(extra="allow")

    collected_at: str | None = None
    error: str | None = None


class ErrorOut(BaseModel):
    detail: str


# --- P1-3 WS 入参校验（禁止裸 dict 透传业务逻辑） -----------------------------

class WsIncoming(BaseModel):
    """WS 上行消息的通用校验骨架：kind 白名单 + payload 结构。"""

    kind: str = Field(min_length=1)
    msg_id: str | None = None
    task_id: str | None = None
    payload: dict | None = Field(default_factory=dict)


class WsUserMessage(WsIncoming):
    """合法的业务上行：user_message。kind 单词校验后由 ws 层二次核对。"""

    kind: Literal["user_message"]
    payload: dict = Field(..., description="须含 text 字段")

    @property
    def text(self) -> str:
        v = (self.payload or {}).get("text", "")
        return v if isinstance(v, str) else ""


class WsUserDecision(WsIncoming):
    """P1-5 上行：对中断任务给出人工决策（审批 approve/reject / 追问 answer）。"""

    kind: Literal["user_decision"]
    payload: dict = Field(..., description="须含 task_id 与 decision")

    @property
    def decision(self) -> dict:
        v = (self.payload or {}).get("decision", {})
        return v if isinstance(v, dict) else {}
