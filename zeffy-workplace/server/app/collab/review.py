"""P7-D2 评审环收敛门闸：迭代上限 + 总超时 + 死循环检测。

审查闭环（D2 范围界定 v2 评审环无收敛保障）：
- **双重收敛**：``max_iter``（迭代次数上限）+ ``deadline``（总超时，秒）任一触发即终止，
  返回收敛失败原因（调用方置节点失败并审计）。
- **循环检测**：相同评审意见（归一化后）重复出现 >= ``loop_penalty``（默认 2）次 →
  判定死循环，强制终止，避免评审-修改无限循环。
- **人工介入入口**：``human_override(key)`` 供上层在评审环卡住时注入裁决放行。
- 本模块纯逻辑；总闸由调用方（runner）判定——``AGENT_COLLAB_ENABLED=false`` 时不启用，
  复用既有 ``tpl.max_revision`` 行为（零漂移）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class ReviewGate:
    """一次评审环的收敛控制器（多轮评审共享同一实例）。"""

    max_iter: int = 3
    timeout: float = 120.0
    loop_penalty: int = 2  # 同一意见归一化后重复达几次判死循环
    _started: float = field(default_factory=time.monotonic)
    _seen: list[str] = field(default_factory=list)
    _override_by: str = ""

    @staticmethod
    def _norm(comment: str) -> str:
        return " ".join(str(comment).strip().lower().split())

    def human_override(self, by: str) -> None:
        """人工裁决放行（评审环卡住时的人肉入口）。"""
        self._override_by = by

    def check(self, round_no: int, comments: list[str]) -> str | None:
        """返回终止原因；无不终止。round_no 从 1 起。"""
        if self._override_by:
            return "human_override"
        if round_no > self.max_iter:
            return "too_many_iter"
        if time.monotonic() - self._started > self.timeout:
            return "timeout"
        normed = [self._norm(c) for c in comments if self._norm(c)]
        self._seen.extend(normed)
        for n in normed:
            if self._seen.count(n) >= self.loop_penalty:
                return "loop_detected"
        return None
