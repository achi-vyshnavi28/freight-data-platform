"""Settings from environment variables. Defaults match docker-compose and CI."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DBT_DIR = ROOT / "dbt"

PG_DSN = os.getenv("FDP_PG_DSN", "postgresql://fdp:fdp@localhost:5432/fdp")
# Where source systems drop files. "s3://bucket/prefix" uses S3/MinIO, anything else is a local folder.
LANDING_URI = os.getenv("FDP_LANDING_URI", str(ROOT / "landing"))
S3_ENDPOINT = os.getenv("FDP_S3_ENDPOINT")  # e.g. http://localhost:9000 for MinIO
