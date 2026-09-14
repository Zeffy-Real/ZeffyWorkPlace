"""工具注册表（P1-3）。

约束（⭐，审查强制）：
- 每个工具**必须**声明：``name`` / ``description`` / ``permission`` / ``timeout`` / ``max_calls``。
- ``max_calls``：单任务内该工具的最大调用次数，超限直接阻断（防 Agent 失控刷写/刷读）。
- ``timeout``：单次调用超时上限，超时判定调用失败（防止工具卡死拖垮事件循环）。
- 所有工具均为 ``async`` 回调；参数经 ``**kwargs`` 透传。
- 工具调用日志（谁调用、时长、参数摘要、结果状态）由调用方写入 AuditLog，
  注册表本身不依赖 DB（保持纯函数，便于测试与复用）。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

# 工具回调：async，接收任意 kwargs，返回 dict 结果。
ToolHandler = Callable[..., Awaitable[dict[str, Any]]]


class ToolError(Exception):
    """工具执行失败（参数非法、IO 失败等）。"""


class ToolPermissionError(ToolError):
    """工具越权/越界访问（路径逃逸、白名单外操作等）。"""


@dataclass
class ToolResult:
    """一次工具调用的标准化返回值。"""

    name: str
    status: str  # ok / error
    data: dict[str, Any] | None = None
    error: str | None = None
    duration: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass(frozen=True)
class ToolSpec:
    """工具元数据（含超时/最大调用次数约束）。"""

    name: str
    description: str
    permission: str
    timeout: float
    max_calls: int
    handler: ToolHandler


class ToolRegistry:
    """工具注册表 + 执行沙箱（per-run 计次 + 超时）。"""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._calls: dict[str, int] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ToolError(f"工具重复注册：{spec.name}")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError:
            raise ToolError(f"未知工具：{name!r}") from None

    def list(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def reset(self) -> None:
        """重置本 run 的调用计数（一次任务开始前调用）。"""
        self._calls.clear()

    def remaining(self, name: str) -> int:
        spec = self.get(name)
        return max(0, spec.max_calls - self._calls.get(name, 0))

    async def run(self, name: str, **kwargs: Any) -> ToolResult:
        """执行工具：校验权限等级占位、超限阻断、超时控制。"""
        try:
            spec = self.get(name)
        except ToolError as exc:
            return ToolResult(name=name, status="error", error=str(exc))

        if self._calls.get(name, 0) >= spec.max_calls:
            return ToolResult(
                name=name,
                status="error",
                error=f"工具 {name} 达到最大调用次数上限({spec.max_calls})，本次调用被阻断",
            )

        self._calls[name] = self._calls.get(name, 0) + 1
        start = time.perf_counter()
        try:
            data = await asyncio.wait_for(spec.handler(**kwargs), timeout=spec.timeout)
            return ToolResult(name=name, status="ok", data=data, duration=time.perf_counter() - start)
        except asyncio.TimeoutError:
            return ToolResult(
                name=name,
                status="error",
                error=f"工具 {name} 执行超时(>{spec.timeout}s)",
                duration=time.perf_counter() - start,
            )
        except ToolError as exc:
            return ToolResult(name=name, status="error", error=str(exc), duration=time.perf_counter() - start)
        except Exception as exc:  # noqa: BLE001 工具内部未捕获异常不静默
            return ToolResult(
                name=name, status="error", error=f"工具 {name} 异常：{type(exc).__name__}: {exc}",
                duration=time.perf_counter() - start,
            )