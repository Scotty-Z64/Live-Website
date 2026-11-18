"""Object storage: private S3 bucket in af-south-1 with SSE-KMS (production) or a local
filesystem backend (development). Downloads are always via short-lived signed URLs."""
from __future__ import annotations

import hashlib
import hmac
import time
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from ..config import get_settings


class Storage:
    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def signed_url(self, key: str, expires_s: int = 900, filename: str | None = None) -> str: ...


class S3Storage(Storage):
    def __init__(self) -> None:
        import boto3

        s = get_settings()
        self.bucket = s.aws_s3_bucket
        self.kms_key = s.aws_kms_key_id
        self.client = boto3.client(
            "s3",
            region_name=s.aws_region,
            aws_access_key_id=s.aws_access_key_id or None,
            aws_secret_access_key=s.aws_secret_access_key or None,
        )

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        extra = {"ServerSideEncryption": "aws:kms"}
        if self.kms_key:
            extra["SSEKMSKeyId"] = self.kms_key
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type, **extra)
        return key

    def get(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:  # noqa: BLE001
            return False

    def signed_url(self, key: str, expires_s: int = 900, filename: str | None = None) -> str:
        params = {"Bucket": self.bucket, "Key": key}
        if filename:
            params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
        return self.client.generate_presigned_url("get_object", Params=params, ExpiresIn=expires_s)


class LocalStorage(Storage):
    def __init__(self) -> None:
        self.root = Path(get_settings().local_storage_dir)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if not str(p).startswith(str(self.root.resolve())):
            raise ValueError("invalid storage key")
        return p

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return key

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def signed_url(self, key: str, expires_s: int = 900, filename: str | None = None) -> str:
        exp = int(time.time()) + expires_s
        sig = sign_local(key, exp)
        url = f"{get_settings().app_base_url}/api/files/local?key={quote(key)}&exp={exp}&sig={sig}"
        if filename:
            url += f"&filename={quote(filename)}"
        return url


def sign_local(key: str, exp: int) -> str:
    secret = get_settings().jwt_secret.encode()
    return hmac.new(secret, f"{key}|{exp}".encode(), hashlib.sha256).hexdigest()


def verify_local(key: str, exp: int, sig: str) -> bool:
    return exp >= int(time.time()) and hmac.compare_digest(sign_local(key, exp), sig)


@lru_cache
def get_storage() -> Storage:
    return S3Storage() if get_settings().storage_backend == "s3" else LocalStorage()


def tenant_key(tenant_id, *parts: str) -> str:  # noqa: ANN001
    return "/".join(["tenants", str(tenant_id), *parts])
