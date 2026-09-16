"""P7-D2 并行子步骤派发：仅子步骤级并行，不产生独立 TaskNode。

审查闭环（D2 范围界定 v2 真相源保护 + 资源隔离）：
- **仅子步骤级并行**：并行单位是某个节点内派发的子步骤，**不新建 TaskNode**，所有状态
  收敛回调用方（AgentRunner），由主 TaskNode 唯一真相源统一写回。
- **并行回滚原子性**：任一子步失败 → 该批次整体判失败（``CollabResult.ok=False``），
  不残留部分成功；调用方据此把主节点置 failed，绝不落半成功态。
- **资源隔离**：独立 ``worker_pool`` 并发槽（注册表内管理与主线 ARQ worker 解耦）。
- 全部结果按子步 id 稳定排序返回，便于审计（协作审计可视化）。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class SubStep:
    """一个并行子步骤（由 supervisor 计划拆解而来，仅做并行执行单位）。"""

    id: str
    role: str
    title: str
    description: str
    acceptance_criteria: str = ""


@dataclass
class SubOutcome:
    """单个子步骤的结算结果（成功或失败均记录，供审计/合并）。"""

    id: str
    ok: bool
    text: str = ""
    error: str = ""


@dataclass
class CollabResult:
    """一批并行子步骤的结算（全程收敛，供调用方同步主 TaskNode）。"""

    ok: bool
    subs: list[SubOutcome] = field(default_factory=list)
    error: str = ""

    @property
    def text(self) -> str:
        """合并产物文本（按子步顺序拼接，含失败标记）。"""
        parts = []
        for s in self.subs:
            tag = "✅" if s.ok else "❌"
            parts.append(f"[{tag} {s.id}] {s.text or s.error}".rstrip())
        return "\n\n".join(p for p in parts if p)


# 执行器：async (SubStep, index) -> str
SubExecutor = Callable[[SubStep, int], Awaitable[str]]


async def run_parallel(steps: list[SubStep], executor: SubExecutor, *,
                       pool: int = 2) -> CollabResult:
    """并发执行子步骤；任一失败即整体失败并取消在途任务（并行回滚原子性）。"""
    if not steps:
        return CollabResult(ok=True, subs=[])

    sem = asyncio.Semaphore(max(1, pool))
    # 用事件触发「首个失败即取消全部在途」
    cancel_evt = asyncio.Event()

    async def _guarded(index: int, step: SubStep) -> SubOutcome:
        async with sem:
            if cancel_evt.is_set():
                return SubOutcome(id=step.id, ok=False, error="parallel_aborted")
            try:
                text = await executor(step, index)
                return SubOutcome(id=step.id, ok=True, text=text)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 单步失败不应使事件循环崩溃
                cancel_evt.set()
                logger.warning("并行子步失败 step=%s：%s", step.id, exc)
                return SubOutcome(id=step.id, ok=False, error=f"{type(exc).__name__}: {exc}")

    tasks = [asyncio.create_task(_guarded(i, s)) for i, s in enumerate(steps)]
    # gather 保持任务顺序 = 输入顺序，直接可按输入 id 稳定对应（审计稳定）
    subs = list(await asyncio.gather(*tasks))

    failed = [o for o in subs if not o.ok]
    if failed:
        cancel_evt.set()  # 通知未醒来的任务跳过（幂等）
        return CollabResult(ok=False, subs=subs,
                            error="并行子步存在失败：" + "；".join(f"{o.id}:{o.error}" for o in failed))
    return CollabResult(ok=True, subs=subs)


def split_substeps(plan_decision: dict | None) -> list[SubStep]:
    """从 supervisor 计划决策中抽取可并行子步骤（无则返回空 → 退化为单执行）。"""
    if not isinstance(plan_decision, dict):
        return []
    subs: list[SubStep] = []
    for item in plan_decision.get("substeps", []) or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        subs.append(SubStep(
            id=str(item["id"]),
            role=str(item.get("role", "doer")),
            title=str(item.get("title", item["id"])),
            description=str(item.get("description", "")),
            acceptance_criteria=str(item.get("acceptance_criteria", "")),
        ))
    return subs
