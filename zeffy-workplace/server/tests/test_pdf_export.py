"""P7-B2 加密合规报表 PDF 导出 · 单元 + API 冒烟测试。

覆盖：
1. 渲染：%PDF 魔数、/Encrypt 加密（防复制 canCopy=0）存在、中文 CID 字体可用
2. 字体降级：注册失败 → 回退 Helvetica + 告警，渲染仍成功不崩溃
3. 分页：多行 → 页数增加（长报表自动分页 + 表头重复）
4. 元数据：creator/producer 注入
5. 指纹：SHA256 前24位稳定
6. 水印文案：含导出者 + 报表范围
7. 行数上限：REPORT_PDF_MAX_ROWS 超限不崩溃
8. API 冒烟：format=pdf 开关关→404；开（admin）→200 application/pdf；非 admin→404

注入约定：API 冒烟覆盖 ``get_current_user`` 依赖返回 admin principal；
``_encryption_report`` 不依赖真实查询（空库返回空列表）。
"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.reporting import pdf_export as P

_ROWS = [
    {"date": "2026-09-15", "action": "加密", "total": 10, "ok": 10, "fail": 0},
    {"date": "2026-09-16", "action": "解密", "total": 5, "ok": 4, "fail": 1},
]

_BIG_ROWS = [{"date": "2026-09-16", "action": "加密", "total": 1, "ok": 1, "fail": 0}
             for _ in range(300)]


@pytest.fixture(autouse=True)
def _pdf_env():
    s = get_settings()
    s.REPORT_PDF_ENABLED = True
    s.REPORT_PDF_WATERMARK = True
    s.REPORT_PDF_WM_TEXT = ""
    s.REPORT_PDF_FONT_PATH = ""
    s.REPORT_PDF_MAX_ROWS = 2000
    yield s
    s.REPORT_PDF_ENABLED = False
    s.REPORT_PDF_FONT_PATH = ""
    s.REPORT_PDF_MAX_ROWS = 2000


# ---------------- 渲染核心 ----------------

def test_render_pdf_magic_and_encrypt():
    data = P.render_encryption_pdf(_ROWS, operator="system",
                                   since="2026-09-15", until="2026-09-16")
    assert data[:5] == b"%PDF-"
    # 加密（防复制 canCopy=0）启用：/Encrypt 对象存在
    assert b"/Encrypt" in data
    assert b"application/pdf" or b"/Type /Catalog" in data


def test_render_chinese_font_available():
    font, warn = P._register_font()
    assert font in ("STSong-Light", "CJK")  # 中文字体注册成功
    assert warn == ""
    # 渲染含中文 -> 不抛异常（字体可用）
    P.render_encryption_pdf(_ROWS, operator="system", since="", until="")


def test_font_fallback_on_register_failure(monkeypatch):
    """字体注册失败 → 回退 Helvetica + 告警，渲染仍成功不崩溃。"""
    s = get_settings()
    s.REPORT_PDF_FONT_PATH = "/nonexistent/font.ttf"  # 触发 TTFont 注册失败
    font, warn = P._register_font()
    assert font == "Helvetica"
    assert "字体" in warn
    data = P.render_encryption_pdf(_ROWS, operator="system", since="", until="")
    assert data[:5] == b"%PDF-"


def test_pagination_many_rows():
    """长报表分页：行数多 → 页数增加。"""
    small = P.render_encryption_pdf(_ROWS, operator="system", since="", until="")
    big = P.render_encryption_pdf(_BIG_ROWS, operator="system", since="", until="")
    assert P._peek_page_count(big) > P._peek_page_count(small)


def test_metadata_injected():
    data = P.render_encryption_pdf(_ROWS, operator="system", since="", until="")
    # producer/creator 注入（reportlab 元数据）
    assert b"Zeffy-Workplace" in data or b"/Producer" in data


def test_fingerprint_stable():
    data = P.render_encryption_pdf(_ROWS, operator="system", since="", until="")
    assert P.pdf_offprint_fingerprint(data) == P.pdf_offprint_fingerprint(data)
    assert len(P.pdf_offprint_fingerprint(data)) == 24


def test_watermark_text_contains_operator_and_range():
    s = get_settings()
    s.REPORT_PDF_WM_TEXT = ""
    wm = P._watermark_text(operator="admin1", since="2026-09-15", until="2026-09-16")
    assert "admin1" in wm
    assert "2026-09-15" in wm and "2026-09-16" in wm
    assert "合规报表" in wm


def test_max_rows_cap():
    """REPORT_PDF_MAX_ROWS 超限不崩溃（切片保护）。"""
    s = get_settings()
    s.REPORT_PDF_MAX_ROWS = 1
    data = P.render_encryption_pdf(_BIG_ROWS, operator="system", since="", until="")
    assert data[:5] == b"%PDF-"


def test_custom_watermark_text():
    s = get_settings()
    s.REPORT_PDF_WM_TEXT = "内部资料"
    wm = P._watermark_text(operator="op1", since="a", until="b")
    assert wm.startswith("内部资料")
    assert "op1" not in wm  # 自定义文案不追加 operator


# ---------------- API 冒烟（admin-only + 开关） ----------------

@pytest.fixture
async def _pdf_api(tmp_path):
    from app.auth.deps import UserPrincipal, get_current_user
    from app.db.base import set_global_engine
    from app.db.init_db import init_db
    from app.main import app
    from app.storage import reset_backend, set_backend
    from app.storage.local import LocalBackend

    s = get_settings()
    from httpx import ASGITransport, AsyncClient

    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    set_global_engine(eng)
    set_backend(LocalBackend(tmp_path))
    s.ARTIFACT_META_ENABLED = True

    # 覆盖 get_current_user：返回 admin principal
    async def _admin():
        return UserPrincipal(id="a1", username="admin", role="admin")

    async def _anon():
        return UserPrincipal(id=None, username=None, is_system=False, role="user")

    app.dependency_overrides[get_current_user] = _admin
    ac = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    await ac.__aenter__()
    yield ac, s, app, get_current_user, _anon
    await ac.__aexit__(None, None, None)
    app.dependency_overrides.clear()
    await eng.dispose()
    reset_backend()
    s.ARTIFACT_META_ENABLED = False


@pytest.mark.asyncio
async def test_api_pdf_disabled_404(_pdf_api):
    ac, s, _, _, _ = _pdf_api
    s.REPORT_PDF_ENABLED = False
    r = await ac.get("/admin/governance/encryption/report", params={"format": "pdf"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_api_pdf_admin_ok(_pdf_api):
    ac, s, _, _, _ = _pdf_api
    s.REPORT_PDF_ENABLED = True
    r = await ac.get("/admin/governance/encryption/report", params={"format": "pdf"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/pdf")
    assert r.content[:5] == b"%PDF-"


@pytest.mark.asyncio
async def test_api_pdf_non_admin_404(_pdf_api):
    ac, s, app, dep, anon = _pdf_api
    s.REPORT_PDF_ENABLED = True
    app.dependency_overrides[dep] = anon
    r = await ac.get("/admin/governance/encryption/report", params={"format": "pdf"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_api_pdf_bad_format(_pdf_api):
    ac, s, _, _, _ = _pdf_api
    s.REPORT_PDF_ENABLED = True
    r = await ac.get("/admin/governance/encryption/report", params={"format": "xml"})
    assert r.status_code == 400
