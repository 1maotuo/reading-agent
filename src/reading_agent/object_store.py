"""Private S3-compatible object storage for the local Stage 05 preview.

The adapter intentionally accepts only server-created book keys.  It is
usable with local MinIO now and the same boundary can point at a private OSS
S3-compatible bucket later without changing the API contract.
"""

from __future__ import annotations

import io
import os
import re
from pathlib import Path
from uuid import UUID


class ObjectStoreError(RuntimeError):
    """The configured object store is unavailable or rejected an operation."""


_BOOK_KEY = re.compile(r"^books/[0-9a-f-]{36}/[0-9a-f]{64}\.(?:pdf|epub|txt|md)$")


class MinioObjectStore:
    """Small private-bucket adapter with fail-closed key validation."""

    def __init__(
        self,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str = "reading-agent-stage05",
        secure: bool = False,
    ) -> None:
        if not endpoint or not access_key or not secret_key or not bucket:
            raise ObjectStoreError("MinIO endpoint, access key, secret key and bucket are required")
        try:
            from minio import Minio
            from minio.error import S3Error
        except ImportError as exc:  # pragma: no cover - exercised by configuration checks
            raise ObjectStoreError("MinIO SDK is not installed; install requirements-stage05-production.txt") from exc
        self._s3_error = S3Error
        self.endpoint = endpoint.removeprefix("http://").removeprefix("https://").rstrip("/")
        self.bucket = bucket
        self.client = Minio(self.endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
        try:
            if not self.client.bucket_exists(bucket):
                self.client.make_bucket(bucket)
        except self._s3_error as exc:
            raise ObjectStoreError("MinIO bucket is unavailable") from exc

    @staticmethod
    def book_key(book_id: UUID, file_sha256: str, extension: str) -> str:
        key = f"books/{book_id}/{file_sha256}{extension}"
        if not _BOOK_KEY.fullmatch(key):
            raise ObjectStoreError("invalid server-generated object key")
        return key

    @staticmethod
    def _validate_key(key: str) -> None:
        if not _BOOK_KEY.fullmatch(key):
            raise ObjectStoreError("invalid object key")

    def put_bytes(self, key: str, data: bytes, *, content_type: str) -> None:
        self._validate_key(key)
        try:
            self.client.put_object(
                self.bucket,
                key,
                io.BytesIO(data),
                length=len(data),
                content_type=content_type,
            )
        except self._s3_error as exc:
            raise ObjectStoreError("MinIO object upload failed") from exc

    def exists(self, key: str) -> bool:
        self._validate_key(key)
        try:
            self.client.stat_object(self.bucket, key)
            return True
        except self._s3_error as exc:
            if getattr(exc, "code", "") in {"NoSuchKey", "NoSuchObject", "NoSuchBucket"}:
                return False
            raise ObjectStoreError("MinIO object check failed") from exc

    def download_to(self, key: str, target: Path) -> None:
        self._validate_key(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.downloading")
        try:
            response = self.client.get_object(self.bucket, key)
            try:
                with temporary.open("wb") as handle:
                    for chunk in response.stream(1024 * 1024):
                        handle.write(chunk)
            finally:
                response.close()
                response.release_conn()
            os.replace(temporary, target)
        except self._s3_error as exc:
            if temporary.exists():
                temporary.unlink()
            raise ObjectStoreError("MinIO object download failed") from exc
        except Exception:
            if temporary.exists():
                temporary.unlink()
            raise

    def delete_book_objects(self, book_id: UUID) -> int:
        prefix = f"books/{book_id}/"
        try:
            objects = list(self.client.list_objects(self.bucket, prefix=prefix, recursive=True))
            for item in objects:
                self.client.remove_object(self.bucket, item.object_name)
            return len(objects)
        except self._s3_error as exc:
            raise ObjectStoreError("MinIO object deletion failed") from exc
