"""The planted business problems must be recovered by the marts. If a model change breaks this, the analysis is wrong."""

from pathlib import Path


def rows(conn, sql):
    return conn.execute(sql).fetchall()


def test_overbilling_vendor_is_found(built_warehouse, conn):
    (top, pct), *rest = rows(
        conn, "select transporter, overbilled_pct from marts.mart_transporter_scorecard order by overbilled_pct desc"
    )
    assert top == "Godavari Freight Carriers"
    assert 20 <= pct <= 40
    assert all(p < 6 for _, p in rest)


def test_mumbai_lanes_are_the_least_reliable(built_warehouse, conn):
    worst = rows(
        conn,
        """select lane from marts.fct_shipments where on_time is not null group by lane
                         order by avg(case when on_time then 1 else 0 end) limit 2""",
    )
    assert {w[0] for w in worst} == {"Hyderabad -> Mumbai", "Mumbai -> Hyderabad"}


def test_effective_dated_rate_join_beats_latest_rate(built_warehouse, conn):
    ((correct, naive_missed),) = rows(
        conn,
        """
        with latest as (select distinct on (origin, destination, vehicle_class_t) origin, destination, vehicle_class_t, rate_per_trip
                        from staging.stg_rates__rate_cards order by origin, destination, vehicle_class_t, effective_from desc)
        select count(*) filter (where s.is_overbilled),
               count(*) filter (where s.is_overbilled and not s.freight_amount > l.rate_per_trip * 1.02)
        from marts.fct_shipments s join latest l using (origin, destination, vehicle_class_t)""",
    )
    assert correct > 0 and naive_missed > 0


def test_every_shipment_got_a_contract_rate(built_warehouse, conn):
    assert rows(conn, "select count(*) from marts.fct_shipments where contracted_rate is null")[0][0] == 0


def test_revenue_leakage_found_both_ways(built_warehouse, conn):
    issues = {r[0] for r in rows(conn, "select distinct issue from marts.mart_revenue_leakage")}
    assert issues == {"delivered, never invoiced", "invoice for unknown shipment"}


def test_report_renders(built_warehouse, tmp_path):
    from fdp.report import write_report

    text = Path(write_report(tmp_path / "f.md")).read_text()
    assert "Godavari Freight Carriers" in text and "effective-dated" in text
