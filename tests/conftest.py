"""Integration tests run against a real Postgres (CI: a service container) in a dedicated database."""

import os

os.environ.setdefault("FDP_PG_DB", "fdp_test")
os.environ["FDP_PG_DSN"] = (
    f"postgresql://{os.getenv('FDP_PG_USER', 'fdp')}:{os.getenv('FDP_PG_PASSWORD', 'fdp')}"
    f"@{os.getenv('FDP_PG_HOST', 'localhost')}:{os.getenv('FDP_PG_PORT', '5432')}/{os.environ['FDP_PG_DB']}"
)
os.environ.pop("ANTHROPIC_API_KEY", None)

import pytest  # noqa: E402

from fdp import db  # noqa: E402
from fdp.storage import LocalStore  # noqa: E402


@pytest.fixture(scope="session")
def built_warehouse(tmp_path_factory):
    """Generate a small dataset, load it and build every dbt model + test once for the session."""
    from fdp.extract_load import load_all
    from fdp.generate import generate
    from fdp.pipeline import transform

    landing = tmp_path_factory.mktemp("landing")
    db.reset()
    store = LocalStore(landing)
    stats = generate(shipments=400, days=60, ping_minutes=5, store=store)
    loads = load_all(store=store)
    transform(full_refresh=True)
    return {"store": store, "generate": stats, "load": loads}


@pytest.fixture
def conn():
    with db.connect() as c:
        yield c
