"""P7 真机全功能流（Playwright，账号 demo@zeffy.local / demo123456）。
覆盖：登录态 → 治理面板 → 新建任务(真实 API) → 详情/节点 → 审批(UI) → 完成 → 产物。
全程收集浏览器 console/pageerror，作为「确保不会出错」的证据。
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import time

import httpx
import websockets
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8787"
WS = "ws://127.0.0.1:8787/ws"
FE = "http://localhost:5173"
TOKEN_KEY = "zw_token"
USER = {"email": "demo@zeffy.local", "password": "demo123456"}
SHOT = pathlib.Path(__file__).resolve().parent / "_full_flow_shots"
SHOT.mkdir(exist_ok=True)

results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")
    results.append((name, bool(cond), extra))


def submit_via_ws(token: str, prompt: str, timeout: float = 60.0) -> str | None:
    """经 WS 鉴权帧 + user_message 入队（真实跑任务路径），返回 task_db_id。"""
    import threading

    out: dict = {}

    def _run() -> None:
        async def loop() -> None:
            task_id = None
            async with websockets.connect(WS, max_size=2**22) as ws:
                await ws.send(json.dumps({"kind": "auth", "payload": {"token": token}}))
                deadline = asyncio.get_event_loop().time() + timeout
                sent = False
                while asyncio.get_event_loop().time() < deadline:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                    except asyncio.TimeoutError:
                        break
                    msg = json.loads(raw)
                    kind = msg.get("kind", "")
                    p = msg.get("payload") or {}
                    # 认证成功后提交任务
                    if not sent and (p.get("auth_ok") is True or p.get("done") == "auth"
                                     or kind == "system_notify"):
                        await ws.send(json.dumps(
                            {"kind": "user_message", "payload": {"text": prompt}}))
                        sent = True
                    if kind == "task_update" and p.get("event") == "enqueued":
                        task_id = p.get("task_db_id") or p.get("task_id")
                        break
            out["task_id"] = task_id

        asyncio.run(loop())

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=timeout + 10)
    return out.get("task_id")


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        console_issues: list[str] = []
        page_errors: list[str] = []
        page.on("console", lambda m: console_issues.append(f"{m.type}: {m.text}")
                if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        token: str = ""

        # 1) 登录页（AUTH on：无凭证异步跳登录；等待登录表单渲染）
        page.goto(FE, wait_until="domcontentloaded")
        page.wait_for_selector('input[type="email"]', timeout=15000)
        check("无凭证跳转登录页", "#/login" in page.url, page.url)
        page.screenshot(path=str(SHOT / "01_login.png"))

        page.fill('input[type="email"]', USER["email"])
        page.fill('input[type="password"]', USER["password"])
        page.get_by_role("button", name="登录", exact=True).click()
        page.wait_for_selector("text=我的任务", timeout=20000)
        check("UI 登录成功进入任务列表", "#/" in page.url or "#/" in page.evaluate("location.hash"),
              page.url)
        page.screenshot(path=str(SHOT / "02_list.png"))

        token = page.evaluate(f"() => localStorage.getItem('{TOKEN_KEY}') || ''")
        check("已写入登录 token", bool(token), f"len={len(token)}")

        # 2) 治理面板（登录后应为可见）
        page.wait_for_timeout(1200)
        gov_visible = "产物治理" in page.content()
        check("治理面板可见(登录态)", gov_visible)

        # 3) WS 鉴权入队建任务（真实跑任务路径）
        task_id = submit_via_ws(token, "请用 Python 编写一个斐波那契生成器代码，保存到文件并写运行说明")
        check("WS 提交任务并入队", bool(task_id), f"task={task_id}")

        # 4) 列表出现新任务 → 进入详情（真实 UI 渲染）
        headers = {"Authorization": f"Bearer {token}"}
        if task_id:
            page.goto(FE, wait_until="networkidle")
            page.wait_for_timeout(1000)
            check("列表出现新任务", task_id[:8] in page.content(), f"id={task_id[:8]}")
            page.goto(FE + f"/#/tasks/{task_id}", wait_until="networkidle")
            page.wait_for_timeout(1500)
            check("进入任务详情", f"/tasks/" in page.url, page.url)
            page.screenshot(path=str(SHOT / "03_detail.png"))

        # 5) 轮询节点至 审批 blocked / 失败，然后 UI 审批通过
        approved = False
        if task_id:
            with httpx.Client(base_url=BASE, timeout=30, trust_env=False) as c:
                final = "running"
                for _ in range(150):  # 150×2s=5min，容纳真实 LLM 长任务
                    time.sleep(2)
                    r = c.get(f"/tasks/{task_id}/nodes", headers=headers)
                    if r.status_code != 200:
                        continue
                    items = r.json()["items"]
                    sts = {n["status"] for n in items}
                    if "failed" in sts:
                        final = "failed"
                        break
                    blocked = [n for n in items if n["status"] == "blocked"]
                    if blocked and not approved:
                        page.goto(FE + f"/#/tasks/{task_id}", wait_until="networkidle")
                        page.wait_for_timeout(1200)
                        field = page.get_by_text("通过", exact=True).first
                        if field.count() > 0:
                            field.click()
                            approved = True
                            print(f"  -> UI 审批通过 (blocked: {blocked[0]['node_name']})")
                            page.screenshot(path=str(SHOT / "04_approved.png"))
                        else:
                            # 卡片未渲染，尝试刷新后再找
                            page.reload(wait_until="networkidle")
                            page.wait_for_timeout(800)
                            if page.get_by_text("通过", exact=True).count() > 0:
                                page.get_by_text("通过", exact=True).first.click()
                                approved = True
                                print("  -> UI 审批通过（刷新后）")
                        continue
                    if all(n["status"] == "done" for n in items) and items:
                        final = "done"
                        print("  节点:", {n["node_name"]: n["status"] for n in items})
                        break
                check("任务跑通至完成(含 UI 审批)", final == "done" and approved,
                      f"final={final} approved={approved}")

        # 6) 产物区（预览/下载按钮存在即验证渲染）
        if task_id:
            page.goto(FE + f"/#/tasks/{task_id}", wait_until="networkidle")
            page.wait_for_timeout(1500)
            page.screenshot(path=str(SHOT / "05_artifacts.png"))
            has_dl = page.get_by_text("下载", exact=True).count() > 0
            check("产物区渲染(下载按钮)", has_dl)

        page.screenshot(path=str(SHOT / "06_final.png"))

        check("浏览器零 pageerror", len(page_errors) == 0, f"count={len(page_errors)}")
        print("---- console error/warning ----")
        for e in console_issues:
            print("  ", e[:200])
        if page_errors:
            print("---- pageerror ----")
            for e in page_errors:
                print("  ", e[:200])
        browser.close()

    ok = all(ok for _, ok, _ in results)
    print("\n结论:", "全部通过 ✅" if ok else "存在失败 ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())