"""P4-4 成本统计：从 audit_logs 聚合近窗口 token/费用。

- 聚合粒度 (task, model) 分别匹配单价，避免多模型混合误算（审查🔴）。
- 缺定价模型 → 单价记 0 并列入 ``unknown_price_models``。
- 权限：非 admin 仅统计自己任务；AUTH off（匿名）统计全部。
- 导出 CSV 带 UTF-8 BOM（兼容 Excel，防中文乱码）。
"""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from app.api.schemas import BillingRowOut, BillingSummaryOut
from app.auth.deps import UserPrincipal, get_current_user
from app.config import get_settings
from app.db import repos
from app.db.base import get_session_factory

router = APIRouter(prefix="/billing", tags=["billing"])
CurrentUser = Annotated[UserPrincipal, Depends(get_current_user)]


def _pricing() -> dict:
    return get_settings().MODEL_PRICING or {}


def _cost(model: str, prompt: int, completion: int) -> float:
    p = _pricing().get(model)
    if not p:
        return 0.0  # 🔴 缺价记 0（调用方列入 unknown）
    return (prompt / 1_000_000) * (p.get("prompt_per_1m") or 0.0) + \
        (completion / 1_000_000) * (p.get("completion_per_1m") or 0.0)


async def _load(user: UserPrincipal):
    """返回 (since, scoped_user_id)。admin/匿名→全量；登录普通用户→自己任务。"""
    s = get_settings()
    since = datetime.now(UTC) - timedelta(days=s.BILLING_WINDOW_DAYS)
    scoped = None
    if user.authenticated and not user.role_is_admin():
        scoped = user.id
    return since, scoped


async def _aggregate(since, scoped) -> dict:
    factory = get_session_factory()
    async with factory() as session:
        rows = await repos.usage_rows(session, since=since, user_id=scoped)
    agg: dict[tuple[str, str], dict] = {}
    unknown: set[str] = set()
    for r in rows:
        key = (r["task_id"], r["model"])
        d = agg.setdefault(key, {
            "task_id": r["task_id"], "model": r["model"],
            "prompt_tokens": 0, "completion_tokens": 0, "amount": 0.0,
        })
        d["prompt_tokens"] += r["prompt_tokens"]
        d["completion_tokens"] += r["completion_tokens"]
        if not _pricing().get(r["model"]):
            unknown.add(r["model"] or "(unknown)")
    for d in agg.values():
        d["amount"] = _cost(d["model"], d["prompt_tokens"], d["completion_tokens"])
        d["total_tokens"] = d["prompt_tokens"] + d["completion_tokens"]
    return {"agg": agg, "unknown": sorted(unknown)}


@router.get("/summary", response_model=BillingSummaryOut)
async def summary(user: CurrentUser) -> BillingSummaryOut:
    since, scoped = await _load(user)
    data = await _aggregate(since, scoped)
    rows = sorted(data["agg"].values(), key=lambda d: (-d["total_tokens"], d["task_id"]))
    return BillingSummaryOut(
        window_days=get_settings().BILLING_WINDOW_DAYS,
        rows=[BillingRowOut(**{k: d.get(k, 0) for k in BillingRowOut.model_fields}) for d in rows],
        totals=_totals(rows),
        unknown_price_models=data["unknown"],
    )


def _totals(rows: list[dict]) -> dict[str, float]:
    return {
        "prompt_tokens": sum(r["prompt_tokens"] for r in rows),
        "completion_tokens": sum(r["completion_tokens"] for r in rows),
        "total_tokens": sum(r["total_tokens"] for r in rows),
        "amount": round(sum(r["amount"] for r in rows), 4),
    }


@router.get("/export.csv")
async def export_csv(user: CurrentUser) -> Response:
    since, scoped = await _load(user)
    data = await _aggregate(since, scoped)
    rows = sorted(data["agg"].values(), key=lambda d: (-d["total_tokens"], d["task_id"]))
    # 管理操作全审计（审查⭐）：成本导出记录 user_id
    if user.authenticated:
        factory = get_session_factory()
        async with factory() as session:
            await repos.write_audit(session, task_id=None, operator="user",
                                    action="billing_export", detail={"user_id": user.id})
    buf = io.StringIO()
    buf.write("\ufeff")  # UTF-8 BOM（Excel 兼容）
    buf.write("task_id,model,prompt_tokens,completion_tokens,total_tokens,amount\n")
    for d in rows:
        buf.write(
            f"{d['task_id']},{d['model']},{d['prompt_tokens']},"
            f"{d['completion_tokens']},{d['total_tokens']},{d['amount']:.4f}\n"
        )
    return Response(content=buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=billing.csv"})
