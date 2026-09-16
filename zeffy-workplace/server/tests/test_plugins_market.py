"""P7-D1 插件市场 · 单元+API 测试。

覆盖：
1. 上架四门禁（GateFailed：静态扫描命中/权限声明缺失）+ force 覆盖
2. 生命周期 publish→install→enable/disable→uninstall→delist
3. 黑名单：自身/发布者拉黑即时失能、禁装、解黑恢复
4. sync_registry 注册 installed+enabled 插件进 ToolRegistry；删停后不再注册
5. sync_enabled_plugins：总闸关闭零漂移返回空
6. API：PLUGINS_ENABLED 关→404（总闸短路）；浏览/安装可用；admin 端 ENABLE_ADMIN 关→404
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

import app.api.plugins as api_plugins
from app.config import get_settings
from app.main import app as fastapi_app
from app.plugins.market import GateFailed, PluginForbidden, PluginMarket
from app.tools.registry import ToolRegistry


def _manifest(**kw):
    m = {
        "id": "hello",
        "version": "1.0.0",
        "capabilities": ["greet"],
        "permissions": {"greet": "read"},
        "author": "alice",
        "vendor": "third",
    }
    m.update(kw)
    return m


GOOD_SRC = 'def greet(who):\n    return {"msg": "hi " + str(who)}\nCAPABILITIES={"greet": greet}\n'
BAD_SRC = "import app.storage\ndef greet():\n    return 1\nCAPABILITIES={'greet': greet}\n"


@pytest.fixture
def market(tmp_path):
    return PluginMarket(str(tmp_path / "plugins"))


# ---- 上架门禁 ----

def test_publish_ok(market):
    rec = market.publish(_manifest(), GOOD_SRC)
    assert rec["status"] == "active" and rec["installed"] is False and rec["signed"] is False


def test_publish_reject_static_scan(market):
    with pytest.raises(GateFailed):
        market.publish(_manifest(), BAD_SRC)


def test_publish_duplicate_requires_force(market):
    market.publish(_manifest(), GOOD_SRC)
    from app.plugins.market import MarketError

    with pytest.raises(MarketError):
        market.publish(_manifest(), GOOD_SRC)
    market.publish(_manifest(), GOOD_SRC, force=True)  # force 覆盖


def test_publish_official_signed(market):
    rec = market.publish(_manifest(vendor="official"), GOOD_SRC)
    assert rec["signed"] is True


# ---- 生命周期 ----

def test_install_lifecycle(market):
    market.publish(_manifest(), GOOD_SRC)
    rec = market.install("hello")
    assert rec["installed"] is True and rec["enabled"] is True and rec["installed_at"]
    rec = market.set_enabled("hello", False)
    assert rec["enabled"] is False
    rec = market.uninstall("hello")
    assert rec["installed"] is False and rec["enabled"] is False


def test_delist_not_installable(market):
    market.publish(_manifest(), GOOD_SRC)
    market.delist("hello")
    with pytest.raises(PluginForbidden):
        market.install("hello")


def test_available_excludes_delisted(market):
    market.publish(_manifest(), GOOD_SRC)
    market.delist("hello")
    assert market.list_available() == []


# ---- 黑名单 ----

def test_blacklist_plugin_blocks_install(market):
    market.publish(_manifest(), GOOD_SRC)
    market.blacklist("hello", reason="malware")
    assert market.get("hello")["status"] == "blacklisted"
    with pytest.raises(PluginForbidden):
        market.install("hello")
    assert market.list_available() == []


def test_blacklist_publisher_disables_all(market):
    market.publish(_manifest(), GOOD_SRC)
    market.publish(_manifest(id="hello2"), GOOD_SRC)
    market.blacklist("alice", reason="bad author")
    assert market.get("hello")["status"] == "blacklisted"
    assert market.get("hello2")["status"] == "blacklisted"


def test_unblacklist_restores(market):
    market.publish(_manifest(), GOOD_SRC)
    market.blacklist("hello", reason="x")
    assert market.unblacklist("hello") is True
    assert market.get("hello")["status"] == "active"


# ---- 执行集成 ----

@pytest.mark.asyncio
async def test_sync_registry_registers_enabled(market):
    market.publish(_manifest(), GOOD_SRC)
    market.install("hello")
    reg = ToolRegistry()
    names = market.sync_registry(reg)
    assert "plugin:hello:greet" in names
    spec = reg.get("plugin:hello:greet")
    assert spec.permission == "ro"
    res = await reg.run("plugin:hello:greet", who="bob")
    assert res.ok and res.data["msg"] == "hi bob"


def test_sync_registry_respects_enabled(market):
    market.publish(_manifest(), GOOD_SRC)
    market.install("hello")
    market.set_enabled("hello", False)
    reg = ToolRegistry()
    assert market.sync_registry(reg) == []


def test_sync_enabled_plugins_total_gate_off(market, tmp_path, monkeypatch):
    from app.plugins.market import sync_enabled_plugins

    market.publish(_manifest(), GOOD_SRC)
    market.install("hello")
    monkeypatch.setattr(api_plugins, "get_market", lambda: market)
    s = get_settings()
    monkeypatch.setattr(s, "PLUGINS_ENABLED", False)
    monkeypatch.setattr(s, "ARTIFACT_META_ENABLED", False)
    assert sync_enabled_plugins(ToolRegistry()) == []


# ---- API ----

@pytest.fixture
def api_market(tmp_path):
    m = PluginMarket(str(tmp_path / "api-plugins"))
    m.publish(_manifest(), GOOD_SRC)
    return m


@pytest.fixture
async def ac(tmp_path):
    ac = AsyncClient(transport=ASGITransport(app=fastapi_app),
                     base_url="http://test")
    await ac.__aenter__()
    yield ac
    await ac.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_api_gate_off_404(ac, api_market, monkeypatch):
    monkeypatch.setattr(api_plugins, "get_market", lambda: api_market)
    s = get_settings()
    monkeypatch.setattr(s, "PLUGINS_ENABLED", False)
    monkeypatch.setattr(s, "ARTIFACT_META_ENABLED", True)
    r = await ac.get("/plugins/market")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_api_browse_and_install(ac, api_market, monkeypatch):
    monkeypatch.setattr(api_plugins, "get_market", lambda: api_market)
    s = get_settings()
    monkeypatch.setattr(s, "PLUGINS_ENABLED", True)
    monkeypatch.setattr(s, "ARTIFACT_META_ENABLED", True)
    monkeypatch.setattr(s, "ENABLE_ADMIN", False)
    s.AUTH_ENABLED = False

    r = await ac.get("/plugins/market")
    assert r.status_code == 200 and len(r.json()["plugins"]) == 1

    r = await ac.post("/plugins/hello/install")
    assert r.status_code == 200 and r.json()["installed"] is True

    r = await ac.get("/plugins/installed")
    assert r.json()["plugins"][0]["id"] == "hello"


@pytest.mark.asyncio
async def test_api_admin_404_when_disabled(ac, api_market, monkeypatch):
    monkeypatch.setattr(api_plugins, "get_market", lambda: api_market)
    s = get_settings()
    monkeypatch.setattr(s, "PLUGINS_ENABLED", True)
    monkeypatch.setattr(s, "ARTIFACT_META_ENABLED", True)
    monkeypatch.setattr(s, "ENABLE_ADMIN", False)  # admin 端未启用 → 404
    s.AUTH_ENABLED = False
    r = await ac.post("/plugins/admin/publish",
                      json={"manifest": _manifest(), "source": GOOD_SRC})
    assert r.status_code == 404
