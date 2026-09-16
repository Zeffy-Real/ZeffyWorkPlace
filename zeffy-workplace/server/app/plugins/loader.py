"""P7-D1 插件加载器：受限作用域执行 + 能力→ToolSpec 构建 + 注册进工具注册表。

- **受限作用域**：``exec`` 于 ``minimal_builtins()`` globals，**仅注入白名单对象** ``inject``；
  无 ``__import__``/``open``/``eval``/``exec``/``compile``，插件无法导入内部包外部系统访问。
- **静态扫描门禁**：加载前 ``static_scan``，命中内部模块/危险调用 → ``PluginSecurityError`` 直接拒绝。
- **权限三级**：能力按 manifest 权限映射构建 ``ToolSpec.permission``；由调用方（registry/上层）校验。
- **异常熔断**：exec/注册异常收敛为 ``PluginError``，不影响主进程。
"""
from __future__ import annotations

import asyncio
from typing import Any

from app.config import get_settings
from app.plugins import security
from app.plugins.manifest import Perm, resolve_permissions
from app.tools.registry import ToolError, ToolRegistry, ToolSpec

_CAP_ATTR = "CAPABILITIES"  # 插件暴露能力的约定属性 {cap_name: callable}


def _as_async(fn: Any) -> Any:
    """把插件能力适配为 ToolRegistry 约定的 async 回调（同步能力包一层薄壳）。"""
    if asyncio.iscoroutinefunction(fn):
        return fn

    async def wrapper(**kwargs: Any) -> Any:
        return fn(**kwargs)

    wrapper.__name__ = getattr(fn, "__name__", "cap")
    return wrapper


def load_plugin(source: str, *, inject: dict | None = None) -> dict[str, Any]:
    """在受限作用域执行插件源码，返回其 CAPABILITIES 映射。红线命中/执行异常抛 PluginError。

    :param source: 插件源码文本。
    :param inject: 白名单注入对象 {global_name: value}（仅此列表可被插件访问）。
    """
    hits = security.static_scan(source)
    if hits:
        raise security.PluginSecurityError(f"插件静态扫描命中红线: {hits}")
    globals_ = security.minimal_builtins()
    globals_["__builtins__"] = security.minimal_builtins()
    if inject:
        for name, val in inject.items():
            if not _SAFE_NAME.match(name):
                raise security.PluginSecurityError(f"注入名称非法: {name!r}")
            globals_[name] = val
    try:
        exec(compile(source, "<plugin>", "exec"), globals_)  # noqa: S102 受限作用域，无危险内置
    except Exception as exc:  # noqa: BLE001
        raise security.PluginError(f"插件执行失败: {type(exc).__name__}: {exc}") from exc
    caps = globals_.get(_CAP_ATTR)
    if not isinstance(caps, dict):
        raise security.PluginError("插件未声明 CAPABILITIES 能力映射")
    return caps


import re as _re  # noqa: E402 需在方法外定义

_SAFE_NAME = _re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def install_plugin(registry: ToolRegistry, *, plugin_id: str, manifest: dict,
                   capabilities: dict[str, Any]) -> dict:
    """把插件能力注册为受限 ToolSpec 进工具注册表（越权/非法抛 PluginError）。"""
    perms = resolve_permissions(manifest["permissions"])
    s = get_settings()
    created: list[str] = []
    for cap in manifest["capabilities"]:
        handler = capabilities.get(cap)
        if handler is None or not callable(handler):
            raise security.PluginSecurityError(f"能力 {cap} 未提供可调用实现（未声明超权限）")
        perm = perms.get(cap, Perm.NONE)
        if perm == Perm.NONE:
            raise security.PluginSecurityError(f"能力 {cap} 未声明权限（最小权限原则）")
        tool_name = f"plugin:{plugin_id}:{cap}"
        spec = ToolSpec(name=tool_name, description=f"[plugin:{plugin_id}] {cap}",
                        permission=str(perm.name.lower()), timeout=10.0,
                        max_calls=max(1, int(s.PLUGINS_MAX_CALLS)),
                        handler=_as_async(handler))
        try:
            registry.register(spec)
        except ToolError as exc:
            raise security.PluginError(f"能力注册冲突 {tool_name}: {exc}") from exc
        created.append(tool_name)
    return {"plugin_id": plugin_id, "caps": created}
