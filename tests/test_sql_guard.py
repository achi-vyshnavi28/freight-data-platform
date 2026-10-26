import pytest

from fdp.ai.sql_guard import UnsafeSQL, guard

TABLES = {"fct_shipments", "mart_transporter_scorecard"}


def test_plain_select_gets_a_limit():
    out = guard("select transporter, count(*) from marts.fct_shipments group by 1", TABLES)
    assert out.sql.lower().endswith("limit 1000")
    assert out.tables == ["marts.fct_shipments"]


def test_large_limit_is_capped_small_limit_kept():
    assert guard("select * from marts.fct_shipments limit 50000", TABLES).sql.lower().endswith("limit 1000")
    assert guard("select * from marts.fct_shipments limit 5", TABLES).sql.lower().endswith("limit 5")


def test_ctes_and_window_functions_are_fine():
    sql = """with t as (select transporter, sum(freight_variance) v from marts.fct_shipments group by 1)
             select transporter, v, rank() over (order by v desc) from t"""
    assert guard(sql, TABLES).tables == ["marts.fct_shipments"]


@pytest.mark.parametrize(
    "sql",
    [
        "delete from marts.fct_shipments",
        "drop table marts.fct_shipments",
        "update marts.fct_shipments set freight_amount = 0",
        "select 1; drop table marts.fct_shipments",
        "select * from marts.fct_shipments; select 1",
        "insert into marts.fct_shipments select * from marts.fct_shipments",
        "set statement_timeout = 0",
        "copy marts.fct_shipments to '/tmp/x'",
        "create table marts.x as select 1",
    ],
)
def test_writes_and_multiple_statements_are_rejected(sql):
    with pytest.raises(UnsafeSQL):
        guard(sql, TABLES)


@pytest.mark.parametrize(
    "sql",
    [
        "select * from raw.erp_invoices",
        "select * from pg_catalog.pg_shadow",
        "select usename, passwd from pg_shadow",
        "select * from information_schema.tables",
        "select * from fct_shipments",  # unqualified: could resolve to anything on the search_path
        "select * from marts.secret_table",
        "select * from marts.fct_shipments where shipment_id in (select invoice_id from staging.stg_erp__invoices)",
    ],
)
def test_out_of_scope_tables_are_rejected(sql):
    with pytest.raises(UnsafeSQL):
        guard(sql, TABLES)


@pytest.mark.parametrize(
    "sql",
    [
        "select pg_sleep(30)",
        "select pg_read_file('/etc/passwd')",
        "select current_setting('data_directory')",
        "select set_config('statement_timeout', '0', false)",
        "select * from marts.fct_shipments where pg_sleep(1) is null",
    ],
)
def test_dangerous_functions_are_rejected(sql):
    with pytest.raises(UnsafeSQL):
        guard(sql, TABLES)


def test_select_into_rejected():
    with pytest.raises(UnsafeSQL):
        guard("select * into marts.copy from marts.fct_shipments", TABLES)


def test_garbage_rejected():
    with pytest.raises(UnsafeSQL):
        guard("this is not sql at all )(", TABLES)
