"""真实 Agent 多轮重跑 · 版本链验证（ARTIFACT_VERSIONS_ENABLED=true + 真实 DeepSeek + PG）。

流程：
1. WS 提交 → ARQ worker + 真实 LLM 执行 → doer 用 fs_write 产出文件（走 VersionManager）
2. 等任务 done（自动审批）
3. 验证：该任务产物已登记进 ``artifact_versions``（真实 PG 数据）
4. 对同一文件脚本覆写 3 次（模拟「多轮重跑覆写同一产物」）
5. 验证版本链 total 递增、可回溯任意版本、可 diff
"""

import asyncio
import json
import pathlib
import sys

import httpx
import websockets

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# 脚本进程内显式启用版本（脚本与后端进程独立，env 不共享）
from app.config import get_settings

get_settings().ARTIFACT_VERSIONS_ENABLED = True
get_settings().ARTIFACT_MAX_VERSIONS = 5
from app.storage import reset_backend as _rb

_rb()

BASE = "http://127.0.0.1:8787"
WS = "ws://127.0.0.1:8787/ws"
PROMPT = "请写一个 Python 版 todo 清单程序（支持增删查），把最终代码保存到 todo_app.py 文件。"


async def main() -> int:
    ok = True

    async def check(name: str, cond: bool, extra: str = "") -> None:
        nonlocal ok
        mark = "PASS" if cond else "FAIL"
        if not cond:
            ok = False
        print(f"[{mark}] {name} {extra}")

    async with httpx.AsyncClient(base_url=BASE, timeout=30, trust_env=False) as c:
        # 1. WS 提交真实 Agent
        task_id: str | None = None
        async with websockets.connect(WS, max_size=2**22) as ws:
            await ws.send(json.dumps({"kind": "user_message",
                                      "payload": {"text": PROMPT}}))
            deadline = asyncio.get_event_loop().time() + 30
            while asyncio.get_event_loop().time() < deadline:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=35))
                if msg.get("kind") == "task_update":
                    p = msg.get("payload") or {}
                    if isinstance(p, dict) and p.get("event") == "enqueued":
                        task_id = p.get("task_db_id") or p.get("task_id")
                        break
        await check("ws submit enqueued", task_id is not None, f"task={task_id}")

        # 2. 等完成（自动审批 blocked）
        status = "queued"
        approved = False
        rel = ""
        if task_id:
            for _ in range(160):
                await asyncio.sleep(1.5)
                r = await c.get(f"/tasks/{task_id}/nodes")
                if r.status_code != 200:
                    continue
                items = r.json()["items"]
                sts = {n["status"] for n in items}
                if "failed" in sts:
                    status = "failed"
                    break
                blocked = [n for n in items if n["status"] == "blocked"]
                if blocked and not approved:
                    async with websockets.connect(WS, max_size=2**22) as ws:
                        await ws.send(json.dumps({
                            "kind": "user_decision",
                            "payload": {"task_id": task_id,
                                        "decision": {"kind": "approval", "approved": True,
                                                     "comment": "版本链验证通过"}},
                        }))
                    approved = True
                    continue
                if all(n["status"] == "done" for n in items) and items:
                    status = "done"
                    break
            await check("agent run done", status == "done", f"approved={approved}")
            print("  节点:", {n["node_name"]: n["status"] for n in items})

            # 3. 产物列表 → 找到真实 Agent 写的文件（可能存在 _v 相关，取 task 根下非 _v 的）
            r = await c.get(f"/artifacts/{task_id}")
            keys = [k for k in r.json().get("keys", []) if "/_v/" not in k]
            print("  产物keys:", keys)
            if keys:
                rel = keys[0].split("/", 2)[2]  # artifacts/{tid}/{rel}
                print(f"  待重跑文件: {rel}")

        # 4. 多轮重跑：对同一文件脚本覆写 3 次（模拟 Agent 重跑覆写）
        # 注：真实 Agent doer 用 no_overwrite 首次创建（设计上不产生版本）；
        # 版本链在「overwrite 覆写同一文件」时产生——这正是多轮重跑的覆写路径。
        if task_id and rel:
            raw = (await c.get(f"/artifacts/{task_id}/{rel}")).content
            # 首次覆写 v1（把 Agent 产出固化为版本1）
            from app.storage import get_backend

            await get_backend().put(f"artifacts/{task_id}/{rel}", raw,
                                    mode="overwrite", run_id="baseline")
            for i, label in enumerate(["进阶功能", "优化注释", "最终版"]):
                content = raw.decode("utf-8", errors="replace") + f"\n# {label} 追加行-{i}\n"
                await get_backend().put(f"artifacts/{task_id}/{rel}",
                                        content.encode(), mode="overwrite", run_id=f"rerun-{i}")
            res = await c.get(f"/artifacts/{task_id}/_versions?path={rel}")
            body = res.json()
            # baseline + 3 次覆写 = 4 个版本
            await check("版本链已增长", body["total"] == 4,
                        f"total={body['total']} (期望4: baseline+3覆盖)")
            versions = [i["version"] for i in body["items"]]
            await check("版本号倒序", versions == sorted(versions, reverse=True),
                        f"versions={versions}")

            # 5. 回溯 + diff
            v_last = versions[0]
            r_back = await c.get(f"/artifacts/{task_id}/{rel}?version={v_last}")
            await check("回溯最新版本", r_back.status_code == 200 and len(r_back.content) == len(content.encode()),
                        f"v{v_last} bytes={len(r_back.content)}")
            d = (await c.get(f"/artifacts/{task_id}/_diff?path={rel}&from_v=1&to_v={v_last}")).json()
            await check("diff 生效", d.get("status") == "ok" and d.get("added", 0) > 0,
                        f"diff added={d.get('added')} removed={d.get('removed')}")

            # 6. PG 真实落库统计
            from app.db.base import get_session_factory
            from app.db import repos

            factory = get_session_factory()
            async with factory() as s:
                stats = await repos.version_stats(s, task_id=task_id)
            await check("PG 版本记录落库", stats["total"] == 4,
                        json.dumps(stats, ensure_ascii=False))

    print("\n结论:", "全部通过 ✅" if ok else "存在失败 ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))