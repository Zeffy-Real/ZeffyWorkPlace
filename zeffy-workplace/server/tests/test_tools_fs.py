"""P1-3 工具层测试：fs 白名单 + 幂等不盲目覆盖 + 越界拒绝 + 调用上限。"""

import pytest

from app.tools.fs import make_fs_tools, safe_resolve_workspace_path
from app.tools.registry import ToolError
from app.tools import ToolPermissionError
from app.tools.registry import ToolRegistry


@pytest.fixture
def registry(tmp_path) -> ToolRegistry:
    reg = ToolRegistry()
    for spec in make_fs_tools(tmp_path):
        reg.register(spec)
    return reg


async def test_write_read_list_roundtrip(registry, tmp_path):
    res = await registry.run("fs_write", task_id="t1", path="doc.md",
                             content="# Hello", mode="no_overwrite")
    assert res.ok and res.data is not None
    assert res.data["exists"] is True

    r = await registry.run("fs_read", task_id="t1", path="doc.md")
    assert r.ok and r.data["content"] == "# Hello"

    lst = await registry.run("fs_list", task_id="t1")
    assert lst.ok and lst.data["entries"] == ["doc.md"]


async def test_no_overwrite_refuses_existing(registry):
    await registry.run("fs_write", task_id="t1", path="a.md", content="v1", mode="no_overwrite")
    res = await registry.run("fs_write", task_id="t1", path="a.md", content="v2",
                             mode="no_overwrite")
    assert not res.ok
    assert "拒绝覆盖" in (res.error or "")
    # 内容未被覆盖
    r = await registry.run("fs_read", task_id="t1", path="a.md")
    assert r.data["content"] == "v1"


async def test_new_mode_preserves_existing(registry):
    await registry.run("fs_write", task_id="t1", path="a.md", content="v1", mode="no_overwrite")
    res = await registry.run("fs_write", task_id="t1", path="a.md", content="v2",
                             mode="new", run_id="run-9")
    assert res.ok and res.data["path"] == "a-run-9.md"


async def test_overwrite_work_but_explicit(registry):
    await registry.run("fs_write", task_id="t1", path="a.md", content="v1", mode="no_overwrite")
    res = await registry.run("fs_write", task_id="t1", path="a.md", content="v2", mode="overwrite")
    assert res.ok
    r = await registry.run("fs_read", task_id="t1", path="a.md")
    assert r.data["content"] == "v2"


def test_safe_resolve_rejects_escape(tmp_path):
    with pytest.raises(ToolPermissionError):
        safe_resolve_workspace_path(tmp_path, "../outside.md")


async def test_calling_limit_blocks(registry):
    """超出 max_calls 直接阻断（fs_list 上限 50，写入小循环验证上限机制以 fs_write 0 为例不适用，
    故用未注册工具测试上限由 spec.max_calls 生效逻辑）。"""
    # 验证超限阻断：注册一个 max_calls=2 的探针工具
    reg = ToolRegistry()

    async def probe(**kw):
        return {"ok": 1}

    from app.tools.registry import ToolSpec

    reg.register(ToolSpec(name="probe", description="probe", permission="x",
                          timeout=5.0, max_calls=2, handler=probe))
    assert (await reg.run("probe")).ok
    assert (await reg.run("probe")).ok
    third = await reg.run("probe")
    assert not third.ok
    assert "最大调用次数" in (third.error or "")


async def test_unknown_tool_fails(registry):
    res = await registry.run("nope")
    assert not res.ok
    assert "未知工具" in (res.error or "")


async def test_invalid_mode_rejected(registry):
    res = await registry.run("fs_write", task_id="t1", path="a.md", content="x", mode="bogus")
    assert not res.ok
    assert "非法写入模式" in (res.error or "")