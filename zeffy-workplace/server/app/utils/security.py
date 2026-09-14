"""安全工具（通用层，不属于 db 业务模块）。

Agent 文件工具的安全壳子：工作区白名单路径解析。
这是后续所有 Agent 文件工具（读/写/删除）的唯一路径校验入口，
P0 就实现并测试，避免 P1 临时漏写导致目录穿越漏洞（CWE-22）。

说明：P1 追加权限校验、输入过滤等安全工具时，统一放在本模块，避免职责混乱。
"""

from __future__ import annotations

from pathlib import Path

from app.config import get_settings


class WorkspacePathError(ValueError):
    """路径越界/非法时抛出。"""


def safe_resolve_workspace_path(relative_path: str, root: Path | None = None) -> Path:
    """将相对路径安全解析为空绝对路径并存于 WORKSPACE_ROOT 内。

    - 仅接受相对路径；拒绝绝对路径与盘符。
    - 使用 .resolve() 后校验前缀，防 `../` 穿越与 symlink 逃逸。
    - 越界抛 WorkspacePathError。

    :param relative_path: 相对 WORKSPACE_ROOT 的路径，如 "task-abc/readme.md"
    """
    root = (root or get_settings().WORKSPACE_ROOT).resolve()
    candidate = Path(relative_path)

    if candidate.is_absolute():
        raise WorkspacePathError(f"拒绝绝对路径：{relative_path!r}")

    # 不 resolve 前先做一次前缀校验，避免 Path('..') 各类拼接歧义
    parts = candidate.parts
    if parts and parts[0] in {"..", "~"}:
        raise WorkspacePathError(f"拒绝穿越路径：{relative_path!r}")

    resolved = root.joinpath(candidate).resolve()

    # 关键校验：解析后的真实路径必须仍在 root 之下
    if not (resolved == root or root in resolved.parents):
        raise WorkspacePathError(f"路径越出工作区白名单：{relative_path!r}")

    return resolved
