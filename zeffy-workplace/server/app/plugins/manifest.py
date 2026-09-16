"""P7-D1 插件清单（manifest）校验 + 权限三级 + 上架四项安全门禁校验。

权限三级：RO（只读查询）/ RW（读写产物）/ ADMIN（管理/系统操作）；默认零权限，按需申请。
上架门禁四项：完整性 / 签名 / 静态安全扫描 / 权限声明审核（本模块提供校验器，签名校验由市场层完成）。
"""
from __future__ import annotations

import re
from enum import IntEnum

from app.plugins import security


class Perm(IntEnum):
    """插件能力权限等级：默认零权限，按需申请。"""
    NONE = 0
    RO = 1       # 只读（查询/列表）
    RW = 2       # 读写（创建/修改产物）
    ADMIN = 3    # 管理（配置/系统操作）


_PERM_NAMES = {"none": Perm.NONE, "read": Perm.RO, "write": Perm.RW, "admin": Perm.ADMIN}
_ID_RE = re.compile(r"^[a-zA-Z0-9][\w.-]{0,63}$")
_VER_RE = re.compile(r"^\d+\.\d+\.\d+$")


class ManifestError(Exception):
    pass


def validate_manifest(m: dict) -> tuple[bool, str]:
    """校验插件清单：id/版本/能力/权限/作者必填 + 字段合法。返回 (ok, err)。"""
    if not isinstance(m, dict):
        return False, "manifest 必须为对象"
    for field in ("id", "version", "capabilities", "permissions", "author"):
        if not m.get(field):
            return False, f"缺少必填字段: {field}"
    pid = str(m["id"])
    if not _ID_RE.match(pid):
        return False, f"插件 id 非法: {pid!r}"
    if not _VER_RE.match(str(m["version"])):
        return False, f"版本号非法(需 x.y.z): {m['version']!r}"
    caps = m["capabilities"]
    perms = m["permissions"]
    if not isinstance(caps, list) or not caps:
        return False, "capabilities 必须为非空能力列表"
    if not isinstance(perms, dict) or not perms:
        return False, "permissions 必须声明能力->权限映射"
    if not isinstance(m.get("author"), str) or not m["author"].strip():
        return False, "author 非法"
    return True, ""


def resolve_permissions(perms: dict) -> dict[str, Perm]:
    """解析权限声明为非负等级映射；未声明/非法映射按 NONE。"""
    out: dict[str, Perm] = {}
    for cap, p in (perms or {}).items():
        if isinstance(p, str):
            out[cap] = _PERM_NAMES.get(p.strip().lower(), Perm.NONE)
        elif isinstance(p, int) and p in (0, 1, 2, 3):
            out[cap] = Perm(p)
        else:
            out[cap] = Perm.NONE
    return out


def check_permission(perm: Perm, required: Perm) -> bool:
    """权限校验：实际等级 >= 需求等级才放行。"""
    return int(perm) >= int(required)


def run_upload_gate(m: dict, source: str) -> tuple[bool, str]:
    """上架四项门禁：完整性(字段) + 签名(占位，市场层) + 静态扫描 + 权限声明。"""
    ok, err = validate_manifest(m)
    if not ok:
        return False, f"清单非法: {err}"
    hits = security.static_scan(source)
    if hits:
        return False, f"静态安全扫描命中红线: {hits}"
    perms = resolve_permissions(m["permissions"])
    if any(p == Perm.ADMIN for p in perms.values()) and str(m.get("vendor", "third")) != "official":
        return False, "第三方插件禁止声明 ADMIN 权限"
    if any(p == Perm.NONE for p in perms.values()) and len(perms) != len(m["capabilities"]):
        return False, "部分能力未声明权限（最小权限原则：须逐能力声明）"
    return True, "gate_ok"
