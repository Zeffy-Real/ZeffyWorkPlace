"""P5 S3/MinIO 兼容后端（可选依赖 aiobotocore；未装/未配则不可用）。

- **原子写入兜底**（🔴1）：先写临时 key ``artifacts/_tmp/{uuid}.part`` → 成功后
  ``copy_object`` 到目标 key → 删除临时 key；任一步失败删除临时 key，不污染目标。
- 三种幂等模式与 LocalBackend 完全一致：``overwrite`` 直接覆盖；``no_overwrite``
  先 ``head_object`` 判存在，存在抛 ``FileExistsError_``；``new`` 追加 run_id 后缀。
- 流式（⭐2）：多段上传/下载，不整包进内存。
- 完整性（⭐3）：写入计算 MD5/SHA256 随元数据；读取可用 ETag 服务端校验。
- IAM 最小权限（🔴2）：仅允许 ``artifacts/*`` 前缀（文档/部署清单写明）。
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from typing import Any

from app.config import get_settings
from app.storage.base import (
    ArtifactMeta,
    FileExistsError_,
    IntegrityError,
    RangeNotSatisfiableError,
    StorageBackend,
    StorageError,
    ensure_artifact_key,
    guess_mime,
    normalize_artifact_key,
    tmp_key,
    validate_start,
)

logger = logging.getLogger(__name__)

_CHUNK = 1 << 20  # 1 MiB 分块


class S3Backend(StorageBackend):
    name = "s3"

    def __init__(self, settings: Any) -> None:
        self.endpoint = settings.S3_ENDPOINT or None
        self.bucket = settings.S3_BUCKET
        self.region = settings.S3_REGION or None
        self.aws_access_key_id = settings.S3_ACCESS_KEY
        self.aws_secret_access_key = settings.S3_SECRET_KEY
        self.public_base = (settings.ST_ARTIFACT_PUBLIC_BASE or "").rstrip("/")
        self.signed_ttl = settings.ST_SIGNED_URL_TTL
        self._client: Any = None
        self._session: Any = None
        self._ctx: Any = None

    @staticmethod
    def available() -> bool:
        try:
            import aiobotocore  # noqa: F401
            return True
        except ImportError:
            return False

    async def _get_client(self) -> Any:
        if self._client is None:
            if not self.available():
                raise StorageError("aiobotocore 未安装，无法使用 S3 后端")
            if not self.bucket:
                raise StorageError("S3 后端缺少 S3_BUCKET 配置")
            import aiobotocore.session

            self._session = aiobotocore.session.get_session()
            # aiobotocore 3.x：create_client 返回 ClientCreatorContext，须手动 __aenter__ 获取 client
            self._ctx = self._session.create_client(
                "s3",
                region_name=self.region,
                endpoint_url=self.endpoint,
                aws_access_key_id=self.aws_access_key_id,
                aws_secret_access_key=self.aws_secret_access_key,
            )
            self._client = await self._ctx.__aenter__()
        return self._client

    async def close(self) -> None:
        if self._ctx is not None:
            try:
                await self._ctx.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            self._ctx = None
        self._client = None
        self._session = None

    # ---- 统一契约 ----

    async def put(self, key: str, data, mode: str = "overwrite", *, run_id: str = "",
                  producer_role: str = "", mime: str | None = None,
                  preserve_abs: bool = True) -> ArtifactMeta:
        if mode not in {"overwrite", "no_overwrite", "new"}:
            raise StorageError(f"非法写入模式：{mode!r}")
        ensure_artifact_key(key)
        rel = key[len("artifacts/"):]
        task_id, rel_path = rel.split("/", 1)

        if mode == "new":
            stem, ext = os.path.splitext(rel_path)
            rel_path = f"{stem}-{run_id}{ext}" if run_id else f"{stem}-{uuid.uuid4().hex[:8]}{ext}"
            key = normalize_artifact_key(task_id, rel_path)

        if mode == "no_overwrite":
            if await self.exists(key):
                raise FileExistsError_(f"文件已存在且 mode=no_overwrite，拒绝覆盖：{rel_path}")

        md5, sha256 = await self._write_atomic(key, data)
        return ArtifactMeta(
            key=key, task_id=task_id, rel_path=rel_path,
            size=0, mode=mode, exists=True,
            mime=mime or guess_mime(rel_path), run_id=run_id,
            producer_role=producer_role, md5=md5, sha256=sha256,
            abs_path=None,  # 🔴3 兼容：S3 无本地路径
            url=self._public_url(key),
            backend=self.name,
        )

    async def _write_atomic(self, key: str, data) -> tuple[str, str]:
        client = await self._get_client()
        tmp = tmp_key()
        md5, sha256 = hashlib.md5(), hashlib.sha256()
        try:
            if isinstance(data, (bytes, bytearray)):
                payload = bytes(data)
                md5.update(payload)
                sha256.update(payload)
                await client.put_object(Bucket=self.bucket, Key=tmp, Body=payload)
            else:
                # 流式：多段上传（⭐2 大文件不整包进内存）
                await self._multipart_upload(client, tmp, data, md5, sha256)
            # 原子落位：copy 到目标 → 删除临时
            await client.copy_object(
                Bucket=self.bucket, Key=key,
                CopySource={"Bucket": self.bucket, "Key": tmp},
            )
            await client.delete_object(Bucket=self.bucket, Key=tmp)
        except BaseException:
            # 任一步失败：删除临时 key，不污染目标路径（🔴1）
            try:
                await client.delete_object(Bucket=self.bucket, Key=tmp)
            except Exception:  # noqa: BLE001
                logger.warning("S3 临时对象清理失败：%s", key)
            raise
        return md5.hexdigest(), sha256.hexdigest()

    async def _multipart_upload(self, client, key: str, data, md5, sha256) -> None:
        mpu = await client.create_multipart_upload(Bucket=self.bucket, Key=key)
        upload_id = mpu["UploadId"]
        parts: list[dict] = []
        part_no = 1
        try:
            buffer = b""
            async for chunk in data:
                if not isinstance(chunk, (bytes, bytearray)):
                    chunk = str(chunk).encode("utf-8")
                md5.update(chunk)
                sha256.update(chunk)
                buffer += bytes(chunk)
                if len(buffer) >= _CHUNK:
                    parts.append(await self._upload_part(client, key, upload_id, part_no, buffer))
                    part_no += 1
                    buffer = b""
            if buffer:
                parts.append(await self._upload_part(client, key, upload_id, part_no, buffer))
            if not parts:  # 空流：单块空内容
                parts.append(await self._upload_part(client, key, upload_id, 1, b""))
            await client.complete_multipart_upload(
                Bucket=self.bucket, Key=key, UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
        except BaseException:
            try:
                await client.abort_multipart_upload(
                    Bucket=self.bucket, Key=key, UploadId=upload_id)
            except Exception:  # noqa: BLE001
                logger.warning("S3 多段上传中止失败：%s", key)
            raise

    async def _upload_part(self, client, key: str, upload_id: str, part_no: int, body: bytes) -> dict:
        resp = await client.upload_part(
            Bucket=self.bucket, Key=key, UploadId=upload_id,
            PartNumber=part_no, Body=body,
        )
        return {"PartNumber": part_no, "ETag": resp["ETag"]}

    async def get(self, key: str) -> bytes | None:
        ensure_artifact_key(key)
        client = await self._get_client()
        try:
            resp = await client.get_object(Bucket=self.bucket, Key=key)
            body = await resp["Body"].read()
            await self._check_etag(resp, body, key)
            return body
        except client.exceptions.NoSuchKey:
            return None

    async def stream(self, key: str, start: int = 0):
        ensure_artifact_key(key)
        client = await self._get_client()
        # 🔴3 边界输入：非负整数（负数/浮点/bool 直接 416）；越界由 S3 InvalidRange 兜底
        validate_start(start)
        try:
            kwargs: dict = {"Bucket": self.bucket, "Key": key}
            if start > 0:
                kwargs["Range"] = f"bytes={start}-"
            resp = await client.get_object(**kwargs)
        except client.exceptions.NoSuchKey:
            raise StorageError(f"产物不存在：{key}") from None
        except client.exceptions.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "InvalidRange":
                raise RangeNotSatisfiableError(f"Range 偏移越界：{start}") from exc
            raise
        body = resp["Body"]
        while True:
            chunk = await body.read(_CHUNK)
            if not chunk:
                break
            yield chunk

    async def size(self, key: str) -> int | None:
        ensure_artifact_key(key)
        client = await self._get_client()
        try:
            resp = await client.head_object(Bucket=self.bucket, Key=key)
            return int(resp["ContentLength"])
        except client.exceptions.NoSuchKey:
            return None
        except client.exceptions.ClientError:
            return None

    async def fingerprint(self, key: str) -> str | None:
        """🔴2 S3 强指纹：head_object 的服务端 ETag（同大小内容变更亦不同）。"""
        ensure_artifact_key(key)
        client = await self._get_client()
        try:
            resp = await client.head_object(Bucket=self.bucket, Key=key)
            etag = (resp.get("ETag") or "").strip('"')
            return etag or None
        except client.exceptions.ClientError:
            return None

    async def exists(self, key: str) -> bool:
        ensure_artifact_key(key)
        client = await self._get_client()
        try:
            await client.head_object(Bucket=self.bucket, Key=key)
            return True
        except client.exceptions.NoSuchKey:
            return False
        except client.exceptions.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NoSuchBucket"):
                return False
            raise StorageError(f"S3 head_object 失败：{code}") from exc

    async def delete(self, key: str) -> bool:
        ensure_artifact_key(key)
        client = await self._get_client()
        try:
            await client.delete_object(Bucket=self.bucket, Key=key)
            return True
        except client.exceptions.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey"):
                return False
            raise StorageError(f"S3 delete 失败：{code}") from exc

    async def move(self, src: str, dst: str) -> None:
        """🔴4 事务提交：copy_object 到最终 key + delete 源。"""
        ensure_artifact_key(src)
        ensure_artifact_key(dst)
        client = await self._get_client()
        try:
            await client.copy_object(
                Bucket=self.bucket, Key=dst,
                CopySource={"Bucket": self.bucket, "Key": src},
                MetadataDirective="COPY")
            await client.delete_object(Bucket=self.bucket, Key=src)
        except client.exceptions.ClientError as exc:
            raise StorageError(f"S3 move 失败：{exc}") from exc

    async def list(self, prefix: str) -> list[str]:
        if not prefix.startswith("artifacts/"):
            raise StorageError(f"list 前缀越界：{prefix!r}")
        client = await self._get_client()
        keys: list[str] = []
        paginator = client.get_paginator("list_objects_v2")
        async for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.startswith("artifacts/_tmp/") or key.startswith("artifacts/_tx/"):
                    continue
                keys.append(key)
        return sorted(keys)

    async def health(self) -> dict:
        try:
            client = await self._get_client()
            await client.head_bucket(Bucket=self.bucket)
            return {"ok": True, "backend": self.name, "detail": "reachable"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "backend": self.name, "detail": f"unreachable: {exc}"}

    async def archive_cold(self, key: str) -> bool:
        """P6 分层：copy_object 到自身，指定冷 StorageClass（S3 原生分层）。"""
        ensure_artifact_key(key)
        client = await self._get_client()
        storage_class = get_settings().TIER_COLD_S3_CLASS or "STANDARD_IA"
        try:
            await client.copy_object(
                Bucket=self.bucket, Key=key,
                CopySource={"Bucket": self.bucket, "Key": key},
                MetadataDirective="COPY",
                StorageClass=storage_class,
            )
            return True
        except client.exceptions.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NoSuchBucket"):
                return False
            raise StorageError(f"S3 归档冷存储失败：{key} ({code})") from exc

    # ---- 辅助 ----

    def _public_url(self, key: str) -> str | None:
        if not self.public_base:
            return None
        return f"{self.public_base}/{key}"

    async def _check_etag(self, resp: dict, body: bytes, key: str) -> None:
        """⭐3 完整性：ETag 与本地 md5 对齐校验（S3 单块 ETag = md5 引号包裹）。"""
        etag = (resp.get("ETag") or "").strip('"')
        if etag and len(etag) == 32 and etag.lower() != hashlib.md5(body).hexdigest():
            raise IntegrityError(f"产物完整性校验失败（ETag 不匹配）：{key}")
