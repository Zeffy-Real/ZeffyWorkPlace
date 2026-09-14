"""WS 消息协议：前端与后端共享的结构化 JSON 消息格式。

核心共识（P0 就定型，P1 群聊直接复用）：
- 禁止裸文本收发，一律结构化 JSON。
- kind 枚举已预埋 P1 的 agent_reply / task_update / review_notify。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal


class WsKind(StrEnum):
    USER_MESSAGE = "user_message"
    AGENT_REPLY = "agent_reply"
    SYSTEM_NOTIFY = "system_notify"
    TASK_UPDATE = "task_update"
    REVIEW_NOTIFY = "review_notify"
    # P1-3 新增下发 kind
    AGENT_MESSAGE = "agent_message"
    TASK_NODE_UPDATE = "task_node_update"
    REVIEW_EVENT = "review_event"
    # P1-5 新增上行：对中断任务给出人工决策（审批/追问补充）
    USER_DECISION = "user_decision"


# 允许的 kind 字面量
WsKindLiteral = Literal[
    "user_message",
    "agent_reply",
    "system_notify",
    "task_update",
    "review_notify",
    "agent_message",
    "task_node_update",
    "review_event",
    "user_decision",
]


class WsMessage:
    """一条 WS 消息的结构化容器。"""

    __slots__ = ("msg_id", "kind", "payload", "task_id", "timestamp")

    def __init__(
        self,
        *,
        kind: WsKindLiteral,
        payload: str | dict,
        task_id: str | None = None,
        msg_id: str | None = None,
    ) -> None:
        self.msg_id = msg_id or str(uuid.uuid4())
        self.kind = kind
        self.payload = payload
        self.task_id = task_id
        self.timestamp = datetime.now(UTC).isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "msg_id": self.msg_id,
            "kind": self.kind,
            "payload": self.payload,
            "task_id": self.task_id,
            "timestamp": self.timestamp,
        }


def parse_incoming(raw: str) -> WsMessage:
    """解析收到的字符串为 WsMessage；不符合协议抛 ValueError。

    P0 的 echo 需要 kind=user_message。
    """
    import json

    data = json.loads(raw)
    kind = data.get("kind")
    if kind not in {k.value for k in WsKind}:
        raise ValueError(f"未知 kind：{kind!r}")
    return WsMessage(
        kind=kind,  # type: ignore[arg-type]
        payload=data.get("payload", ""),
        task_id=data.get("task_id"),
        msg_id=data.get("msg_id"),
    )


def build_echo_reply(received: WsMessage) -> WsMessage:
    """P0 echo：对 user_message 回 system_notify。"""
    return WsMessage(
        kind=WsKind.SYSTEM_NOTIFY.value,
        payload=f"pong:{received.payload}",
        task_id=received.task_id,
    )
