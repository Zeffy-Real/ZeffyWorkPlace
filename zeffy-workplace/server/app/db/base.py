"""SQLAlchemy async 基础设施：Base、engine、session factory。"""

from pathlib import Path

from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(AsyncAttrs, DeclarativeBase):
    """所有 ORM 模型的基类。"""


def create_engine(url: str | None = None) -> AsyncEngine:
    """创建 async engine（供默认 PG 与测试内存 SQLite 复用）。"""
    url = url or get_settings().DATABASE_URL
    return create_async_engine(url, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


# 默认 engine 与 session factory（懒加载）
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_engine()
    return _engine


def set_global_engine(engine: AsyncEngine) -> None:
    """注入/替换全局 engine（测试用）。"""
    global _engine, _session_factory
    _engine = engine
    _session_factory = None


def get_session_factory() -> async_sessionmaker:
    global _session_factory
    if _session_factory is None:
        _session_factory = create_session_factory(get_engine())
    return _session_factory


def ensure_workspace_root(root: Path | None = None) -> Path:
    """确保 WORKSPACE_ROOT 存在、可读写；越权/不可用时抛异常。P0 启动时调用。"""
    root = root or get_settings().WORKSPACE_ROOT
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise RuntimeError(f"WORKSPACE_ROOT 不是目录：{root}")
    probe = root / ".zw-write-probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:  # pragma: no cover - 权限异常
        raise RuntimeError(f"WORKSPACE_ROOT 不可写：{root} ({exc})") from exc
    return root
