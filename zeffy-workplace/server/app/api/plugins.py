"""P7-D1 插件市场管理 API（探索线，独立于产物治理 API）。

安全与兼容锚点（审查🔴）：
- **总闸短路**：``PLUGINS_ENABLED`` 或总闸 ``ARTIFACT_META_ENABLED`` 任一关闭 → 全部 404，
  运行时功能完全不可达（零漂移）。
- **分级权限**：
  - 浏览 /plugins/market、已装列表、安装 / 卸载 / 启停：任意登录用户（AUTH 关=匿名，P2 兼容）。
  - 管理端 上架 / 下架 / 拉黑 / 解黑：仅 ``ENABLE_ADMIN`` 开启且 admin/system 角色（否则 404）。
- **执行集成**：安装成功即把该插件能力注册进共享 ToolRegistry；卸载/停用即从注册表移除，
  插件异常走 registry 计次/超时/熔断，不影响主进程。

存储为独立 JSON（``PLUGINS_STORE_DIR``），不触碰主线 storage/governance/crypto/DB。
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.deps import UserPrincipal, get_current_user
from app.config import get_settings
from app.plugins.market import MarketError, PluginForbidden, PluginNotFound, get_market

router = APIRouter(prefix="/plugins", tags=["plugins"])


# ---- 参数模型 ----
class PublishReq(BaseModel):
    manifest: dict
    source: str
    publisher: str = ""


class BlacklistReq(BaseModel):
    key: str
    reason: str = ""


# ---- 门禁 ----
def _plugins_gate() -> None:
    """总闸短路：关闭则完全不可达（404，避免资源枚举）。"""
    s = get_settings()
    if not (s.PLUGINS_ENABLED and s.ARTIFACT_META_ENABLED):
        raise HTTPException(status_code=404, detail="Not Found")


def _plugins_admin(user: Annotated[UserPrincipal, Depends(get_current_user)]) -> UserPrincipal:
    """插件管理端：总闸 + ENABLE_ADMIN + admin 角色，否则 404。"""
    s = get_settings()
    if not (s.PLUGINS_ENABLED and s.ARTIFACT_META_ENABLED):
        raise HTTPException(status_code=404, detail="Not Found")
    if not s.ENABLE_ADMIN or not (user.is_system or user.role_is_admin()):
        raise HTTPException(status_code=404, detail="Not Found")
    return user


def _market():
    return get_market()


# FastAPI 依赖注入：Annotated 模式（B008 合规，对齐 admin.py 风格）
_MarketDep = Annotated[Any, Depends(_market)]


# ---- 浏览 / 安装（任意登录用户）----
@router.get("/market", dependencies=[Depends(_plugins_gate)])
async def list_market(mkt: _MarketDep):
    return {"plugins": mkt.list_available()}


@router.get("/installed", dependencies=[Depends(_plugins_gate)])
async def list_installed(mkt: _MarketDep):
    return {"plugins": mkt.list_installed()}


@router.post("/{plugin_id}/install", dependencies=[Depends(_plugins_gate)])
async def install(plugin_id: str, mkt: _MarketDep):
    try:
        return mkt.install(plugin_id)
    except PluginNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except PluginForbidden as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None


@router.post("/{plugin_id}/uninstall", dependencies=[Depends(_plugins_gate)])
async def uninstall(plugin_id: str, mkt: _MarketDep):
    try:
        return mkt.uninstall(plugin_id)
    except PluginNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


@router.post("/{plugin_id}/enable", dependencies=[Depends(_plugins_gate)])
async def enable(plugin_id: str, mkt: _MarketDep):
    try:
        return mkt.set_enabled(plugin_id, True)
    except PluginNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except MarketError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post("/{plugin_id}/disable", dependencies=[Depends(_plugins_gate)])
async def disable(plugin_id: str, mkt: _MarketDep):
    try:
        return mkt.set_enabled(plugin_id, False)
    except PluginNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except MarketError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


# ---- 管理端（上架/下架/拉黑）----
@router.post("/admin/publish", dependencies=[Depends(_plugins_admin)])
async def publish(req: PublishReq, mkt: _MarketDep):
    from app.plugins.market import GateFailed

    try:
        return mkt.publish(req.manifest, req.source, publisher=req.publisher)
    except GateFailed as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except MarketError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post("/admin/{plugin_id}/delist", dependencies=[Depends(_plugins_admin)])
async def delist(plugin_id: str, mkt: _MarketDep):
    try:
        return mkt.delist(plugin_id)
    except PluginNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


@router.post("/admin/blacklist", dependencies=[Depends(_plugins_admin)])
async def blacklist(req: BlacklistReq, mkt: _MarketDep):
    mkt.blacklist(req.key, reason=req.reason)
    return {"ok": True, "key": req.key}


@router.post("/admin/unblacklist", dependencies=[Depends(_plugins_admin)])
async def unblacklist(req: BlacklistReq, mkt: _MarketDep):
    removed = mkt.unblacklist(req.key)
    return {"ok": removed, "key": req.key}
