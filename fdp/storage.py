"""Landing zone: the same interface over a local folder or an S3 / MinIO bucket."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol

from .config import LANDING_URI, S3_ENDPOINT


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def list(self, prefix: str) -> list[str]: ...


class LocalStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def put(self, key: str, data: bytes) -> None:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        return (self.root / key).read_bytes()

    def list(self, prefix: str) -> list[str]:
        base = self.root / prefix
        if not base.exists():
            return []
        return sorted(str(p.relative_to(self.root)) for p in base.rglob("*") if p.is_file())


class S3Store:
    def __init__(self, bucket: str, prefix: str = "", endpoint_url: str | None = None) -> None:
        import boto3

        self.bucket, self.prefix = bucket, prefix.strip("/")
        self.s3 = boto3.client("s3", endpoint_url=endpoint_url)

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def put(self, key: str, data: bytes) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=self._key(key), Body=data)

    def get(self, key: str) -> bytes:
        return self.s3.get_object(Bucket=self.bucket, Key=self._key(key))["Body"].read()

    def list(self, prefix: str) -> list[str]:
        keys, token = [], None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": self._key(prefix)}
            if token:
                kw["ContinuationToken"] = token
            page = self.s3.list_objects_v2(**kw)
            strip = len(self.prefix) + 1 if self.prefix else 0
            keys += [o["Key"][strip:] for o in page.get("Contents", [])]
            if not page.get("IsTruncated"):
                return sorted(keys)
            token = page["NextContinuationToken"]


def open_store(uri: str = LANDING_URI) -> ObjectStore:
    if uri.startswith("s3://"):
        bucket, _, prefix = uri[5:].partition("/")
        return S3Store(bucket, prefix, S3_ENDPOINT)
    return LocalStore(uri)


def checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
