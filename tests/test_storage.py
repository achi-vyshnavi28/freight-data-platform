"""The same loader works against S3 (MinIO in docker-compose) - tested here with moto's in-memory S3."""

import boto3
from moto import mock_aws

from fdp.storage import S3Store, checksum, open_store


@mock_aws
def test_s3_store_roundtrip_and_pagination():
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="landing")
    store = S3Store("landing", "freight")
    for i in range(1100):  # more than one ListObjectsV2 page
        store.put(f"gps/dt=2026-07-01/part-{i:04d}.jsonl.gz", b"x")
    keys = store.list("gps/")
    assert len(keys) == 1100 and keys[0] == "gps/dt=2026-07-01/part-0000.jsonl.gz"
    assert store.get(keys[0]) == b"x"


def test_open_store_picks_backend(tmp_path):
    assert type(open_store(str(tmp_path))).__name__ == "LocalStore"


def test_checksum_is_content_based():
    assert checksum(b"a") == checksum(b"a") != checksum(b"b")
