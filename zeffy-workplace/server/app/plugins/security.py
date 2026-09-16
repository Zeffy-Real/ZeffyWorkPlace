"""P7-D1 插件能力沙箱核心（探索线，独立于产物治理线）。

审查闭环（D1 范围界定 v2）：
- **能力沙箱**：插件运行于**受限作用域**，仅注入白名单能力对象 + 受限内置（禁 __import__/open/eval/exec/compile），
  间接规避对内部模块的访问；**静态扫描门禁**在上架/加载前扫描代码，命中内部模块引用或危险调用直接驳回。
- **权限三级 + 最小权限**：only_read(RO) / read_write(RW) / admin(ADMIN)；默认零权限按需申请。
  每次能力调用经 manifest 声明权限校验，越界抛 ``ToolPermissionError``。
- **异常熔断**：复用 ``ToolRegistry`` 计次/超时/异常收敛；插件异常不影响主进程。

兼容锚点：``PLUGINS_ENABLED=false``（或总闸关）→ 插件完全不加载，工具注册表零漂移。

模块级导入零副作用。
"""
from __future__ import annotations

# 敏感/内部模块引用片段（静态扫描黑名单，防插件侵入主线）
FORBIDDEN_MODULES = (
    "app.storage", "app.storage.", "governance", "crypto_gate", "crypto.",
    "app.db", "sqlalchemy", "app.workflow", "app.agents",
)
# 危险调用/接口（静态扫描黑名单）
FORBIDDEN_CALLS = (
    "subprocess", "os.system", "os.popen", "open(", "eval(", "exec(", "compile(",
    "__import__", "importlib", "ctypes", "socket.",
)
# 受限内置需移除的危险名称
_STRIP_BUILTINS = ("__import__", "open", "input", "eval", "exec", "compile", "globals", "locals")


class PluginError(Exception):
    """插件加载/执行业务异常。"""


class PluginSecurityError(PluginError):
    """插件命中安全红线（越权/危险调用）→ 拒绝/终止。"""


def static_scan(source: str) -> list[str]:
    """静态安全扫描：返回命中的红线片段列表（空=通过）。"""
    hits: list[str] = []
    low = source
    for frag in FORBIDDEN_MODULES:
        if frag in low:
            hits.append(frag)
    for frag in FORBIDDEN_CALLS:
        if frag in low:
            hits.append(frag)
    return hits


def minimal_builtins() -> dict:
    """受限内置（无 __import__/open/eval/exec/compile 等危险能力），作插件执行作用域 globals 基底。"""
    import builtins

    b = dict(vars(builtins))
    for name in _STRIP_BUILTINS:
        b.pop(name, None)
    return b
