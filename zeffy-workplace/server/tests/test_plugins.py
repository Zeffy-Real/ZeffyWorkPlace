"""P7-D1 插件能力沙箱 · 单元测试。

覆盖：
1. 静态扫描红线命中（内部模块 import / 危险调用）
2. manifest 校验（缺字段/非法 id/版本/能力/权限）
3. 权限三级解析 + check_permission（最小权限）
4. 上架四项门禁（静态扫描/第三方禁 ADMIN/最小权限原则）
5. loader 受限作用域（无法 import 内部包 / 无法 open / 无法 eval）
6. install_plugin 注册受限 ToolSpec 进 registry + 超权限拒绝
"""
from __future__ import annotations

import pytest

from app.plugins import loader, manifest, security
from app.tools.registry import ToolRegistry


def _ok_manifest(**kw):
    m = {
        "id": "my-plugin",
        "version": "1.0.0",
        "capabilities": ["cap_a"],
        "permissions": {"cap_a": "read"},
        "author": "tester",
        "vendor": "third",
    }
    m.update(kw)
    return m


# ---- 静态扫描 ----

def test_static_scan_forbidden_import():
    hits = security.static_scan("import app.storage.governance\ndef x():\n    os.system('rm')\n")
    assert any("app.storage" in h for h in hits)
    assert any("os.system" in h for h in hits)


def test_static_scan_clean():
    assert security.static_scan("def cap():\n    return 42\n") == []


def test_static_scan_danger_calls():
    assert any("eval(" in h for h in security.static_scan("eval('x')"))

def test_minimal_builtins_stripped():
    b = security.minimal_builtins()
    for name in ("__import__", "open", "eval", "exec", "compile"):
        assert name not in b


# ---- manifest ----

def test_manifest_missing_fields():
    ok, err = manifest.validate_manifest({})
    assert not ok and "必填" in err


def test_manifest_ok():
    assert manifest.validate_manifest(_ok_manifest()) == (True, "")


def test_manifest_bad_id():
    ok, _ = manifest.validate_manifest(_ok_manifest(id="bad id!"))
    assert not ok


def test_permission_levels():
    perms = manifest.resolve_permissions({"a": "read", "b": "write", "c": 3, "d": "bogus"})
    assert perms["a"] == manifest.Perm.RO
    assert perms["b"] == manifest.Perm.RW
    assert perms["c"] == manifest.Perm.ADMIN
    assert perms["d"] == manifest.Perm.NONE  # 非法 → 最小权限 NONE


def test_check_permission():
    assert manifest.check_permission(manifest.Perm.RW, manifest.Perm.RO)
    assert not manifest.check_permission(manifest.Perm.RO, manifest.Perm.RW)
    assert not manifest.check_permission(manifest.Perm.NONE, manifest.Perm.RO)


# ---- 上架四项门禁 ----

def test_upload_gate_static_scan_reject():
    src = "import app.storage"
    ok, err = manifest.run_upload_gate(_ok_manifest(), src)
    assert not ok and "静态安全扫描" in err


def test_upload_gate_third_party_no_admin():
    m = _ok_manifest(permissions={"cap_a": "admin"})
    ok, _ = manifest.run_upload_gate(m, "x=1")
    assert not ok  # 第三方禁止 ADMIN


def test_upload_gate_ok():
    ok, err = manifest.run_upload_gate(_ok_manifest(), "def cap_a():\n    return 1\n")
    assert ok


# ---- loader 受限作用域 + 注册 ----

GOOD_SRC = '''
def _do():
    return {"ok": True, "sum": 1+2}

CAPABILITIES = {"cap_a": _do}
'''

BAD_IMPORT_SRC = 'import app.storage.governance\nCAPABILITIES={"cap_a": lambda: 1}'

BAD_OPEN_SRC = '''
def _do():
    f = open("/etc/passwd")
    return f.read()
CAPABILITIES = {"cap_a": _do}
'''

def test_loader_ok_capabilities():
    caps = loader.load_plugin(GOOD_SRC)
    assert "cap_a" in caps and callable(caps["cap_a"])
    assert caps["cap_a"]()["sum"] == 3


def test_loader_rejects_import():
    from app.plugins.security import PluginSecurityError

    with pytest.raises(PluginSecurityError):
        loader.load_plugin(BAD_IMPORT_SRC)


def test_loader_rejects_open():
    from app.plugins.security import PluginSecurityError

    with pytest.raises(PluginSecurityError):
        loader.load_plugin(BAD_OPEN_SRC)


def test_loader_scoped_no_access_to_internal():
    """受限作用域：未注入的任何内部模块均不可达（引用即 NameError/被拦截）。"""
    src = '''def cap():
    try:
        import app
        return {"ok": False}
    except Exception:
        return {"ok": True}
CAPABILITIES={"cap": cap}
'''
    # 静态扫描命中断言（更前置拦截）或受限作用域缺少 __import__ 而失败 → 均安全

    from app.plugins.security import PluginSecurityError

    try:
        caps = loader.load_plugin(src)
        res = caps["cap"]()
        assert res["ok"] is True  # 无法 import → 回退路径
    except PluginSecurityError:
        pass  # 静态扫描拦截（更严格）也视为通过


def test_install_plugin_registers_tool():
    reg = ToolRegistry()
    caps = loader.load_plugin(GOOD_SRC)
    loader.install_plugin(reg, plugin_id="p1", manifest=_ok_manifest(),
                          capabilities=caps)
    spec = reg.get("plugin:p1:cap_a")
    assert spec.permission == "ro" and spec.max_calls >= 1


def test_install_plugin_missing_perm_reject():
    reg = ToolRegistry()
    caps = loader.load_plugin(GOOD_SRC)
    m = _ok_manifest(permissions={"cap_a": "none"})  # NONE 权限 → 拒绝
    from app.plugins.security import PluginSecurityError

    with pytest.raises(PluginSecurityError):
        loader.install_plugin(reg, plugin_id="p1", manifest=m, capabilities=caps)
