"""P7-B2 加密合规报表 PDF 导出 · 渲染器（中文/水印/防复制/分页/元数据）。

设计（Stage0 审查通过）：
- **技术选型**：reportlab（纯 Python、零系统库依赖、CID 中文字体、原生文档加密权限）。
  仅 PDF 导出时 lazy import，关闭时零加载。
- **中文字体**：方案 A —— ``UnicodeCIDFont("STSong-Light")`` 动态引用，零外部字体文件；
  预留 ``REPORT_PDF_FONT_PATH`` 可升级方案 C（外部 TTF 嵌入）。**字体降级**：注册失败
  → 回退默认字体并告警，绝不崩溃。
- **安全红线**：仅消费聚合层统计维度；PDF **加密禁复制文字**（canCopy=0）；每页半透明
  水印 = 导出时间戳 + 导出者 + 报表范围，同步写入审计可互证。
- **可复用**：``render_encryption_pdf(rows, *, operator, since, until)`` 纯函数，后续
  B1 定时归档可直接复用输出 PDF 归档。

兼容锚点：模块级导入零副作用；PDF 功能由 ``REPORT_PDF_ENABLED`` 控制（接口层 404），
本模块不承载开关判断，纯渲染。
"""
from __future__ import annotations

import hashlib
import io
import logging
from datetime import UTC, datetime

from app.config import get_settings

logger = logging.getLogger(__name__)

# 合规统计列头（与 _encryption_report 输出列对应；白名单，不扩展敏感字段）
_HEADERS = ["日期", "操作类型", "总数", "成功", "失败"]


def _register_font() -> tuple[str, str]:
    """注册中文字体：优先配置的外部字体；否则 CID STSong-Light；失败回退默认并告警。

    返回 (正常渲染字体名, 告警文本或 "")。
    """
    s = get_settings()
    try:
        if s.REPORT_PDF_FONT_PATH:
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.ttfonts import TTFont

            pdfmetrics.registerFont(TTFont("CJK", s.REPORT_PDF_FONT_PATH))
            return "CJK", ""
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont

        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        return "STSong-Light", ""
    except Exception as exc:  # noqa: BLE001 字体降级：不崩溃，回退默认字体 + 告警
        logger.warning("中文字体注册失败，回退默认字体（中文可能无法渲染）：%s", exc)
        return "Helvetica", f"字体注册失败: {type(exc).__name__}"


def _watermark_text(*, operator: str, since: str, until: str) -> str:
    """构造水印文案：导出时间戳 + 导出者 + 报表范围。"""
    s = get_settings()
    if s.REPORT_PDF_WM_TEXT:
        base = s.REPORT_PDF_WM_TEXT
    else:
        base = f"Zeffy 合规报表 {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')} {operator}"
    return f"{base} | {since} ~ {until}"


def _make_canvasmaker(fontname: str, watermark: str):
    """返回绑定水印/字体的 Canvas 子类（canvasmaker）——每页叠加水印 + 页脚页码。

    用工厂闭包避免全局可变状态，Platypus 会以 ``Canvas(filename, pagesize=...)`` 实例化。
    """
    from reportlab.pdfgen.canvas import Canvas

    class _WMCanvas(Canvas):
        __page = 0

        def showPage(self):  # noqa: N802 (reportlab 底层调用)
            type(self).__page += 1
            try:
                self.saveState()
                # 对角线半透明水印（用注册中文字体，保证中文水印不方块）
                self.setFont(fontname, 26)
                self.setFillAlpha(0.08)
                self.setFillColorRGB(0.45, 0.45, 0.45)
                w = self._pagesize[0] if self._pagesize else 595
                h = self._pagesize[1] if self._pagesize and len(self._pagesize) > 1 else 842
                self.translate(w / 2, h / 2)
                self.rotate(45)
                for dx in (-230, 0, 230):
                    self.drawCentredString(dx, 0, watermark)
                # 页脚页码
                self.setFont("Helvetica", 8)
                self.setFillAlpha(0.5)
                self.setFillColorRGB(0, 0, 0)
                self.drawRightString(w - 40, 24, f"第 {type(self).__page} 页")
                self.restoreState()
            except Exception:  # noqa: BLE001 水印/页码异常不影响正文
                logger.exception("PDF 水印渲染异常")
            Canvas.showPage(self)

    return _WMCanvas


def render_encryption_pdf(rows: list[dict], *, operator: str, since: str,
                          until: str) -> bytes:
    """把加密合规报表聚合行渲染为受保护 PDF（bytes）。

    :param rows: ``_encryption_report`` 输出（已脱敏统计维度）。
    :param operator: 导出者标识（system/admin id），写入水印与审计互证。
    :param since/until: 报表时间范围（ISO），写入水印。
    :return: PDF 字节（加密禁复制 + 水印 + 分页 + 元数据）。
    :raises RuntimeError: 渲染失败（reportlab 异常），由调用方转 5xx。
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.pdfencrypt import StandardEncryption
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    s = get_settings()
    font, font_warn = _register_font()
    rows = rows[: max(1, int(s.REPORT_PDF_MAX_ROWS))]  # 行数上限，防超大报表阻塞

    buf = io.BytesIO()
    title = "加密合规报表"
    sub = (f"统计范围：{since} ~ {until} · "
           f"生成：{datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}")
    watermark = _watermark_text(operator=operator, since=since, until=until)

    encrypt = StandardEncryption("", operator or "zeffy-admin", strength=128,
                                 canPrint=True, canModify=False,
                                 canCopy=False, canAnnotate=False)
    doc = SimpleDocTemplate(
        buf, pagesize=A4, title=title, creator="Zeffy-Workplace",
        producer="Zeffy-Workplace", subject=f"加密合规报表 {since}~{until}",
        encrypt=encrypt, canvasmaker=_make_canvasmaker(font, watermark))

    h_style = ParagraphStyle("h", fontName=font, fontSize=14, leading=20, alignment=1)
    p_style = ParagraphStyle("sub", fontName=font, fontSize=9, leading=13,
                             textColor=colors.HexColor("#555555"), alignment=1)

    story = [Paragraph(title, h_style), Spacer(1, 6),
             Paragraph(sub, p_style), Spacer(1, 12)]

    # 表头 + 数据行（repeatRows=1 长报表分页时自动重复表头）
    body = [_HEADERS]
    for r in rows:
        body.append([
            str(r.get("date", "")), str(r.get("action", "")),
            str(r.get("total", 0)), str(r.get("ok", 0)), str(r.get("fail", 0)),
        ])
    table = Table(body, repeatRows=1, colWidths=[90, 150, 60, 60, 60])
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef1f5")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c9cfd8")),
        ("ALIGN", (2, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#f7f8fa")]),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(table)
    if font_warn:
        story.append(Paragraph(f"⚠ {font_warn}（字体降级，中文可能显示为方块）", p_style))

    doc.build(story)
    return buf.getvalue()


def pdf_offprint_fingerprint(data: bytes) -> str:
    """水印/输出指纹（SHA256 前24位），供审计留痕互证。"""
    return hashlib.sha256(data).hexdigest()[:24]


def _peek_page_count(data: bytes) -> int:
    """轻量统计页数（统计 ``/Type /Page`` 出现次数），仅供测试断言分页。"""
    return data.count(b"/Type /Page")
