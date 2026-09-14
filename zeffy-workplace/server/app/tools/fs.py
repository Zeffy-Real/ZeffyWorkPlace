"""文件系统工具（P1-3）。

安全约束（🔴，审查强制）：
- 所有文件读写**必须**经 ``safe_resolve_workspace_path`` 解析，把相对路径规范化为
  ``<WORKSPACE_ROOT>/task-<task_id>/...`` 下的绝对路径，并校验不逃逸工作区根。
- 幂等防护（🔴）：多次执行同一 Agent 会重复输出同一文件。写文件支持三种模式：
  - ``overwrite``：覆盖已存在文件（默认**不**使用）。
  - ``no_overwrite``：文件已存在则拒绝（默认模式，防重复覆盖丢数据）。
  - ``new``：生成唯一文件名（追加 ``-<run_id>`` 后缀），克隆层次不覆盖。
  写操作采用「临时文件 + rename」原子模式，避免半截文件。
- 一切操作失败抛 ``ToolPermissionError`` / ``ToolError``，由注册表转为失败结果。
"""

from __future__ import annotations

import os
from pathlib import Path

from app.tools.registry import ToolError, ToolPermissionError, ToolSpec


def safe_resolve_workspace_path(workspace_root: str | Path, rel: str) -> Path:
    """把 ``rel`` 解析为工作区根下的绝对路径；逃逸根目录则抛 ToolPermissionError。

    :param workspace_root: 绝对工作区根路径。
    :param rel: 相对路径（允许 ``task-<id>/x.md``）。
    """
    root = Path(workspace_root).resolve()
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise ToolPermissionError(f"路径越界，禁止访问工作区之外：{rel!r}")
    return target


def _task_dir(workspace_root: str | Path, task_id: str) -> Path:
    safe = safe_resolve_workspace_path(workspace_root, "");
    base = safe / f"task-{task_id}"  # base 已在根内
    base.mkdir(parents=True, exist_ok=True)
    return base


async def _write_file(*, workspace_root: str, task_id: str, path: str,
                      content: str, mode: str = "no_overwrite",
                      run_id: str = "") -> dict:
    """写文件工具处理函数。

    :param mode: overwrite | no_overwrite | new
    """
    if mode not in {"overwrite", "no_overwrite", "new"}:
        raise ToolError(f"非法写入模式：{mode!r}（可选 overwrite/no_overwrite/new）")

    if "\x00" in path:
        raise ToolError("非法路径（含 NUL 字节）")
    if ".." in path.replace(os.sep, "/").split("/"):
        raise ToolError("非法路径（不允许上级目录跳转）")

    base = _task_dir(workspace_root, task_id)
    target = safe_resolve_workspace_path(workspace_root, f"task-{task_id}/{path}")

    if mode == "new":
        stem, ext = os.path.splitext(path)
        unique = f"{stem}-{run_id}{ext}" if run_id else f"{stem}-{_timestamp()}{ext}"
        target = base / unique

    if target.exists() and mode == "no_overwrite":
        raise ToolError(f"文件已存在且 mode=no_overwrite，拒绝覆盖：{path}")

    # 原子写：临时文件 + rename
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, target)

    rel = target.relative_to(base).as_posix()
    return {"path": rel, "abs_path": str(target), "mode": mode, "bytes": len(content.encode("utf-8")), "exists": target.exists()}


async def _read_file(*, workspace_root: str, task_id: str, path: str) -> dict:
    target = safe_resolve_workspace_path(workspace_root, f"task-{task_id}/{path}")
    if not target.is_file():
        raise ToolError(f"文件不存在：{path}")
    return {"path": path, "content": target.read_text(encoding="utf-8")}


async def _list_dir(*, workspace_root: str, task_id: str, path: str = "") -> dict:
    base = _task_dir(workspace_root, task_id)
    target = safe_resolve_workspace_path(workspace_root, f"task-{task_id}/{path}")
    if not target.is_dir():
        raise ToolError(f"目录不存在：{path or '.'}")
    entries = [p.name for p in sorted(target.iterdir())]
    return {"path": path or ".", "entries": entries, "count": len(entries)}


def _timestamp() -> str:
    import time

    return time.strftime("%H%M%S")


def make_fs_tools(workspace_root: str | Path) -> list[ToolSpec]:
    """构建 fs 相关工具规范列表（默认权限等级 'fs'）。"""
    root = str(Path(workspace_root).resolve())
    return [
        ToolSpec(
            name="fs_write",
            description="把文本内容写入任务目录下的文件。mode=overwrite/no_overwrite/new；默认 no_overwrite（不盲目覆盖）。",
            permission="fs",
            timeout=10.0,
            max_calls=50,
            handler=lambda **kw: _write_file(workspace_root=root, **kw),
        ),
        ToolSpec(
            name="fs_read",
            description="读取任务目录下指定文件内容。",
            permission="fs",
            timeout=10.0,
            max_calls=100,
            handler=lambda **kw: _read_file(workspace_root=root, **kw),
        ),
        ToolSpec(
            name="fs_list",
            description="列出任务目录下（或子目录内）的文件名。",
            permission="fs",
            timeout=10.0,
            max_calls=50,
            handler=lambda **kw: _list_dir(workspace_root=root, **kw),
        ),
    ]