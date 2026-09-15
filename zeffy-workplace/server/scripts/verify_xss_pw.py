"""Playwright XSS/类型欺骗验证：注入恶意 markdown，断言浏览器 DOM 无脚本执行、无恶意链接、无外部请求。

恶意样本（.md为 .md 扩展名伪装）：<script>alert(1)</script>、javascript: 链接、data: 链接、外部图片、img onerror。
"""
import sys, pathlib, re
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from playwright.sync_api import sync_playwright

MALICIOUS = """# 标题
<script>window.__xss=1</script>
<img src=x onerror=window.__xss=2>
![外部图](https://evil.example/track.png)
[坏链](javascript:alert(1))
[data链](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)
[好链](https://example.com/ok)
""" + "https://evil.example/bare\n"

# 交给后端存储
import httpx
BASE = "http://127.0.0.1:8787"
c = httpx.Client(base_url=BASE, trust_env=False)
t = c.post("/tasks", json={"title": "xss test", "workflow_id": "generic"}).json()
TID = t["id"]
from app.storage import get_backend
import asyncio
asyncio.run(get_backend().put(f"artifacts/{TID}/evil.md", MALICIOUS.encode(), mode="overwrite"))
print("task:", TID)

# 浏览器验证
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto("http://localhost:5173/#/tasks/" + TID)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1500)
    page.get_by_role("button", name="预览").first.click()
    page.wait_for_timeout(2000)

    # 检查是否执行了脚本
    global_xss = page.evaluate("window.__xss || 0")
    print("\n=== 结果 ===")
    print("window.__xss (脚本执行):", global_xss, "-> 应为 0")

    # 检查 DOM 是否有 script/img 注入
    scripts = page.locator('script[src], script:not([src])').count()
    print("DOM script 标签数:", scripts, "-> 应为 0（文本不渲染本条）")

    # DUMP 预览容器的 innerHTML，确认 <script> 是元素还是转义文本
    import re as _re
    preview_html = page.locator('[role="dialog"]').inner_html()
    raw_script_elem = _re.search(r'<script[^>]*>', preview_html)
    esc_script = preview_html.count('&lt;script&gt;')
    print("dialog 内 <script> 真实元素:", bool(raw_script_elem))
    print("dialog 内 转义文本 &lt;script&gt; 次数:", esc_script)
    print("dialog HTML 片段:", preview_html[:300])

    # 检查页面内 img（恶意外部图不应加载）
    imgs = page.locator('img').count()
    print("DOM img 标签数:", imgs, "-> 应为 0（外部图纯文本化）")

    # 精确限定 dialog 作用域内检查
    dialog = page.locator('[role="dialog"]')
    d_scripts = dialog.locator('script').count()
    d_imgs = dialog.locator('img').count()
    d_links = dialog.locator('a').all()
    d_hrefs = [a.get_attribute('href') for a in d_links]
    print("dialog 内 script 元素(应为0):", d_scripts)
    print("dialog 内 img 元素(应为0):", d_imgs)
    print("dialog 内链接 hrefs:", d_hrefs)

    # 真实链接
    links = page.locator('a').all()
    hrefs = [a.get_attribute('href') for a in links]
    print("全页面链接 hrefs:", hrefs)
    evil_href = [h for h in hrefs if h and not (h.startswith('http') or h.startswith('#') or h.startswith('/'))]
    print("非 http 链接(应为空):", evil_href)

    # 外部请求：拦截
    external_req = []
    page.on("request", lambda r: external_req.append(r.url) if "evil.example" in r.url else None)
    page.reload(); page.wait_for_timeout(1500)
    page.get_by_role("button", name="预览").first.click(); page.wait_for_timeout(1500)
    print("访问 evil.example 请求数(应为0):", sum("evil.example" in u for u in external_req))

    # 标记：以 dialog 精确指标为准
    all_ok = (global_xss == 0 and d_scripts == 0 and d_imgs == 0
              and not evil_href and not any("evil.example" in u for u in external_req)
              and d_hrefs == ['https://example.com/ok'])
    print("\n结论:", "全部安全通过 ✅（无脚本执行 / 无 img 注入 / 无外部请求 / 仅白名单外链）"
          if all_ok else f"存在 XSS 风险 ❌ d_hrefs={d_hrefs}")
    browser.close()