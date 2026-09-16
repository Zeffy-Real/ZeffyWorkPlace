"""P7-D1 插件市场存储（探索线，独立于产物治理线）。

审查闭环（D1 范围界定 v2）：
- **上架四门禁**：publish 必经 ``manifest.run_upload_gate``（完整性/静态安全扫描/权限声明审核），
  官方 vendor 签名=内置真实摘要（可信）；第三方签名占位（演进为发布者密钥），缺失不阻断但标记未签名。
- **黑名单机制**：发现恶意插件 ``blacklist``（按插件 id 或 publisher），拉黑后立即失能且不可安装，
  记录 reason 供审计；``unblacklist`` 解除。
- **生命周期**：publish → install(校验未拉黑) → enable/disable → uninstall / delist / blacklist。
- **执行集成**：``sync_registry`` 把 installed+enabled 插件能力经 ``loader.install_plugin``
  注册进 ``ToolRegistry``（计次/超时/异常熔断复用），供 Agent 调用。
- **资源隔离**：存储为独立 JSON（PLUGINS_STORE_DIR），不触碰 storage/governance/crypto/
  内部模块与 Paychain DB。常驻进程内单例；提供模块级 ``get_market()`` 惰性构造。
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.plugins import loader, manifest, security

logger = logging.getLogger(__name__)


class MarketError(Exception):
    """插件市场业务异常。"""


class PluginNotFound(MarketError):
    """插件不存在。"""


class PluginForbidden(MarketError):
    """插件被禁止（拉黑/非 active 状态/未签名高危）。"""


class GateFailed(MarketError):
    """上架门禁未通过。"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _digest(source: str) -> str:
    """签名占位/内容摘要：官方插件据源码摘要自证完整性；真实发布者密钥签名留演进。"""
    return f"sig:{hashlib.sha256(source.encode('utf-8')).hexdigest()[:16]}"


class PluginMarket:
    """插件市场存储：内存态 + JSON 落盘。所有 mutation 原子落盘。"""

    def __init__(self, store_dir: str | Path = "") -> None:
        s = get_settings()
        self._dir = Path(store_dir or s.PLUGINS_STORE_DIR)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._file = self._dir / "market.json"
        self._plugins: dict[str, dict[str, Any]] = {}
        self._blacklist: dict[str, str] = {}
        self._load()

    # ---- 持久化 ----
    def _load(self) -> None:
        try:
            with self._file.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._plugins = data.get("plugins", {})
            self._blacklist = data.get("blacklist", {})
        except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
            logger.warning("插件市场加载失败(%s)，以空态启动：%s", self._file, exc)
            self._plugins = {}
            self._blacklist = {}

    def _save(self) -> None:
        tmp = self._file.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"plugins": self._plugins, "blacklist": self._blacklist},
            ensure_ascii=False, indent=2,
        ), encoding="utf-8")
        tmp.replace(self._file)

    # ---- 查询 ----
    def get(self, plugin_id: str, *, include_source: bool = False) -> dict[str, Any]:
        try:
            rec = self._plugins[plugin_id]
        except KeyError:
            raise PluginNotFound(f"插件不存在：{plugin_id}") from None
        rec = dict(rec)
        if not include_source:
            rec.pop("source", None)
        return rec

    def list_all(self) -> list[dict[str, Any]]:
        return [self.get(k) for k in self._plugins]

    def list_installed(self) -> list[dict[str, Any]]:
        return [self.get(k) for k in self._plugins if self._plugins[k].get("installed")]

    def list_available(self) -> list[dict[str, Any]]:
        """可浏览/可安装 = active 且未被拉黑（自身或发布者）。"""
        out = []
        for pid, rec in self._plugins.items():
            if rec.get("status") != "active":
                continue
            if self._is_blacklisted(pid, rec.get("author", "")):
                continue
            out.append(self.get(pid))
        return out

    def _is_blacklisted(self, plugin_id: str, author: str) -> bool:
        return plugin_id in self._blacklist or author in self._blacklist

    # ---- 上架 / 下架 ----
    def publish(self, m: dict, source: str, publisher: str = "", *, force: bool = False) -> dict[str, Any]:
        """上架：四门禁（完整性/签名占位/静态扫描/权限声明）。强制覆盖用 force。"""
        ok, err = manifest.run_upload_gate(m, source)
        if not ok:
            raise GateFailed(err)
        pid = str(m["id"])
        vendor = str(m.get("vendor", "third"))
        exists = pid in self._plugins
        if exists and not force:
            raise MarketError(f"插件 {pid} 已上架，需 force 覆盖或用新版本号")
        rec = {
            "id": pid,
            "version": str(m["version"]),
            "author": str(m["author"]),
            "vendor": vendor,
            "manifest": m,
            "source": source,
            "signature": _digest(source),
            "signed": vendor == "official",
            "status": "active",
            "installed": False,
            "enabled": False,
            "publisher": publisher,
            "created_at": _now(),
            "installed_at": None,
        }
        self._plugins[pid] = rec
        self._save()
        logger.info("插件上架 plugin=%s ver=%s vendor=%s", pid, rec["version"], vendor)
        return self.get(pid)

    def delist(self, plugin_id: str) -> dict[str, Any]:
        rec = self._plugins[plugin_id]  # 原地变更，保留 source
        rec["status"] = "delisted"
        rec["installed"] = False
        rec["enabled"] = False
        self._save()
        return self.get(plugin_id)

    # ---- 黑名单 ----
    def blacklist(self, key: str, reason: str = "", by: str = "") -> None:
        if not key:
            raise MarketError("黑名单 key 不能为空（插件 id 或 publisher）")
        self._blacklist[key] = f"{by}:{reason}" if by else reason
        # 即时失能：命中该 key 的插件全下架
        for pid, rec in list(self._plugins.items()):
            if pid == key or rec.get("author") == key:
                rec["status"] = "blacklisted"
                rec["installed"] = False
                rec["enabled"] = False
        self._save()
        logger.warning("插件拉黑 key=%s reason=%s", key, reason)

    def unblacklist(self, key: str) -> bool:
        if key not in self._blacklist:
            return False
        self._blacklist.pop(key, None)
        for pid, rec in self._plugins.items():
            if rec.get("status") == "blacklisted" and (pid == key or rec.get("author") == key):
                rec["status"] = "active"
        self._save()
        return True

    # ---- 安装 / 启停 ----
    def install(self, plugin_id: str) -> dict[str, Any]:
        rec = self._plugins[plugin_id]
        if rec.get("status") != "active":
            raise PluginForbidden(f"插件 {plugin_id} 不在可安装状态（status={rec.get('status')}）")
        if self._is_blacklisted(pid := rec["id"], rec.get("author", "")):
            raise PluginForbidden(f"插件或其发布者已被拉黑，禁止安装：{pid}")
        rec["installed"] = True
        rec["enabled"] = True
        rec["installed_at"] = _now()
        self._save()
        return self.get(plugin_id)

    def uninstall(self, plugin_id: str) -> dict[str, Any]:
        rec = self._plugins[plugin_id]
        rec["installed"] = False
        rec["enabled"] = False
        rec["installed_at"] = None
        self._save()
        return self.get(plugin_id)

    def set_enabled(self, plugin_id: str, enabled: bool) -> dict[str, Any]:
        rec = self._plugins[plugin_id]
        if not rec.get("installed"):
            raise MarketError(f"插件 {plugin_id} 未安装，无法启停")
        rec["enabled"] = enabled
        self._save()
        return self.get(plugin_id)

    # ---- 执行集成 ----
    def load_caps(self, plugin_id: str) -> dict[str, Any]:
        """在受限作用域加载插件能力（上架时已过静态门禁，此处再拦一次）。"""
        rec = self.get(plugin_id, include_source=True)
        return loader.load_plugin(rec["source"])

    def sync_registry(self, registry) -> list[str]:
        """把 installed+enabled 的插件能力注册进 ToolRegistry，返回注册的工具名列表。

        零已安装→返回空，不触碰 registry（默认关零漂移）。
        """
        out: list[str] = []
        for pid, rec in self._plugins.items():
            if not (rec.get("installed") and rec.get("enabled")):
                continue
            caps = loader.load_plugin(rec["source"])
            res = loader.install_plugin(
                registry, plugin_id=pid, manifest=rec["manifest"], capabilities=caps
            )
            out.extend(res["caps"])
        return out


_market: PluginMarket | None = None


def get_market() -> PluginMarket:
    """进程内单例（惰性构造，存储于配置 PLUGINS_STORE_DIR）。测试可直接实例化独立 PluginMarket。"""
    global _market
    if _market is None:
        _market = PluginMarket()
    return _market


def sync_enabled_plugins(registry) -> list[str]:
    """执行集成入口：总闸关闭直接零漂移返回空；否则把 installed+enabled 插件注册进 registry。

    供运行时（WS registry 构建 / ARQ worker）在构造宿主 ToolRegistry 后调用。
    """
    s = get_settings()
    if not (s.PLUGINS_ENABLED and s.ARTIFACT_META_ENABLED):
        return []
    try:
        return get_market().sync_registry(registry)
    except security.PluginError:
        logger.exception("插件执行集成失败（熔断，不影响主线）：")
        return []
