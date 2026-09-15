"""Playwright 内存泄漏验证：反复开关图片/PDF 预览，断言每 open 都 revokeObjectURL、活跃 blob: URL 不增长。

用真实 PNG/PDF 字节（对比魔数，走 objectURL 渲染路径）。
"""
import sys, pathlib, asyncio
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import httpx
from playwright.sync_api import sync_playwright

# 最小合法 PNG（1x1 红点）
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c4944415408d763f8cfc0f01f00050001ff5b02e92f0000000049454e44ae426082")
# 最小 PDF（%PDF-）
PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF"

BASE = "http://127.0.0.1:8787"
c = httpx.Client(base_url=BASE, trust_env=False)
t = c.post("/tasks", json={"title": "mem leak", "workflow_id": "generic"}).json()
TID = t["id"]
from app.storage import get_backend as gb
async def put():
    b = gb()
    await b.put(f"artifacts/{TID}/dot.png", PNG, mode="overwrite")
    await b.put(f"artifacts/{TID}/doc.pdf", PDF, mode="overwrite")
asyncio.run(put())
print("task:", TID)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    # 计数 revokeObjectURL 与 createObjectURL
    page.add_init_script("""
      window.__objMetrics = {};
      const origC = URL.createObjectURL, origR = URL.revokeObjectURL;
      window.__objMetrics.created = 0; window.__objMetrics.revoked = 0;
      URL.createObjectURL = function(b){ window.__objMetrics.created++; return origC.call(this, b); };
      URL.revokeObjectURL = function(u){ window.__objMetrics.revoked++; return origR.call(this, u); };
    """)
    page.goto("http://localhost:5173/#/tasks/" + TID)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1500)

    # 打开 png 预览 → 等渲染 → 关闭；重复 3 次
    buttons = page.get_by_role("button", name="预览")
    for i in range(3):
        buttons.first.click(); page.wait_for_timeout(1200)
        # 确认图片渲染
        page.screenshot(path=f"/tmp/leak_png_{i}.png")
        # 关闭
        page.get_by_role("button", name="关闭").click(); page.wait_for_timeout(400)

    m = page.evaluate("window.__objMetrics")
    print("\n=== PNG 3次开关 ===")
    print("created:", m["created"], "revoked:", m["revoked"])
    print("泄漏差(created-revoked):", m["created"] - m["revoked"], "-> 应 ≤3（StrictMode prev + 当前）")

    # PDF 预览 2 次
    for i in range(2):
        buttons.nth(1).click(); page.wait_for_timeout(1200)
        page.get_by_role("button", name="关闭").click(); page.wait_for_timeout(400)
    m2 = page.evaluate("window.__objMetrics")
    print("\n=== +PDF 2次 ===")
    print("created:", m2["created"], "revoked:", m2["revoked"])
    print("累计泄漏(created-revoked):", m2["created"] - m2["revoked"])

    # 判断：revoked ≥ created 的 多次开关后最终应接近相等（除 StrictMode 首轮）
    ok = (m2["revoked"] >= m2["created"] - 1)
    print("\nrevoked >= created-1:", ok, "(允许 StrictMode 首轮+1)")
    print("结论:", "无泄漏 ✅" if ok else "存在泄漏 ❌")
    browser.close()