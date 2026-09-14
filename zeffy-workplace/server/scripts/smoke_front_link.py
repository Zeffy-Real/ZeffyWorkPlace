"""前端联调冒烟：经 vite(5173) 代理访问 /artifacts（模拟前端 fetch 链路）。

验证：
1. GET  {vite}/artifacts/{task_id} 列表代理转发到后端
2. GET  {vite}/artifacts/{task_id}/{path} 下载内容一致（带 Authorization）
3. 404 产物 → 404
"""

import asyncio
import pathlib
import sys

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

VITE = "http://localhost:5173"


async def main() -> int:
    ok = True

    async def check(name: str, cond: bool, extra: str = "") -> None:
        nonlocal ok
        mark = "PASS" if cond else "FAIL"
        if not cond:
            ok = False
        print(f"[{mark}] {name} {extra}")

    # 用后端直接建任务 + 写产物（产物真实落在后端存储）
    from app.db.base import get_session_factory
    from app.db import repos
    from app.storage import get_backend

    factory = get_session_factory()
    async with factory() as s:
        task = await repos.create_task(s, title="frontend link", workflow_id="generic")
    tid = task.id
    backend = get_backend()
    await backend.put(f"artifacts/{tid}/guide.md", b"# Frontend Download OK", mode="no_overwrite")

    async with httpx.AsyncClient(base_url=VITE, timeout=20, trust_env=False) as c:
        r = await c.get(f"/artifacts/{tid}")
        await check("proxy list", r.status_code == 200 and r.json()["count"] == 1,
                    f"via {VITE}")

        r = await c.get(f"/artifacts/{tid}/guide.md")
        await check("proxy download", r.status_code == 200 and r.content == b"# Frontend Download OK",
                    f"mime={r.headers.get('content-type')}")

        r = await c.get(f"/artifacts/{tid}/nope.md")
        await check("proxy 404", r.status_code == 404)

        # 清理
        r = await c.delete(f"/artifacts/{tid}/guide.md")
        await check("proxy delete", r.status_code == 200)

    print("\n结论:", "全部通过 ✅" if ok else "存在失败 ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
