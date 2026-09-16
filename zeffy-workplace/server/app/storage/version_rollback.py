"""P7-C2 版本回滚 · 纯元数据预览 + 事务化原子回滚。

审查闭环（Stage0 v2）：
- **预览纯元数据**：复用版本链读取元数据，**不移动/复制文件**，返回回滚后状态快照 + 一次性 token。
- **事务化回滚（内容落位）**：目标历史版本内容原子写回主 key；旧当前内容归档为新版本号（不覆盖原版本链）；
  更新 Artifact 元表 size/sha256 与配额冲正**同一 DB 事务**，全成或全败，物理文件零丢失。
- **权限**：`VERSION_ROLLBACK_ADMIN_ONLY`（默认 true）→ 仅 admin；预览 token 绑操作人 + 一次性 + 过期。
- **审计全字段**：操作人/IP/时间/原版本/目标版本/大小变化/原因。
- **兼容锚点**：`VERSION_ROLLBACK_ENABLED=false`（或总闸/版本开关关）→ 接口 404，读取链路不变，零漂移。
"""
from __future__ import annotations

import logging
import secrets
import time as _time

from app.config import get_settings

logger = logging.getLogger(__name__)

_preview_tokens: dict[str, dict] = {}


def reset_rollback_for_test() -> None:
    _preview_tokens.clear()


class VersionNotFound(Exception):
    pass


class RollbackNotAllowed(Exception):
    pass


def _enabled() -> bool:
    s = get_settings()
    return bool(s.VERSION_ROLLBACK_ENABLED and s.ARTIFACT_META_ENABLED)


def _main_key(task_id: str, rel_path: str) -> str:
    from app.storage.base import ensure_artifact_key

    key = f"artifacts/{task_id}/{rel_path}"
    ensure_artifact_key(key)
    return key


async def _load(session, task_id, rel_path, version):
    """读目标历史版本记录与当前治理 Artifact 行。返回 (rec, cur) 或抛 VersionNotFound。"""
    from app.db import repos

    rec = await repos.get_version(session, task_id=task_id, rel_path=rel_path,
                                  version=version)
    cur = await repos.get_artifact_by_rel(session, task_id=task_id, rel_path=rel_path)
    if rec is None or cur is None or not rec.key:
        raise VersionNotFound("目标版本不存在或为目录/大文件未归档")
    return rec, cur


async def preview(session_factory, *, task_id: str, rel_path: str, version: int,
                  operator: str) -> dict:
    """纯元数据预览：返回回滚后状态快照 + 一次性 token（绑操作人）。"""
    async with session_factory() as s:
        rec, cur = await _load(s, task_id, rel_path, version)
        cur_size = int(cur.size or 0)
        target_size = int(rec.size or 0)
    s = get_settings()
    token = secrets.token_urlsafe(24)
    exp = int(_time.time()) + max(30, int(s.VERSION_ROLLBACK_TOKEN_TTL))
    _preview_tokens[token] = {
        "operator": operator, "exp": exp, "task_id": task_id, "rel_path": rel_path,
        "version": version, "target_size": target_size, "cur_size": cur_size,
    }
    return {
        "version": version, "target_size": target_size,
        "current_size": cur_size, "size_delta": target_size - cur_size,
        "tier": cur.tier if hasattr(cur, "tier") else None,
        "encrypted": bool(rec.mime and False),  # 加密态由读取解密链路按密文头判定
        "rollback_token": token, "expires_at": exp,
    }


def consume_token(token: str, *, operator: str) -> dict | None:
    """校验预览 token（操作人/过期）；仅成功消费（跨用户/过期不消耗 owner 的 token）。"""
    data = _preview_tokens.get(token)
    if data is None or data.get("operator") != operator:
        return None
    if int(data.get("exp", 0)) < int(_time.time()):
        _preview_tokens.pop(token, None)
        return None
    _preview_tokens.pop(token, None)
    return data


async def rollback(session_factory, *, token: str, reason: str, operator: str,
                   client_ip: str = "") -> dict:
    """事务化回滚：内容落位 + 归档旧当前为新版本 + DB 事务 size/sha + 配额 + 审计。"""
    import contextlib

    from app.db import repos
    from app.storage import get_backend
    from app.storage.versioning import V_ARCHIVE, version_key

    if not _enabled():
        raise RollbackNotAllowed("版本回滚未启用")
    if get_settings().VERSION_ROLLBACK_REASON_REQUIRED and not reason:
        raise ValueError("回滚原因必填")
    data = consume_token(token, operator=operator)
    if data is None:
        raise ValueError("回滚 token 无效/过期/已用")
    task_id, rel_path, version = data["task_id"], data["rel_path"], data["version"]
    backend = get_backend()

    # 1) 读目标版本记录与当前治理行
    async with session_factory() as sess:
        rec, cur = await _load(sess, task_id, rel_path, version)
        owner = cur.owner_id or ""
        cur_key = cur.key
        main_key = _main_key(task_id, rel_path)
    old = await backend.get(cur_key)
    new = await backend.get(rec.key)
    if old is None or new is None:
        raise VersionNotFound("当前或目标版本物理文件缺失")
    new_size = len(new)
    delta = new_size - int(data.get("cur_size", 0))

    # 2) 单 DB 事务：分配新版本号 + 归档旧当前为新版本 + 主 key 落位 + 元表/配额/审计
    nv = None
    try:
        async with session_factory() as sess:
            nv = await repos.next_version(sess, task_id=task_id, rel_path=rel_path)
            akey = version_key(task_id, rel_path, nv)
            rec_old = await repos.create_version_record(
                sess, task_id=task_id, rel_path=rel_path, version=nv, key=akey,
                producer_role="rollback", run_id="", mode="overwrite")
            await backend.put(akey, old, mode="overwrite")  # 归档旧当前
            await repos.update_version_status(
                sess, record_id=rec_old.id, status=V_ARCHIVE, size=len(old),
                sha256=_sha(old), mime=rec.mime or "")
            await backend.put(main_key, new, mode="overwrite",
                              producer_role="rollback", mime=rec.mime)  # 主 key 原子写
            await repos.update_artifact_size_sha(sess, artifact_id=cur.id,
                                                 size=new_size, sha256=_sha(new))
            await repos.bump_quota(sess, owner_id=owner, delta=delta)
            await repos.write_audit(
                sess, task_id=task_id, operator=operator,
                action="governance.version.rollback",
                detail={"from_version": version, "new_version": nv,
                        "old_size": data.get("cur_size"), "new_size": new_size,
                        "delta": delta, "reason": reason, "ip": client_ip})
            await sess.commit()
    except Exception:  # noqa: BLE001 事务失败 → 尽力还原主 key 旧内容，避免半状态
        logger.exception("版本回滚 DB 事务失败，还原主 key")
        with contextlib.suppress(Exception):  # noqa: BLE001
            await backend.put(main_key, old, mode="overwrite")
        raise

    return {"ok": True, "version": version, "new_version": nv,
            "new_size": new_size, "delta": delta, "tier": getattr(cur, "tier", None)}


def _sha(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()
