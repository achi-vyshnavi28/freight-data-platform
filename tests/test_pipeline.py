import gzip
import json

import pytest

from fdp.db import connect
from fdp.extract_load import extract_erp_invoices, load_all, load_gps, load_tms
from fdp.generate import revise_erp_invoices


def scalar(conn, sql, *params):
    return conn.execute(sql, params).fetchone()[0]


def test_everything_loads_and_is_audited(built_warehouse, conn):
    load = built_warehouse["load"]
    assert load["tms"]["rows"] == built_warehouse["generate"]["tms_rows"]
    assert load["gps"]["rows"] == built_warehouse["generate"]["gps_pings"]
    assert load["erp"]["rows"] == built_warehouse["generate"]["erp_invoices"]
    assert scalar(conn, "select count(*) from meta.load_audit where status <> 'ok'") == 0


def test_rerun_is_a_no_op(built_warehouse, conn):
    before = scalar(conn, "select count(*) from raw.gps_pings")
    again = load_all(store=built_warehouse["store"])
    assert again["tms"]["files_loaded"] == again["gps"]["files_loaded"] == 0
    assert scalar(conn, "select count(*) from raw.gps_pings") == before


def test_redelivered_file_replaces_instead_of_duplicating(built_warehouse, conn):
    store = built_warehouse["store"]
    key = store.list("gps")[0]
    rows = [json.loads(line) for line in gzip.decompress(store.get(key)).splitlines()]
    store.put(key, gzip.compress("\n".join(json.dumps(r) for r in rows[:-10]).encode()))  # vendor re-sends a corrected file
    total_before = scalar(conn, "select count(*) from raw.gps_pings")
    result = load_gps(store=store)
    assert result["files_loaded"] == 1
    assert scalar(conn, "select count(*) from raw.gps_pings") == total_before - 10
    store.put(key, gzip.compress("\n".join(json.dumps(r) for r in rows).encode()))
    load_gps(store=store)


def test_schema_drift_fails_loudly_and_is_audited(built_warehouse, conn):
    store = built_warehouse["store"]
    store.put("tms/dt=2099-01-01/shipments.csv", b"shipment_id,new_column\nX,1\n")
    with pytest.raises(ValueError, match="missing columns"):
        load_tms(day="2099-01-01", store=store)
    assert scalar(conn, "select count(*) from meta.load_audit where status = 'failed' and object_key like '%%2099-01-01%%'") == 1
    assert scalar(conn, "select count(*) from raw.tms_shipments where shipment_id = 'X'") == 0
    (store.root / "tms/dt=2099-01-01/shipments.csv").unlink()


def test_incremental_erp_picks_up_only_revisions(built_warehouse, conn):
    revised = revise_erp_invoices(fraction=0.05)
    out = extract_erp_invoices()
    # revisions, plus whatever sits inside the 1-hour lookback window
    assert revised <= out["rows"] <= revised + 5


def test_quarantine_catches_broken_rows(built_warehouse, conn):
    assert scalar(conn, "select count(*) from quality.quarantine_shipments") > 0
    reasons = {r[0] for r in conn.execute("select quality_issue from quality.quarantine_gps")}
    assert reasons == {"null island fix", "speed spike"}


def test_dedup_keeps_one_row_per_shipment_and_latest_status(built_warehouse, conn):
    assert scalar(conn, "select count(*) from raw.tms_shipments") > scalar(
        conn, "select count(*) from staging.stg_tms__shipments"
    )
    assert scalar(conn, "select count(*) from staging.stg_tms__shipments where status = 'DELIVERED'") > 0
    assert scalar(conn, "select count(*) - count(distinct shipment_id) from staging.stg_tms__shipments") == 0


def test_messy_values_are_normalised(built_warehouse, conn):
    cities = {r[0] for r in conn.execute("select distinct origin from marts.fct_shipments")}
    assert "Bombay" not in cities and "Hyd" not in cities and "Hyderabad" in cities
    assert scalar(conn, "select count(*) from marts.fct_shipments where vehicle_no !~ '^[A-Z0-9]+$'") == 0
    assert scalar(conn, "select count(*) from marts.fct_shipments where weight_kg not between 1000 and 20000") == 0


def test_dbt_tests_stored_failures_for_the_known_bad_invoices(built_warehouse, conn):
    n = scalar(conn, "select count(*) from staging.stg_erp__invoices where not arithmetic_ok")
    assert n >= 1  # planted ~1% arithmetic errors are detected, as a warning not a pipeline failure


def test_read_only_execution_blocks_writes_even_without_the_guard(built_warehouse):
    from fdp.ai.sql_guard import run_readonly

    with connect() as c, pytest.raises(Exception, match="read-only"):
        run_readonly(c, "delete from marts.fct_shipments")
