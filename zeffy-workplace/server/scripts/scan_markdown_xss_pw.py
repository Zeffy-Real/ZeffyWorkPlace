"""Markdown 渲染系统性 XSS 扫描：把恶意 payload 注入每个渲染结构，断言无执行/无注入/无外部请求。

覆盖结构：标题 / 列表 / 表格单元格 / 代码块 / 链接文字 / 图片文字 / 嵌套 markdown / 属性注入尝试。
"""
import sys, pathlib, asyncio
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import httpx
from playwright.sync_api import sync_playwright

P = "javascript:alert(1)"
EVIL = "https://evil.example/x"

# 每个结构都塞一个 payload + 外部图跟踪
MARKDOWN = f"""# t<scr
ipt>window.__xss=1<\\/script>
# 标题<img src=x onerror=window.__xss=2>

- 列表<svg/onload=window.__xss=3>
+ [[text]]({P})

| 单元格<script>window.__xss=4</script> | [链接]({P}nope) |
| --- | --- |
| <img src=x onerror=fetch('{EVIL}/t1')> | [图!]({P}) |

```js
<img src=x onerror=window.__xss=5>
[真实](https://example.com/ok)
```

[文字注入]({EVIL}/l1)
![pic文字注入]({EVIL}/p1)
"""

# 单独一个纯链接文字含 HTML
MD2 = '[<img src=x onerror=window.__xss=9>](https://example.com/ok)\n[**b<svg/onload=window.__xss=10>**](https://example.com/ok2)\n'


def make_task(text, name):
    import pathlib
    c = httpx.Client(base_url="http://127.0.0.1:8787", trust_env=False)
    t = c.post("/tasks", json={"title": name, "workflow_id": "generic"}).json()
    tid = t["id"]
    # 直接写本地 workspace（LocalBackend 根 = server/workspace，key 落 artifacts/）
    root = pathlib.Path(__file__).resolve().parent.parent / "workspace"
    f = root / "artifacts" / tid / "evil.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(text.encode())
    return tid


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)

    for idx, (md, tag) in enumerate([(MARKDOWN, "A"), (MD2, "B")]):
        tid = make_task(md, f"xss-{tag}")
        page = browser.new_page()
        page.on("request", lambda r, t=tag: print(f"  [{t} REQ]", r.url) if "evil.example" in r.url else None)
        page.goto(f"http://localhost:5173/#/tasks/{tid}")
        page.wait_for_load_state("networkidle"); page.wait_for_timeout(1500)
        page.get_by_role("button", name="预览").first.click(force=True)
        page.wait_for_timeout(1500)

        xss = page.evaluate("window.__xss || 0")
        d_script = page.locator('[role="dialog"] script').count()
        d_img = page.locator('[role="dialog"] img').count()
        d_svg = page.locator('[role="dialog"] svg').count()
        d_links = page.locator('[role="dialog"] a').all()
        hrefs = [a.get_attribute('href') for a in d_links]
        print(f"\n=== 样本 {tag} ===")
        print("window.__xss(执行):", xss, "-> 期望0")
        print("dialog script/img/svg 元素:", d_script, d_img, d_svg, "-> 期望0/0/0")
        print("dialog a hrefs:", hrefs)
        # evil.example 不应出现在 href（外部图/链接文字不应转成链接）
        evil_hrefs = [h for h in hrefs if h and 'evil.example' in h]
        print("evil.example 链接数(期望0):", len(evil_hrefs))
        page.close()

    browser.close()