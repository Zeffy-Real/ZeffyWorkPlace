"""P6-6-4 数据加密 · 上层编排门面（接入 put/get/管理链路，补齐剩余 P0）。

在 ``app.storage.crypto``（纯密码学核心）之上提供**自包含加密封装**与编排：

格式化（gate 层信封 + 核心里层）::

    [GATE_MAGIC 7] [ver u8 1] [salt 16] [wrapped_dek 60] [core_blob ...]

- ``core_blob`` 即 ``crypto.encrypt()`` 产物（内部含 nonce seed 头 + 分块 tag），
  头 HMAC 用元数据密钥（hmack）独立校验；块 tag 用每文件 DEK 校验（先验后出）。
- 封套：``salt + wrapped_dek`` 随密文共存，工件**仅凭主密钥即可解封**，
  满足方案「同密钥不同文件 nonce 不重复」与「DEK 存元数据」。

覆盖的剩余 P0：
- **P0-8 密钥权限/副本**：``unlock()`` 从 ``ENCRYPT_MASTER_KEYFILES``（≥2 副本）
  交叉校验主密钥 + HMAC 密钥；进程内缓存，仅本模块持有，不写日志/异常；不一致即拒解锁。
- **P0-4 密文计量**：``crypto_metrics()`` 明/密双口径 + ``cipher_physical_bytes``。
- **P0-9 故障降级**：解锁/加密失败 → 明文 + 告警审计，不阻塞；解密损坏抛错不崩溃。
- **P0-10 加密审计**：``crypto.*`` 全操作审计（解锁/加密/解密/篡改/降级）。
- **P0-6 事务/版本**：自包含密文可整体迁移（._tx/. _v 同后端），无需重加密；
  diff 在解密后明文上做。

总开关：``ARTIFACT_ENCRYPT_ENABLED`` 且治理总闸 ``ARTIFACT_META_ENABLED``；关则全 no-op。
"""

from __future__ import annotations

import logging
import os
import struct
import threading as _threading
import time as _time

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.config import get_settings
from app.storage import crypto as C

logger = logging.getLogger(__name__)

GATE_MAGIC = b"ZFGATE1"
_GATE_HEAD = 7 + 1 + 16 + 60  # magic + ver + salt + wrapped_dek
_SALT_BYTES = 16


class EncryptConfigError(Exception):
    """密钥配置非法：副本缺失/不一致/HMAC 无法派生/版本回退（触发故障降级）。"""


class _KeyBundle:
    """当前活跃密钥束（仅模块持有，绝不外泄）。"""

    __slots__ = ("master", "hmack", "version", "created_at")

    def __init__(self, master: bytes, hmack: bytes, version: int,
                 created_at: float | None = None) -> None:
        self.master = master
        self.hmack = hmack
        self.version = version
        self.created_at = created_at


class _LegacyBundle:
    """历史版本密钥束（**仅解密**；无任何加密方法，代码级防误用，🔴6）。"""

    __slots__ = ("master", "hmack", "version", "created_at")

    def __init__(self, master: bytes, hmack: bytes, version: int,
                 created_at: float | None = None) -> None:
        self.master = master
        self.hmack = hmack
        self.version = version
        self.created_at = created_at

    # 故意不提供 encrypt / wrap 相关方法：旧密钥绝不用于新加密（🔴6）


_lock: _KeyBundle | None = None
_legacy_bundles: dict[int, _LegacyBundle] = {}
_gray_bundle: _KeyBundle | None = None  # 灰度验证用新版本（ROTATE_GRAY_RATIO 抽样）
_key_revoked = False

_crypto_counters = {
    "encrypt": 0, "decrypt": 0, "decrypt_fail": 0, "tamper": 0,
    "degrade_plain": 0, "unlock_fail": 0, "unlock_ok": 0,
}
_physical_bytes = 0

# 滑动时间窗口事件（告警用；仅白名单计数，不含任何敏感材料）
_wins = {"decrypt_fail": [], "tamper": [], "degrade_plain": []}
_wins_lock = _threading.Lock()

# P7 收尾 · 项3 性能采样（encrypt/decrypt 独立维度；环形上限 + 窗口过期清理）
_PERF_MAX = 1000
_perf: dict[str, list[tuple[float, float]]] = {"encrypt": [], "decrypt": []}


def _perf_record(kind: str, seconds: float, nbytes: int) -> None:
    """记录一次加解密耗时(秒)+字节；超上限环形覆盖，窗口外项惰性清理。"""
    if not get_settings().ENCRYPT_PERF_ENABLED:
        return
    with _wins_lock:
        lst = _perf[kind]
        lst.append((_time.monotonic(), seconds, nbytes))
        if len(lst) > _PERF_MAX:
            del lst[:len(lst) - _PERF_MAX]


def _perf_stats(kind: str) -> dict:
    """某维度的统计：样本数/均值/p50/p95 + ops/s、MB/s（窗口内）。"""
    ws = get_settings().ENCRYPT_WINDOW_S
    now = _time.monotonic()
    with _wins_lock:
        pts = [(s, b) for (t, s, b) in _perf[kind] if now - t <= ws]
    if not pts:
        return {"samples": 0}
    secs = sorted(s for s, _ in pts)
    n = len(secs)
    avg = sum(secs) / n
    p50 = secs[min(n - 1, int(n * 0.50))]
    p95 = secs[min(n - 1, int(n * 0.95))]
    tot_b = sum(b for _, b in pts)
    tot_s = sum(s for s, _ in pts)
    return {
        "samples": n,
        "avg_ms": round(avg * 1000, 3),
        "p50_ms": round(p50 * 1000, 3),
        "p95_ms": round(p95 * 1000, 3),
        "ops_per_s": round(n / ws, 3) if ws else 0.0,
        "mb_per_s": round(tot_b / (1024 * 1024) / (tot_s or 1e-9), 3),
    }


def _bucket_defs() -> list[tuple[int, str]] | None:
    """解析 ENCRYPT_PERF_BUCKETS → [(阈值, 标签)...] 升序；空/非法 → None（不分桶）。

    首档为隐式桶 <最低阈值 的上界。例 ``1048576:1-16M,16777216:16M+`` →
    桶 "<1M"(<1048576) / "1-16M"(≥1048576,<16777216) / "16M+"(≥16777216)。
    """
    s = get_settings()
    raw = (s.ENCRYPT_PERF_BUCKETS or "").strip()
    if not raw:
        return None
    out: list[tuple[int, str]] = []
    try:
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            thr_s, _, label = part.partition(":")
            out.append((int(thr_s.strip()), label.strip() or str(thr_s.strip())))
    except ValueError:
        return None
    return sorted(out, key=lambda t: t[0]) or None


def _bucket_of(size: int) -> str:
    """按文件大小归桶（含下不含上）。

    桶边界：``< thr0`` → 首档隐式桶；``[thr_i, thr_{i+1})`` → label_i；``≥ thr_last`` → 末档。
    """
    defs = _bucket_defs()
    if not defs:
        return "all"
    size = max(0, int(size or 0))
    # < 最低阈值 → 首档隐式桶
    if size < defs[0][0]:
        thr0 = defs[0][0]
        return f"<{thr0 // (1024*1024)}M" if thr0 % (1024 * 1024) == 0 else f"<{thr0}"
    # 落在 [defs[i][0], defs[i+1][0])
    for i, (_thr, label) in enumerate(defs):
        nxt = defs[i + 1][0] if i + 1 < len(defs) else None
        if nxt is None or size < nxt:
            return label
    return defs[-1][1]


def _perf_buckets(kind: str) -> dict:
    """按文件大小分桶的加密性能统计（审查§7-B3）：每桶 samples/avg/p50/p95/mb_per_s。

    纯读 ``_perf`` 快照静态分桶，不改变 ``_perf_record`` 写入；``ENCRYPT_PERF_ENABLED``
    关 → 空（零开销）；桶配置空 → 单桶 ``all``（= 既有全量视图，兼容）。零敏感输出。
    """
    ws = get_settings().ENCRYPT_WINDOW_S
    now = _time.monotonic()
    with _wins_lock:
        pts = [(s, b) for (t, s, b) in _perf[kind] if now - t <= ws] if _perf[kind] else []
    if not pts:
        return {}
    buckets: dict[str, list[tuple[float, int]]] = {}
    for sec, nbytes in pts:
        bkey = _bucket_of(nbytes)
        buckets.setdefault(bkey, []).append((sec, nbytes))
    out: dict[str, dict] = {}
    for bkey, blist in buckets.items():
        nb = len(blist)
        tot_s = sum(x[0] for x in blist)
        tot_b = sum(x[1] for x in blist)
        sc = sorted(x[0] for x in blist)
        out[bkey] = {
            "samples": nb,
            "avg_ms": round((tot_s / nb) * 1000, 3),
            "p50_ms": round(sc[nb // 2] * 1000, 3),
            "p95_ms": round(sc[min(nb - 1, int(nb * 0.95))] * 1000, 3),
            "mb_per_s": round(tot_b / (1024 * 1024) / (tot_s or 1e-9), 3),
        }
    return out


def _bump(name: str) -> None:
    """自增计数器；对窗口事件追加时间戳并裁剪过窗口项（供告警滑动窗口统计）。"""
    now = _time.monotonic()
    _crypto_counters[name] += 1
    if name in _wins:
        with _wins_lock:
            _wins[name].append(now)
            ws = get_settings().ENCRYPT_WINDOW_S
            _wins[name] = [t for t in _wins[name] if now - t <= ws]


def _diag_record(msg: str) -> None:
    """P7-B4 加密异常诊断：解密失败捕获点 → 归因记录。

    惰性导入避免与 observability 互引；诊断关/总闸/加密关任一关闭时 record 内部零采样。
    """
    try:
        from app.observability import encrypt_diagnose

        encrypt_diagnose.record(msg)
    except Exception:  # noqa: BLE001 诊断失败不影响解密主链路
        pass


def crypto_metrics() -> dict:
    """加密可观测快照（明/密双口径 + 计数 + 白名单滑动窗口 + 密钥生命周期）。不泄露任何密钥。"""
    ws = get_settings().ENCRYPT_WINDOW_S
    with _wins_lock:
        window = {k: sum(1 for t in v if _time.monotonic() - t <= ws)
                  for k, v in _wins.items()}
    lc = key_lifecycle_metrics()
    return {
        "enabled": crypt_enabled(),
        "counters": dict(_crypto_counters),
        "window": {**window, "window_seconds": ws},
        "encrypted_physical_bytes": _physical_bytes,
        "cipher_version": _lock.version if _lock else None,
        "key_loaded": _lock is not None,
        "lifecycle": lc,
        "perf": {"encrypt": _perf_stats("encrypt"), "decrypt": _perf_stats("decrypt")},
        # P7-B3 分文件大小性能区间（纯读快照分桶；桶配置空则单桶 all，兼容既有全量视图）
        "perf_buckets": {
            "encrypt": _perf_buckets("encrypt"),
            "decrypt": _perf_buckets("decrypt"),
        },
    }


def key_lifecycle_metrics() -> dict:
    """密钥生命周期（🔴7 / ⭐4）：当前版本/创建时间/到期天数/预警级/历史版本数/灰度版本。"""
    s = get_settings()
    now = _time.time()
    created = _lock.created_at if _lock else None
    rotate_days = max(1, s.ENCRYPT_KEY_ROTATE_DAYS)
    warn_days = max(1, s.ENCRYPT_KEY_WARN_DAYS)
    expire_at = (created + rotate_days * 86400) if created else None
    days_left = None
    level = None
    if expire_at:
        days_left = (expire_at - now) / 86400.0
        if days_left <= 1:
            level = "critical"
        elif days_left <= 7:
            level = "high"
        elif days_left <= warn_days:
            level = "warn"
    return {
        "current_version": _lock.version if _lock else None,
        "created_at": int(created) if created else None,
        "rotate_days": rotate_days,
        "expire_in_days": round(days_left, 1) if days_left is not None else None,
        "expiry_level": level,
        "legacy_versions": sorted(_legacy_bundles),
        "gray_version": _gray_bundle.version if _gray_bundle else None,
        "gray_ratio": s.ENCRYPT_ROTATE_GRAY_RATIO,
    }


def crypt_enabled() -> bool:
    """总开关：治理总闸 + 加密子开关，任一关闭即否（兼容锚点零漂移）。"""
    s = get_settings()
    if not s.ARTIFACT_ENCRYPT_ENABLED:
        return False
    from app.storage.governance import _enabled

    return bool(_enabled())


def is_encrypted_blob(blob: bytes | None) -> bool:
    """探测 blob 是否为本模块密文（读路径判定）。空/非密文 → False。"""
    return bool(blob and blob.startswith(GATE_MAGIC))


# ---------------- P0-8 · 密钥解锁 / 权限 / 副本校验 ----------------

def _wrap_dek_v(master: bytes, dek: bytes, ver: int) -> bytes:
    """信封：主密钥 AESGCM 包裹 DEK，**AAD 绑定版本号**（🔴1 防版本降级）。

    密文头 ver 被篡改 → AAD 不匹配 → 解封失败，杜绝强制走旧密钥路径。
    """
    nonce = os.urandom(12)
    return nonce + AESGCM(master).encrypt(nonce, dek, struct.pack(">B", ver))


def _unwrap_dek_v(master: bytes, wrapped: bytes, ver: int) -> bytes:
    """解封 DEK；AAD 版本不匹配 → EncryptError（DEK 绑定版本校验，🔴1）。"""
    try:
        return AESGCM(master).decrypt(wrapped[:12], wrapped[12:], struct.pack(">B", ver))
    except InvalidTag as exc:
        raise C.EncryptError(f"DEK 封套校验失败（主密钥或版本不匹配：v{ver}）") from exc


def _hmac_for(master: bytes) -> bytes:
    """缺省 HMAC 元数据密钥：独立 HKDF（不同 info），与数据密钥分离。"""
    return HKDF(algorithm=hashes.SHA256(), length=32,
                salt=b"zeffy-meta-hmac", info=b"meta-hmack").derive(master)


def _key_fingerprint(master: bytes) -> str:
    """密钥校验指纹（🔴3）：HKDF 派生 8B 十六进制，用于加载时验证密钥完整性。"""
    fp = HKDF(algorithm=hashes.SHA256(), length=8,
              salt=b"zfk-fp", info=b"fp").derive(master)
    return fp.hex()


# ---- P6-6-6 密钥文件扩展格式（内嵌 版本/创建时间/指纹；兼容旧纯 32B 文件）----
# 首行可选 ``#zfk ver=N created=<epoch> fp=<hex16>``，其后为 RAW/hex 32B。
_KEYFILE_META_MAGIC = b"#zfk"


def _parse_keyfile_meta(raw: bytes) -> dict:
    """解析密钥文件内嵌元数据头；无头 → 空。返回 {ver, created, fp}（可缺失）。"""
    if not raw.startswith(_KEYFILE_META_MAGIC):
        return {}
    out: dict = {}
    line = raw.split(b"\n", 1)[0].decode("utf-8", "ignore")
    for part in line.split():
        if part.startswith("ver="):
            try:
                out["ver"] = int(part.split("=", 1)[1])
            except ValueError:
                pass
        elif part.startswith("created="):
            try:
                out["created"] = float(part.split("=", 1)[1])
            except ValueError:
                pass
        elif part.startswith("fp="):
            out["fp"] = part.split("=", 1)[1].strip().lower()
    return out


def _read_keyfile(path: str) -> bytes | None:
    """读取密钥文件：RAW/hex 32B 或带 ``#zfk`` 元数据头；权限过宽告警。绝不把内容写入日志。"""
    if not path:
        return None
    try:
        st = os.stat(path)
        if hasattr(st, "st_mode") and (st.st_mode & 0o077):
            logger.warning("密钥文件权限过宽 %o（建议 0600）：%s", st.st_mode & 0o777, path)
        if st.st_size > 4096:
            logger.error("密钥文件异常(超长)：%s", path)
            return None
        with open(path, "rb") as f:
            raw = f.read()
    except (OSError, ValueError):
        return None
    if raw.startswith(_KEYFILE_META_MAGIC):
        body = raw.split(b"\n", 1)[1] if b"\n" in raw else b""
        # 内容为精确 32B（随机字节可能含空白，先按原样判定；strip 仅作 hex 尾换行兜底）
    else:
        body = raw  # 纯 32B 不 strip
    if len(body) == 32:
        return body
    body = body.strip()
    if len(body) == 32:
        return body
    try:
        decoded = bytes.fromhex(body.decode("utf-8", "strict"))
        if len(decoded) == 32:
            return decoded
    except (ValueError, UnicodeDecodeError):
        pass
    logger.error("密钥文件内容非 32B（%d）：%s", len(body), path)
    return None


def _parse_legacy_keyfiles(spec: str) -> dict[int, list[str]]:
    """解析 ``ENCRYPT_LEGACY_KEYFILES``：``"1:p1,p2;2:p3"`` → {1:[p1,p2], 2:[p3]}。"""
    out: dict[int, list[str]] = {}
    for group in (spec or "").split(";"):
        group = group.strip()
        if not group or ":" not in group:
            continue
        ver_s, _, paths_s = group.partition(":")
        try:
            ver = int(ver_s.strip())
        except ValueError:
            continue
        paths = [p.strip() for p in paths_s.split(",") if p.strip()]
        if ver > 0 and paths:
            out[ver] = paths
    return out


def _load_master(paths: list[str], *, expected_ver: int,
                 created_at: float | None) -> bytes | None:
    """加载并校验单版本主密钥：副本取**多数一致值**（🔴3）。

    - 多副本逐份比对：不一致/缺失 → 告警（unlock_fail++）但**使用正确副本继续**
      （审查：不一致告警并使用正确副本，杜绝损坏密钥进入运行态）；
    - 内嵌指纹/版本校验仍硬性：多数副本所在文件指纹不符/版本不符 → 拒绝加载。
    """
    if len(paths) < 2:
        return None
    masters = [_read_keyfile(p) for p in paths]
    valid = [m for m in masters if m is not None]
    if not valid:
        return None
    majority = max(set(valid), key=valid.count)
    bad = len(valid) - valid.count(majority)
    if bad:
        logger.warning("主密钥副本存在 %d 份不一致，使用多数副本（建议核对备份）", bad)
    # 内嵌指纹校验（多数副本所在首个文件；旧纯 32B 文件跳过以兼容存量）
    raw = b""
    for p in paths:
        try:
            with open(p, "rb") as f:
                raw = f.read()
            if _parse_keyfile_meta(raw):
                break
        except OSError:
            continue
    meta = _parse_keyfile_meta(raw)
    if meta.get("fp") and _key_fingerprint(majority) != str(meta["fp"]).lower():
        return None  # 密钥被篡改/损坏 → 拒绝加载
    if meta.get("ver") is not None and int(meta["ver"]) != expected_ver:
        return None  # 文件内嵌版本与配置不符 → 拒绝
    return majority


def _created_at_for(s, master: bytes, raw: bytes) -> float:
    """创建时间基准（🔴7）：配置 ENCRYPT_KEY_CREATED_AT > 文件内嵌 > mtime。"""
    if s.ENCRYPT_KEY_CREATED_AT:
        return float(s.ENCRYPT_KEY_CREATED_AT)
    meta = _parse_keyfile_meta(raw)
    if meta.get("created"):
        return float(meta["created"])
    try:
        st = os.stat(s.ENCRYPT_MASTER_KEYFILES.split(",")[0])
        return st.st_mtime
    except (OSError, ValueError, IndexError):
        return 0.0


def _unlock() -> _KeyBundle | None:
    """加载 当前版本 + 历史版本 主密钥束；版本单调校验；任一不过 → None（触发降级）。

    P6-6-6 红线：
    - 当前版本必须 > 所有历史版本（🔴7 版本回退 → 拒绝加载）；
    - 每版本 ≥2 副本逐字节一致 + 内嵌指纹匹配（🔴3 损坏密钥拒用）；
    - 历史版本装入 ``_LegacyBundle``（仅解密，无加密接口，🔴6）。
    """
    global _lock, _legacy_bundles, _gray_bundle
    if _key_revoked:
        return None
    if _lock is not None:
        return _lock
    if not crypt_enabled():
        return None
    s = get_settings()
    cur_ver = s.ENCRYPT_CIPHER_VERSION
    # 版本单调：当前 > 所有历史（防回退降级）
    legacy_map = _parse_legacy_keyfiles(s.ENCRYPT_LEGACY_KEYFILES or "")
    if any(ver >= cur_ver for ver in legacy_map):
        _crypto_counters["unlock_fail"] += 1
        logger.error("密钥版本回退（历史 v%s ≥ 当前 v%s），拒绝加载", sorted(legacy_map), cur_ver)
        return None
    paths = [p.strip() for p in (s.ENCRYPT_MASTER_KEYFILES or "").split(",") if p.strip()]
    master = _load_master(paths, expected_ver=cur_ver, created_at=None)
    if master is None:
        _crypto_counters["unlock_fail"] += 1
        logger.error("主密钥副本缺失/不一致/指纹不符，拒绝解锁")
        return None
    raw = b""
    try:
        with open(paths[0], "rb") as f:
            raw = f.read()
    except OSError:
        pass
    created = _created_at_for(s, master, raw)
    hmack = _read_keyfile(s.ENCRYPT_HMAC_KEYFILE or "")
    if hmack is None:
        hmack = _hmac_for(master)
    _lock = _KeyBundle(master=master, hmack=hmack, version=cur_ver, created_at=created)
    # 历史版本（仅解密；hmack 从各自 master 派生，自包含、与轮换无关——🔴6/兼容）
    for ver, vpaths in legacy_map.items():
        vmaster = _load_master(vpaths, expected_ver=ver, created_at=None)
        if vmaster is None:
            _crypto_counters["unlock_fail"] += 1
            logger.error("历史主密钥 v%s 副本缺失/不一致，拒绝加载", ver)
            _lock = None
            return None
        _legacy_bundles[ver] = _LegacyBundle(master=vmaster, hmack=_hmac_for(vmaster),
                                             version=ver, created_at=None)
    # 灰度验证用新版本（可选；自包含派生）
    gray_paths = [p.strip() for p in (s.ENCRYPT_GRAY_MASTER_KEYFILES or "").split(",")
                  if p.strip()]
    if gray_paths:
        gmaster = _load_master(gray_paths, expected_ver=cur_ver + 1, created_at=None)
        if gmaster is not None:
            _gray_bundle = _KeyBundle(master=gmaster, hmack=_hmac_for(gmaster),
                                      version=cur_ver + 1, created_at=None)
    _crypto_counters["unlock_ok"] += 1
    return _lock


def revoke_keys() -> None:
    """显式撤销密钥束（安全管理：换钥/应急时清零内存引用）。"""
    global _lock, _legacy_bundles, _gray_bundle, _key_revoked
    _lock = None
    _legacy_bundles = {}
    _gray_bundle = None
    _key_revoked = True


def reset_for_test() -> None:
    """测试复位：清内存束、撤销位、指标计数与滑动窗口。"""
    global _lock, _legacy_bundles, _gray_bundle, _key_revoked, _physical_bytes
    _lock = None
    _legacy_bundles = {}
    _gray_bundle = None
    _key_revoked = False
    _physical_bytes = 0
    for k in _crypto_counters:
        _crypto_counters[k] = 0
    with _wins_lock:
        for k in _wins:
            _wins[k].clear()
        for k in _perf:
            _perf[k].clear()


async def _audit_crypto(action: str, *, task_id: str = "", owner_id: str = "",
                        detail: dict | None = None, ok: bool = True, error: str = "") -> None:
    """加密专用审计（P0-10）：并入治理审计通道，action 前缀 crypto.*。"""
    from app.storage.governance import _audit_gov

    await _audit_gov(task_id=task_id, owner_id=owner_id, action=f"crypto.{action}",
                     detail=detail, ok=ok, error=error)


# ---------------- 写路径 · 加密（P0-4 计量 / P0-9 降级） ----------------

async def encrypt_artifact(plain: bytes, *, task_id: str = "", owner_id: str = "") -> tuple[bytes, dict]:
    """明文 → 自包含密文。成功：(cipher, meta{encrypted:True, ...})；失败降级明文。

    故障降级（P0-9）：解锁失败/加密异常 → 返回原始明文 + meta{encrypted:False, reason} +
    严重告警审计，不阻塞写入。计量（P0-4）：meta.cipher_size 密文物理大小。
    """
    global _physical_bytes
    s = get_settings()
    if not crypt_enabled():
        return plain, {"encrypted": False, "plain_size": len(plain),
                       "cipher_size": len(plain), "reason": "crypto_disabled"}
    bundle = _unlock()
    if bundle is None:
        _bump("degrade_plain")
        await _audit_crypto("encrypt.degrade", task_id=task_id, owner_id=owner_id,
                            detail={"reason": "unlock_failed"}, ok=False,
                            error="密钥解锁失败，降级明文")
        return plain, {"encrypted": False, "plain_size": len(plain),
                       "cipher_size": len(plain), "reason": "unlock_failed"}
    try:
        t0 = _time.perf_counter()
        # 灰度抽样（🔴7/⭐1）：ROTATE_GRAY_RATIO>0 且配置灰度密钥 → 按比率用新版本写
        bundle = _lock
        if _gray_bundle is not None and s.ENCRYPT_ROTATE_GRAY_RATIO > 0:
            import random

            if random.random() < min(1.0, max(0.0, s.ENCRYPT_ROTATE_GRAY_RATIO)):
                bundle = _gray_bundle
        ver = bundle.version
        # 🔴6 写入强校验：加密必须用当前活跃版本（灰度新版本为显式白名单，仍在允许范围）
        if ver != s.ENCRYPT_CIPHER_VERSION and ver != s.ENCRYPT_CIPHER_VERSION + 1:
            raise EncryptConfigError(f"加密版本越权：v{ver} 非活跃/灰度版本")
        salt = os.urandom(_SALT_BYTES)
        dek = C.derive_dek(bundle.master, salt)
        wrapped = _wrap_dek_v(bundle.master, dek, ver)  # AAD 绑定版本（🔴1）
        core = C.encrypt(plain, dek, bundle.hmack)
        head = GATE_MAGIC + struct.pack(">B", ver) + salt + wrapped
        cipher = head + core
        _perf_record("encrypt", _time.perf_counter() - t0, len(plain))
        _crypto_counters["encrypt"] += 1
        _physical_bytes += len(cipher)
        await _audit_crypto("encrypt", task_id=task_id, owner_id=owner_id,
                            detail={"plain_size": len(plain), "cipher_size": len(cipher),
                                    "version": ver})
        return cipher, {"encrypted": True, "plain_size": len(plain),
                        "cipher_size": len(cipher), "algo": "AES-256-GCM",
                        "version": ver}
    except Exception as exc:  # noqa: BLE001 降级不阻断
        _bump("degrade_plain")
        await _audit_crypto("encrypt.degrade", task_id=task_id, owner_id=owner_id,
                            detail={"reason": "encrypt_error"}, ok=False, error=str(exc))
        logger.exception("加密失败降级明文（task=%s）：%s", task_id, exc)
        return plain, {"encrypted": False, "plain_size": len(plain),
                       "cipher_size": len(plain), "reason": "encrypt_error"}


# ---------------- 读路径 · 解封 + 解密（头校验 / 篡改检测 / 降级语义） ----------------

async def _crypto_stream_encrypt(plain_iter, *, total: int | None = None,
                                 task_id: str = "", owner_id: str = ""):
    """流式加密（P7 收尾 · P0-1）：明文 async iter → 密文 async iter + 元数据。

    内存常量级（单块缓冲）。返回 ``(enc_stream, meta)``；meta.md5/sha 为流式计算对象，
    待后端消费完密文流后取 ``.hexdigest()``。加密关闭/解锁失败 → 原样透传明文流。
    """
    import hashlib

    if not crypt_enabled():
        return plain_iter, {"encrypted": False, "plain_size": total or 0,
                            "cipher_size": total or 0, "md5": "", "sha256": "",
                            "reason": "crypto_disabled"}
    bundle = _unlock()
    if bundle is None:
        _bump("degrade_plain")
        await _audit_crypto("encrypt.degrade", task_id=task_id, owner_id=owner_id,
                            detail={"reason": "unlock_failed"}, ok=False,
                            error="密钥解锁失败，降级明文")
        return plain_iter, {"encrypted": False, "plain_size": total or 0,
                            "cipher_size": total or 0, "md5": "", "sha256": "",
                            "reason": "unlock_failed"}
    salt = os.urandom(_SALT_BYTES)
    dek = C.derive_dek(bundle.master, salt)
    wrapped = _wrap_dek_v(bundle.master, dek, bundle.version)
    enc = C.StreamingEncryptor(dek, bundle.hmack, plaintext_len=total)
    head = GATE_MAGIC + struct.pack(">B", bundle.version) + salt + wrapped
    md5 = hashlib.md5()
    sha = hashlib.sha256()

    async def _gen():
        global _physical_bytes
        t0 = _time.perf_counter()
        plain_bytes = 0
        yield head + enc.header
        body = 0
        try:
            async for chunk in plain_iter:
                md5.update(chunk)
                sha.update(chunk)
                plain_bytes += len(chunk)
                for f in enc.feed(chunk):
                    body += len(f)
                    yield f
            tail = enc.finalize()
            if tail:
                body += len(tail)
                yield tail
        except BaseException:
            enc.close()
            raise
        _perf_record("encrypt", _time.perf_counter() - t0, plain_bytes)
        _crypto_counters["encrypt"] += 1
        _physical_bytes += len(head) + len(enc.header) + body
        await _audit_crypto("encrypt", task_id=task_id, owner_id=owner_id,
                            detail={"plain_size": total or 0,
                                    "version": bundle.version})

    meta = {"encrypted": True, "plain_size": total or 0, "cipher_size": None,
            "version": bundle.version, "md5": md5, "sha256": sha}
    return _gen(), meta


async def _decrypt_stream_rest(cipher_stream, *, head: bytes, start: int = 0,
                               end: int | None = None,
                               task_id: str = "", owner_id: str = ""):
    """流式解密（P7 收尾 · P0-1）：``head`` 为首块（≥gate 头，含 core 前缀），
    后续密文继续从 ``cipher_stream`` 消费；边解边 yield 明文（内存常量级）。

    先验 gate 头 HMAC + 内层头 HMAC + 每块 tag（P0-3）；篡改抛 C.EncryptError。
    """
    bundle, core_prefix, dek = _split(head)
    d = C.StreamingDecryptor(dek, bundle.hmack, start=start, end=end)
    t0 = _time.perf_counter()
    plain_bytes = 0
    try:
        for pt in d.feed(core_prefix):
            plain_bytes += len(pt)
            yield pt
        async for chunk in cipher_stream:
            for pt in d.feed(chunk):
                plain_bytes += len(pt)
                yield pt
        tail = d.finalize()
        if tail:
            plain_bytes += len(tail)
            yield tail
        _perf_record("decrypt", _time.perf_counter() - t0, plain_bytes)
        _crypto_counters["decrypt"] += 1
        await _audit_crypto("decrypt", task_id=task_id, owner_id=owner_id,
                            detail={"mode": "stream"})
    except C.EncryptError:
        _bump("decrypt_fail")
        _bump("tamper")
        await _audit_crypto("decrypt.tamper", task_id=task_id, owner_id=owner_id,
                            detail={"reason": "stream_tamper"}, ok=False,
                            error="流式解密校验失败")
        _diag_record("流式解密校验失败")
        raise


def _bundle_for(ver: int):
    """按密文头版本选密钥束：当前版本 → _lock；历史 → _legacy_bundles（仅解密）。

    🔴1/🔴6：仅当前与归档版本可解；未知版本拒绝。
    """
    s = get_settings()
    if ver == s.ENCRYPT_CIPHER_VERSION and _lock is not None:
        return _lock
    legacy = _legacy_bundles.get(ver)
    if legacy is not None:
        return legacy
    # 兼容旧纯 `ENCRYPT_LEGACY_VERSIONS` 白名单（无归档密钥时按当前密钥解，仅历史格式兜底）
    old_white = {int(v.strip()) for v in (s.ENCRYPT_LEGACY_VERSIONS or "").split(",")
                 if v.strip()}
    if ver in old_white and _lock is not None:
        return _lock
    return None


def _split(cipher: bytes) -> tuple[_KeyBundle, bytes, bytes]:
    """解析 gate 信封 + 按版本解封 DEK → (bundle, core_blob, dek)。失败抛 C.EncryptError。

    P6-6-6：DEK 封套 AAD 绑定版本号（🔴1），头 ver 被篡改 → 解封失败拒绝解密。
    """
    if _unlock() is None:
        _bump("decrypt_fail")
        raise C.EncryptError("密钥未解锁，无法解密")
    if len(cipher) < _GATE_HEAD or not cipher.startswith(GATE_MAGIC):
        _bump("decrypt_fail")
        raise C.EncryptError("密文信封头损坏")
    ver = cipher[7]
    bundle = _bundle_for(ver)
    if bundle is None:
        _bump("decrypt_fail")
        raise C.EncryptError(f"不支持的加密版本：{ver}")
    wrapped = cipher[8 + _SALT_BYTES:_GATE_HEAD]
    dek = _unwrap_dek_v(bundle.master, wrapped, ver)  # AAD=ver 绑定（🔴1）
    return bundle, cipher[_GATE_HEAD:], dek


async def decrypt_artifact(cipher: bytes, *, task_id: str = "", owner_id: str = "") -> bytes:
    """自包含密文 → 明文。损坏/篡改/密钥错误 → 抛 ``C.EncryptError``（API 层转 4xx）。

    P0-10 篡改：头 HMAC / 块 tag / 信封校验失败 → ``crypto.decrypt.tamper`` 审计 + 计数。
    """
    try:
        t0 = _time.perf_counter()
        _bundle, core, dek = _split(cipher)
        plain = C.decrypt_full(core, dek, _bundle.hmack)
        _perf_record("decrypt", _time.perf_counter() - t0, len(plain))
        _crypto_counters["decrypt"] += 1
        await _audit_crypto("decrypt", task_id=task_id, owner_id=owner_id,
                            detail={"plain_size": len(plain)})
        return plain
    except C.EncryptError as exc:
        _bump("decrypt_fail")
        _bump("tamper")
        await _audit_crypto("decrypt.tamper", task_id=task_id, owner_id=owner_id,
                            detail={"reason": str(exc)}, ok=False, error=str(exc))
        _diag_record(str(exc))
        raise


async def decrypt_range_artifact(cipher: bytes, *, start: int, end: int | None,
                                 task_id: str = "", owner_id: str = "") -> bytes:
    """Range 解密（P0-2）：gate 解封 → 内部按块对齐解 core，再截 [start, end)。"""
    try:
        _bundle, core, dek = _split(cipher)
        plain = C.decrypt_range(core, dek, _bundle.hmack,
                                start=start, end=end)
        _crypto_counters["decrypt"] += 1
        return plain
    except C.EncryptError as exc:
        _bump("decrypt_fail")
        _bump("tamper")
        await _audit_crypto("decrypt.tamper", task_id=task_id, owner_id=owner_id,
                            detail={"reason": str(exc)}, ok=False, error=str(exc))
        _diag_record(str(exc))
        raise


def peek_plain_size(cipher: bytes) -> int | None:
    """读头明文大小（读路径 Content-Length/进度用）；非密文/未解锁/损坏 → None。"""
    if not is_encrypted_blob(cipher):
        return None
    try:
        _bundle, core, _dek = _split(cipher)
        _block, plen, _seed = C.parse_header(core, _bundle.hmack)
        return plen
    except (C.EncryptError, IndexError):
        return None


# ===========================================================================
# P6-6-6 · DEK 重裹（仅重裹不重加密） / 三阶段回收 / 版本引用扫描
# ===========================================================================

def _rewrap_one(cipher: bytes) -> bytes:
    """单密文 DEK 重裹：旧版本 → 当前活跃版本（🔴2 原子语义由调用方保证）。

    - 解封旧 DEK（AAD=旧版本）→ 用当前主密钥重裹（AAD=当前版本）；
    - 内层 core 块密文**原样保留**（同一 DEK），仅重算内层头 HMAC（新版本 hmack）——
      真正「只重裹不重加密明文」；重裹后完整性校验通过（🔴2）。
    """
    bundle, core, dek = _split(cipher)
    old_ver = cipher[7]
    s = get_settings()
    cur_ver = s.ENCRYPT_CIPHER_VERSION
    if old_ver == cur_ver:
        return cipher  # 已是当前版本，幂等
    # 内层 core 头校验（旧 hmack）→ 通过才重裹；失败抛错不产出半成品
    blk, plen, seed = C.parse_header(core, bundle.hmack)
    # 重算内层头 HMAC（新 hmack），块密文原样保留
    new_head = C.build_header(block=blk, plaintext_len=plen, seed=seed,
                              hmack=_lock.hmack)
    head_len = len(C.MAGIC) + 4 + 4 + 8 + 8 + 32  # MAGIC+meta+seed+hmac
    new_core = new_head + core[head_len:]
    salt = os.urandom(_SALT_BYTES)
    new_wrapped = _wrap_dek_v(_lock.master, dek, cur_ver)
    return GATE_MAGIC + struct.pack(">B", cur_ver) + salt + new_wrapped + new_core


async def rotate_rewrap_deks(*, keys: list[str] | None = None,
                             backend=None) -> dict:
    """批量 DEK 重裹（admin/async；🔴2/🔴5）。

    - 范围：仅接受显式 ``keys`` 列表（🔴5 默认禁无范围全量；调用方负责按任务/版本选）；
    - 每文件：读原密文 → ``_rewrap_one`` → 校验新密文可解 → 原子写（临时→替换）。
      Local 后端 put(overwrite) 即临时文件+rename；S3 put 为对象级原子。
    - 失败跳过并审计，不中断；幂等（已当前版本直接跳过）。
    """
    if not crypt_enabled():
        return {"enabled": False, "rewrapped": 0, "failed": 0}
    backend = backend or _get_backend()
    ok_cnt = fail_cnt = 0
    failed: list[str] = []
    for key in keys or []:
        try:
            blob = await backend.get(key)
            if not is_encrypted_blob(blob):
                continue
            new_cipher = _rewrap_one(blob)
            if new_cipher == blob:
                continue  # 已是当前版本
            # 校验新密文可解（🔴2 完整性 100% 通过才落位）
            _b2, _c2, _d2 = _split(new_cipher)
            C.decrypt_full(_c2, _d2, _b2.hmack)
            await backend.put(key, new_cipher, mode="overwrite",
                              producer_role="crypto-rewrap")
            ok_cnt += 1
        except Exception as exc:  # noqa: BLE001 失败跳过不中断（🔴2）
            fail_cnt += 1
            failed.append(key)
            logger.warning("重裹失败 %s: %s", key, exc)
    await _audit_crypto("rotate.rewrap", detail={"count": ok_cnt, "failed": fail_cnt},
                        ok=fail_cnt == 0, error=f"failed={fail_cnt}")
    return {"enabled": True, "rewrapped": ok_cnt, "failed": fail_cnt,
            "failed_keys": failed[:50]}


async def scan_legacy_refs(backend=None) -> dict:
    """全量扫描存量密文的版本引用（🔴4 回收前置校验 + 审计）。返回 {version: count}。"""
    if not crypt_enabled():
        return {"enabled": False, "refs": {}}
    backend = backend or _get_backend()
    from app.storage.governance import _list_governance_keys

    refs: dict[int, int] = {}
    for key in await _list_governance_keys(backend):
        try:
            blob = await backend.get(key)
            if is_encrypted_blob(blob):
                ver = blob[7]
                refs[ver] = refs.get(ver, 0) + 1
        except Exception:  # noqa: BLE001 单个读失败跳过
            continue
    await _audit_crypto("rotate.scan_refs", detail={"refs": refs})
    return {"enabled": True, "refs": refs}


async def retire_legacy_key(*, version: int, force: bool = False,
                            backend=None) -> dict:
    """三阶段回收（🔴4 阶段①归档状态由配置表达，这里做前置校验 + 审计）。

    - 前置：全量扫描确认该版本零活跃引用（force=False 时）；
    - 结果返回 ``ok``；**物理删除/移出配置由运维在配置层执行**（三阶段回收的阶段③），
      本函数仅做安全门槛与留痕，绝不静默删密钥。
    """
    if not crypt_enabled():
        return {"enabled": False, "ok": False}
    scan = await scan_legacy_refs(backend=backend)
    refs = scan.get("refs", {})
    active = int(refs.get(version, 0) or 0)
    if active > 0 and not force:
        await _audit_crypto("rotate.retire_blocked", detail={"version": version,
                                                             "refs": active},
                            ok=False, error="仍有活跃引用")
        return {"ok": False, "reason": "active_refs", "refs": active}
    await _audit_crypto("rotate.retire_allowed", detail={"version": version,
                                                         "refs": active,
                                                         "force": force})
    return {"ok": True, "version": version, "refs": active,
            "stage": "phase1_retired_from_active"}


def _get_backend():
    from app.storage import get_backend as _gb

    return _gb()
