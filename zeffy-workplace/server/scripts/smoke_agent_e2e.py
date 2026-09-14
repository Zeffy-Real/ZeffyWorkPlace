"""真实 Agent 端到端：WS 提交 → ARQ worker 执行（真实 DeepSeek LLM）→ 产物落库 → /artifacts 下载。

验收（P5 §7 第7条）：Agent 执行产出产物 → 前端/跨节点经 /artifacts 接口下载成功。
流程：
1. WS 连接（AUTH off）发送 user_message（要求产出文件）
2. 等待 task_update enqueued（拿 task_id）
3. 轮询 /tasks/{task_id}/nodes 至全部 done 或 failed（≤180s）
4. GET /artifacts/{task_id} 列表 → 取首个产物
5. GET /artifacts/{task_id}/{rel} 下载内容 → 断言非空
"""

import asyncio
import json
import pathlib
import sys

import httpx
import websockets

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

BASE = "http://127.0.0.1:8787"
WS = "ws://127.0.0.1:8787/ws"
PROMPT = "请用 Python 编写一个计算器程序（支持加减乘除），运行说明写清楚，并把最终代码保存到文件。"


async def main() -> int:
    ok = True

    async def check(name: str, cond: bool, extra: str = "") -> None:
        nonlocal ok
        mark = "PASS" if cond else "FAIL"
        if not cond:
            ok = False
        print(f"[{mark}] {name} {extra}")

    async with httpx.AsyncClient(base_url=BASE, timeout=20, trust_env=False) as c:
        # 1. WS 提交
        task_id: str | None = None
        got_kinds: list[str] = []
        async with websockets.connect(WS, max_size=2**22) as ws:
            await ws.send(json.dumps({
                "kind": "user_message",
                "payload": {"text": PROMPT},
            }))
            deadline = asyncio.get_event_loop().time() + 30
            while asyncio.get_event_loop().time() < deadline:
                raw = await asyncio.wait_for(ws.recv(), timeout=30)
                msg = json.loads(raw)
                got_kinds.append(msg.get("kind", ""))
                if msg.get("kind") == "task_update":
                    p = msg.get("payload") or {}
                    if isinstance(p, dict) and p.get("event") == "enqueued":
                        task_id = p.get("task_db_id") or p.get("task_id")
                        break
        await check("ws submit enqueued", task_id is not None, f"task={task_id}")

        # 2. 轮询节点完成；遇 HITL blocked → 自动审批通过 → 继续轮询
        if task_id:
            status = "queued"
            approved = False
            items: list[dict] = []
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
                    # 审批通过：user_decision → resume 排队执行后续节点
                    async with websockets.connect(WS, max_size=2**22) as ws:
                        await ws.send(json.dumps({
                            "kind": "user_decision",
                            "payload": {"task_id": task_id,
                                        "decision": {"kind": "approval", "approved": True,
                                                     "comment": "端到端自动化验收通过"}},
                        }))
                    approved = True
                    print(f"  -> 已提交审批通过（blocked 节点: {blocked[0].get('node_name')}）")
                    continue
                if all(n["status"] == "done" for n in items) and items:
                    status = "done"
                    break
            await check("agent run done", status == "done",
                        f"final={status} approved={approved} nodes={len(items)}")
            print("  节点状态:", {n["node_name"]: n["status"] for n in items})

            # 3. 产物列表
            r = await c.get(f"/artifacts/{task_id}")
            keys = r.json().get("keys", []) if r.status_code == 200 else []
            await check("artifacts list", r.status_code == 200 and len(keys) > 0,
                        f"keys={keys}")

            # 4. 下载首个产物并验证内容
            if keys:
                rel = "/".join(keys[0].split("/")[2:])
                rd = await c.get(f"/artifacts/{task_id}/{rel}")
                await check("artifacts download", rd.status_code == 200 and len(rd.content) > 0,
                            f"rel={rel} bytes={len(rd.content)} mime={rd.headers.get('content-type')}")
                print("  产物预览:\n" + rd.text[:200])

        print("\n收到 WS 消息种类:", sorted(set(got_kinds)))
        print("结论:", "全部通过 ✅" if ok else "存在失败 ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
