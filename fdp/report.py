"""Analysis report: the questions an operations head actually asks, answered from the marts.

Every number in reports/findings.md comes from a query in this file, so the
report is regenerated (and stays honest) on every pipeline run.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .config import ROOT
from .db import connect

QUERIES: dict[str, str] = {
    "headline": """
        select count(*) as shipments,
               round(100.0 * avg(case when on_time then 1 else 0 end), 1) as on_time_pct,
               sum(freight_amount) as freight_billed,
               coalesce(sum(freight_variance) filter (where is_overbilled), 0) as overbilled_amount,
               count(*) filter (where is_overbilled) as overbilled_invoices
        from marts.fct_shipments where status = 'DELIVERED'
    """,
    "vendors": """
        select transporter, shipments, on_time_pct, overbilled_pct, overbilled_amount, invoices_with_errors, delivered_not_invoiced
        from marts.mart_transporter_scorecard order by overbilled_amount desc, on_time_pct
    """,
    "lanes": """
        select lane, sum(shipments)::int as shipments,
               round(sum(on_time_pct * shipments) / sum(shipments), 1) as on_time_pct,
               round(avg(p90_transit_hours)::numeric, 1) as p90_transit_hours
        from marts.mart_lane_performance_monthly group by lane order by on_time_pct
    """,
    "delay_drivers": """
        -- Is lateness explained by long stops (detention/breakdowns) or by slow driving?
        select case when long_stops > 0 then 'had a stop over 60 min' else 'no long stop' end as trip_type,
               count(*) as shipments,
               round(100.0 * avg(case when on_time then 1 else 0 end), 1) as on_time_pct,
               round(avg(delay_hours) filter (where not on_time), 1) as avg_delay_hours_when_late
        from marts.fct_shipments where status = 'DELIVERED' and ping_count > 0
        group by 1 order by 1
    """,
    "leakage": """
        select issue, count(*) as cases, coalesce(sum(amount), 0) as amount
        from marts.mart_revenue_leakage group by issue
    """,
    "naive_vs_correct": """
        with latest_rate as (
            select distinct on (origin, destination, vehicle_class_t) origin, destination, vehicle_class_t, rate_per_trip
            from staging.stg_rates__rate_cards order by origin, destination, vehicle_class_t, effective_from desc
        )
        select count(*) filter (where s.is_overbilled) as correct_effective_dated,
               count(*) filter (where s.freight_amount > r.rate_per_trip * 1.02) as naive_latest_rate,
               count(*) filter (where s.is_overbilled and not s.freight_amount > r.rate_per_trip * 1.02) as missed_by_naive
        from marts.fct_shipments s join latest_rate r using (origin, destination, vehicle_class_t)
    """,
    "quality": """
        select 'shipments quarantined' as check, count(*)::text as value from quality.quarantine_shipments
        union all select 'gps pings quarantined', coalesce(sum(pings), 0)::text from quality.quarantine_gps
        union all select 'invoices failing arithmetic', count(*)::text from staging.stg_erp__invoices where not arithmetic_ok
        union all select 'invoices with revisions', count(*)::text from staging.stg_erp__invoices where revisions_seen > 1
    """,
}


def run_queries() -> dict[str, tuple[list[str], list[tuple]]]:
    out = {}
    with connect() as conn:
        for name, sql in QUERIES.items():
            cur = conn.execute(sql)
            out[name] = ([d.name for d in cur.description], cur.fetchall())
    return out


def _table(cols: list[str], rows: list[tuple]) -> str:
    def fmt(v):
        if v is None:
            return "–"
        if isinstance(v, bool | str):
            return str(v)
        if isinstance(v, int):
            return f"{v:,}"
        if isinstance(v, float | Decimal):
            return f"{v:,.0f}" if abs(v) >= 1000 else f"{v:,.1f}"
        return str(v)

    head = "| " + " | ".join(c.replace("_", " ") for c in cols) + " |\n|" + "---|" * len(cols) + "\n"
    return head + "\n".join("| " + " | ".join(fmt(v) for v in r) + " |" for r in rows)


def write_report(path: Path | None = None) -> str:
    path = path or ROOT / "reports" / "findings.md"
    r = run_queries()
    shipments, on_time, billed, overbilled_amt, overbilled_n = r["headline"][1][0]
    worst_vendor = r["vendors"][1][0]
    worst_lane = r["lanes"][1][0]
    correct, naive, missed = r["naive_vs_correct"][1][0]
    md = f"""# Freight operations findings

_Generated {datetime.now():%Y-%m-%d %H:%M} by `python -m fdp.pipeline report` from the dbt marts._

## Headline
- **{shipments:,} delivered shipments**, **{on_time}% on time**, ₹{billed:,.0f} freight billed.
- **₹{overbilled_amt:,.0f} overbilled** across {overbilled_n} invoices (freight above the contract rate in force at dispatch, beyond a 2% tolerance).
- **{worst_vendor[0]}** accounts for the most overbilling: {worst_vendor[3]}% of its invoices, ₹{worst_vendor[4]:,.0f}.
- **{worst_lane[0]}** is the least reliable lane: {worst_lane[2]}% on time, p90 transit {worst_lane[3]} h.

## Transporter scorecard
{_table(*r["vendors"])}

## Lane reliability
{_table(*r["lanes"])}

## What drives lateness
{_table(*r["delay_drivers"])}

## Revenue leakage between TMS and ERP
{_table(*r["leakage"])}

## Why the rate join must be effective-dated
Rates rose 6% on 1 July 2026. Comparing invoices with the **latest** rate card flags {naive} invoices;
comparing with the rate **in force on the dispatch date** flags {correct}. The naive query silently misses
**{missed}** overbilled invoices, because padded June bills look normal against the higher July rate.

## Data quality this run
{_table(*r["quality"])}
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(md, encoding="utf-8")
    return str(path)
