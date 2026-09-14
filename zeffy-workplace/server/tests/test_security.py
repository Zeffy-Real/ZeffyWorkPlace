"""P0-3 安全函数测试：safe_resolve_workspace_path 防目录穿越。"""

import pytest

from app.utils.security import WorkspacePathError, safe_resolve_workspace_path


@pytest.fixture
def root(tmp_path):
    return tmp_path


class TestSafeResolve:
    def test_normal_path_resolves_under_root(self, root):
        p = safe_resolve_workspace_path("task-abc/readme.md", root=root)
        assert p == (root / "task-abc/readme.md").resolve()
        assert p.is_relative_to(root)

    def test_relative_parent_traversal_rejected(self, root):
        with pytest.raises(WorkspacePathError):
            safe_resolve_workspace_path("../secret.py", root=root)
        with pytest.raises(WorkspacePathError):
            safe_resolve_workspace_path("a/../../secret.py", root=root)

    def test_absolute_path_rejected(self, root):
        with pytest.raises(WorkspacePathError):
            safe_resolve_workspace_path("/etc/passwd", root=root)

    def test_cannonical_left_expansion_rejected(self, root):
        """利用 /c/ 或前缀拼接逃逸（Windows 风格攻击）也应被拒绝。"""
        # 构造一个能解析到 root 上层之外的相对路径（通过重复 .. 穿透）
        escape = "/".join([".."] * 20) + "/outer"
        with pytest.raises(WorkspacePathError):
            safe_resolve_workspace_path(escape, root=root)

    def test_symlink_escape_rejected(self, root):
        """symlink 指向 root 外时，.resolve() 后应被拒绝。

        需要创建 symlink 的系统权限；无权限时跳过（Windows 需管理员/开发者模式）。
        """
        outside = root.parent / "outside-target"
        outside.mkdir(parents=True, exist_ok=True)
        link = root / "evil-link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("当前环境无 symlink 权限，跳过")
        with pytest.raises(WorkspacePathError):
            safe_resolve_workspace_path("evil-link/pwn", root=root)

    def test_root_itself_allowed(self, root):
        p = safe_resolve_workspace_path(".", root=root)
        assert p == root.resolve()
