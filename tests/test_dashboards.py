"""Every Grafana panel query must run, as the read-only role Grafana uses, against the built marts."""

import json
import os
import re
from pathlib import Path

import psycopg
import pytest

from fdp.config import ROOT
from fdp.db import connect

DASHBOARD = json.loads((ROOT / "grafana/provisioning/dashboards/freight_ops.json").read_text())
PANELS = [(p["title"], p["targets"][0]["rawSql"]) for p in DASHBOARD["panels"]]


def expand_grafana_macros(sql: str) -> str:
    return re.sub(r"\$__timeFilter\(([^)]+)\)", r"\1 between '2026-01-01' and '2027-01-01'", sql)


@pytest.fixture(scope="module")
def grafana_conn(built_warehouse):
    with connect(autocommit=True) as admin:
        admin.execute(Path(ROOT / "sql/roles.sql").read_text())
    dsn = os.environ["FDP_PG_DSN"].replace("fdp:fdp@", "grafana_ro:grafana_ro@")
    with psycopg.connect(dsn) as c:
        yield c


@pytest.mark.parametrize("title,sql", PANELS, ids=[t for t, _ in PANELS])
def test_panel_query_runs_as_read_only_role(grafana_conn, title, sql):
    rows = grafana_conn.execute(expand_grafana_macros(sql)).fetchall()
    assert rows, f"panel '{title}' returned no rows"


def test_read_only_role_cannot_touch_raw_or_write(grafana_conn):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        grafana_conn.execute("select * from raw.erp_invoices limit 1")
    grafana_conn.rollback()
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        grafana_conn.execute("delete from marts.fct_shipments")
    grafana_conn.rollback()
