"""P5 实机冒烟：worker 写入产物 → API /artifacts 跨节点读取（模拟前端/跨节点）。

验证点：
1. POST /tasks 建任务 → GET /artifacts/{task_id} 列表（0）
2. worker 侧经 StorageBackend 写入产物（模拟执行节点本地写）
3. GET /artifacts/{task_id}/{path} 读取内容一致（模拟其他节点/前端读）
4. 越权/不存在 404
5. DELETE 产物 → 再读 404
6. GET /metrics 含 storage 指标计数
7. GET /health storage.ok
"""

import asyncio
import json
import pathlib
import sys

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

BASE = "http://127.0.0.1:8787"


async def main() -> int:
    ok = True
    async def check(name: str, cond: bool, extra: str = "") -> None:
        nonlocal ok
        mark = "PASS" if cond else "FAIL"
        if not cond:
            ok = False
        print(f"[{mark}] {name} {extra}")

    async with httpx.AsyncClient(base_url=BASE, timeout=20, trust_env=False) as c:
        # 0. 健康
        h = (await c.get("/health")).json()
        await check("health", h["status"] == "healthy" and h["storage"]["ok"],
                    json.dumps(h["storage"], ensure_ascii=False))

        # 1. 建任务
        r = await c.post("/tasks", json={"title": "P5 smoke", "workflow_id": "generic"})
        await check("create task", r.status_code == 200, f"status={r.status_code}")
        tid = r.json()["id"]
        r0 = await c.get(f"/artifacts/{tid}")
        await check("list empty", r0.status_code == 200 and r0.json()["count"] == 0)

        # 2. worker 侧写入（模拟执行节点）
        from app.storage import get_backend

        backend = get_backend()
        meta = await backend.put(f"artifacts/{tid}/report.md", b"# P5 Smoke Report", mode="no_overwrite")
        await check("worker put", meta.exists and meta.key == f"artifacts/{tid}/report.md",
                    f"abs_path={bool(meta.abs_path)}")

        # 3. API 读取（跨节点/前端）
        r = await c.get(f"/artifacts/{tid}/report.md")
        await check("api get", r.status_code == 200 and r.content == b"# P5 Smoke Report",
                    f"mime={r.headers.get('content-type')}")

        # 4. 列表含产物
        r = await c.get(f"/artifacts/{tid}")
        await check("list has artifact", r.status_code == 200 and r.json()["count"] == 1
                    and f"artifacts/{tid}/report.md" in r.json()["keys"])

        # 5. 不存在 → 404
        r = await c.get(f"/artifacts/{tid}/nope.md")
        await check("get missing 404", r.status_code == 404)

        # 6. 删除 → 再读 404
        r = await c.delete(f"/artifacts/{tid}/report.md")
        await check("delete", r.status_code == 200)
        r = await c.get(f"/artifacts/{tid}/report.md")
        await check("get after delete 404", r.status_code == 404)

        # 7. metrics storage 指标（API 进程内计数；计数跨运行累积故用 >=）
        m = (await c.get("/metrics")).json()
        st = m.get("storage") or {}
        req = st.get("requests", {})
        await check("metrics storage", req.get("get", 0) >= 1 and req.get("delete", 0) >= 1
                    and req.get("list", 0) >= 2,
                    json.dumps(req, ensure_ascii=False))

    print("\n结论:", "全部通过 ✅" if ok else "存在失败 ❌")
    from app.storage import close_backend

    await close_backend()  # 释放 S3 client session
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
