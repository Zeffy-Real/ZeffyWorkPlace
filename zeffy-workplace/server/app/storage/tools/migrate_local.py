"""P5 存量产物迁移脚本：P4 旧 ``task-<id>/`` → 新 key 空间 ``artifacts/{task_id}/{rel}``。

用法：
    python -m app.storage.tools.migrate_local --dry-run          # 预览（不写）
    python -m app.storage.tools.migrate_local                    # 复制迁移
    python -m app.storage.tools.migrate_local --move             # 移动迁移（源删除）
    python -m app.storage.tools.migrate_local --root <新根> --legacy-root <旧根>

安全：逐文件 ``normalize_artifact_key`` 校验（越界跳过并告警）；目标已存在则跳过（不覆盖）。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from app.config import get_settings
from app.storage.base import SecurityError, normalize_artifact_key
from app.storage.local import LocalBackend


def scan_legacy(legacy_root: Path) -> list[tuple[str, str, Path]]:
    """扫描旧根下 ``task-<id>/<rel>`` 文件。返回 (task_id, rel, src)。"""
    out: list[tuple[str, str, Path]] = []
    if not legacy_root.exists():
        return out
    for task_dir in sorted(legacy_root.glob("task-*")):
        if not task_dir.is_dir():
            continue
        task_id = task_dir.name[len("task-"):]
        for src in sorted(task_dir.rglob("*")):
            if not src.is_file():
                continue
            rel = src.relative_to(task_dir).as_posix()
            out.append((task_id, rel, src))
    return out


def run(root: str | Path, legacy_root: str | Path, *, dry_run: bool, move: bool) -> int:
    backend = LocalBackend(root, legacy_root=legacy_root)
    items = scan_legacy(Path(legacy_root))
    moved = skipped = skipped_invalid = 0
    for task_id, rel, src in items:
        try:
            key = normalize_artifact_key(task_id, rel)
        except SecurityError as exc:
            print(f"  ! 跳过越界：{task_id}/{rel} ({exc})")
            skipped_invalid += 1
            continue
        dst = backend._key_to_path(key)
        if dst.exists():
            skipped += 1
            continue
        if dry_run:
            print(f"  -> {src}  =>  {dst}")
            moved += 1
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if move:
            shutil.move(str(src), str(dst))
        else:
            shutil.copy2(str(src), str(dst))
        moved += 1
    print(f"完成：迁移 {moved}，跳过(已存在) {skipped}，跳过(越界) {skipped_invalid}"
          f"{'（dry-run 预览）' if dry_run else ''}")
    return 0 if skipped_invalid == 0 else 2


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    s = get_settings()
    p.add_argument("--root", default=str(s.STORAGE_ROOT or s.WORKSPACE_ROOT),
                   help="新 key 空间根（默认 STORAGE_ROOT/WORKSPACE_ROOT）")
    p.add_argument("--legacy-root", default=None,
                   help="存量根（默认同新根，即 WORKSPACE_ROOT 下 P4 的 task-* 所在）")
    p.add_argument("--dry-run", action="store_true", help="仅预览不写")
    p.add_argument("--move", action="store_true", help="移动（源删除）而非复制")
    args = p.parse_args(argv)
    legacy = Path(args.legacy_root) if args.legacy_root else Path(args.root).resolve()
    return run(args.root, legacy, dry_run=args.dry_run, move=args.move)


if __name__ == "__main__":
    sys.exit(main())
