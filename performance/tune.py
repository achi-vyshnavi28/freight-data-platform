"""SQL tuning case study with real numbers: each slow query, why it is slow, the fix, and EXPLAIN ANALYZE before/after.

    python -m performance.tune --scale 8        # builds perf.pings (~5M rows) from staging GPS data

Writes performance/RESULTS.md. Timings are the median of 3 warm runs on whatever
machine runs it, so re-run locally before quoting numbers in an interview.
"""

from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import dataclass
from pathlib import Path

from fdp.db import connect

HERE = Path(__file__).resolve().parent


@dataclass
class Case:
    title: str
    problem: str
    before_sql: str
    fix: str
    after_sql: str
    setup_after: list[str]  # DDL applied between "before" and "after" (indexes etc.)


DAY = "2026-07-15"

CASES = [
    Case(
        title="One vehicle's track for one day",
        problem="No index: Postgres reads all ~5M rows to return ~700.",
        before_sql=f"select ts, lat, lon from perf.pings where vehicle_no = %(veh)s and ts >= '{DAY}' and ts < date '{DAY}' + 1 order by ts",
        fix="Composite B-tree on (vehicle_no, ts): equality column first, range column second, so the index also returns rows already sorted.",
        after_sql=f"select ts, lat, lon from perf.pings where vehicle_no = %(veh)s and ts >= '{DAY}' and ts < date '{DAY}' + 1 order by ts",
        setup_after=["create index if not exists pings_vehicle_ts on perf.pings (vehicle_no, ts)"],
    ),
    Case(
        title="Non-sargable date filter",
        problem="date(ts) = ... wraps the column in a function, so no index on ts can be used.",
        before_sql=f"select count(*) from perf.pings where date(ts) = '{DAY}'",
        fix="Rewrite as a half-open range on the raw column, and add a BRIN index (table is append-ordered by time, so a BRIN of a few KB replaces a B-tree of ~100 MB).",
        after_sql=f"select count(*) from perf.pings where ts >= '{DAY}' and ts < date '{DAY}' + 1",
        setup_after=["create index if not exists pings_ts_brin on perf.pings using brin (ts) with (pages_per_range = 32)"],
    ),
    Case(
        title="Latest position of every vehicle",
        problem="Correlated subquery runs max(ts) once per row: 5M index probes. DISTINCT ON is better but still reads all 5M rows.",
        before_sql="""select p.vehicle_no, p.ts, p.lat, p.lon from perf.pings p
                      where p.ts = (select max(ts) from perf.pings p2 where p2.vehicle_no = p.vehicle_no)""",
        fix="Drive from the vehicle dimension and fetch each vehicle's newest ping with LATERAL ... ORDER BY ts DESC LIMIT 1: "
        "one short index probe per vehicle (a few thousand) instead of touching every ping.",
        after_sql="""select v.vehicle_no, p.ts, p.lat, p.lon from perf.vehicles v
                     cross join lateral (select ts, lat, lon from perf.pings p where p.vehicle_no = v.vehicle_no
                                         order by ts desc limit 1) p""",
        setup_after=["create table if not exists perf.vehicles as select distinct vehicle_no from perf.pings"],
    ),
    Case(
        title="Data-quality worklist (rare rows)",
        problem="~0.3% of pings are bad; finding them means scanning every row.",
        before_sql="select vehicle_no, ts, quality_issue from perf.pings where quality_issue is not null order by ts desc limit 100",
        fix="Partial index covering only the bad rows: tiny, and only maintained when a bad row is written.",
        after_sql="select vehicle_no, ts, quality_issue from perf.pings where quality_issue is not null order by ts desc limit 100",
        setup_after=["create index if not exists pings_bad on perf.pings (ts desc) where quality_issue is not null"],
    ),
    Case(
        title="Daily pings per vehicle for a dashboard",
        problem="The dashboard aggregates 5M rows on every refresh.",
        before_sql="""select vehicle_no, ts::date as day, count(*) as pings, max(speed_kmph) as max_speed
                      from perf.pings where ts >= '2026-07-01' and ts < '2026-08-01' group by 1, 2""",
        fix="Pre-aggregate into a materialized view refreshed by the pipeline; the dashboard reads ~10k rows.",
        after_sql="select vehicle_no, day, pings, max_speed from perf.daily_vehicle_pings where day >= '2026-07-01' and day < '2026-08-01'",
        setup_after=[
            """create materialized view if not exists perf.daily_vehicle_pings as
               select vehicle_no, ts::date as day, count(*) as pings, max(speed_kmph) as max_speed
               from perf.pings group by 1, 2""",
            "create index if not exists dvp_day on perf.daily_vehicle_pings (day)",
        ],
    ),
]


def build_table(scale: int) -> int:
    with connect(autocommit=True) as conn:
        conn.execute("create schema if not exists perf")
        conn.execute("drop materialized view if exists perf.daily_vehicle_pings")
        conn.execute("drop table if exists perf.pings")
        conn.execute("drop table if exists perf.vehicles")
        # Replicate the real pings across `scale` fleets; order by time so the heap is append-ordered like a real feed.
        conn.execute(
            """
            create table perf.pings as
            select p.vehicle_no || '-' || k as vehicle_no, p.ts, p.lat, p.lon, p.speed_kmph, p.quality_issue
            from staging.stg_gps__pings p cross join generate_series(1, %s) k
            order by p.ts
            """,
            (scale,),
        )
        conn.execute("vacuum analyze perf.pings")
        return conn.execute("select count(*) from perf.pings").fetchone()[0]


def explain(conn, sql: str, params: dict) -> tuple[float, str, int]:
    times, plan = [], None
    for _ in range(3):
        row = conn.execute(f"explain (analyze, buffers, format json) {sql}", params).fetchone()[0]
        plan = row[0] if isinstance(row, list) else json.loads(row)[0]
        times.append(plan["Execution Time"])
        if times[0] > 5000:  # very slow plans: one run is enough to make the point
            break

    def nodes(n):
        yield n
        for c in n.get("Plans", []):
            yield from nodes(c)

    top = plan["Plan"]
    scans = sorted(
        {
            n["Node Type"] + (f" on {n['Index Name']}" if n.get("Index Name") else "")
            for n in nodes(top)
            if "Scan" in n["Node Type"]
        }
    )
    buffers = top.get("Shared Hit Blocks", 0) + top.get("Shared Read Blocks", 0)
    return statistics.median(times), ", ".join(scans) or top["Node Type"], buffers


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=int, default=8)
    a = ap.parse_args()
    rows = build_table(a.scale)
    out = [
        f"# SQL tuning case study\n\nTable `perf.pings`: **{rows:,} rows** (real pipeline GPS data replicated x{a.scale}). "
        "Postgres 16, median of 3 warm runs, `EXPLAIN (ANALYZE, BUFFERS)`.\n",
        "| # | Query | Before | After | Speed-up | Plan before → after |",
        "|---|---|---|---|---|---|",
    ]
    details = []
    with connect(autocommit=True) as conn:
        veh = conn.execute("select vehicle_no from perf.pings where ts::date = %s limit 1", (DAY,)).fetchone()[0]
        params = {"veh": veh}
        for i, c in enumerate(CASES, 1):
            b_ms, b_plan, b_buf = explain(conn, c.before_sql, params)
            for ddl in c.setup_after:
                conn.execute(ddl)
            conn.execute("analyze perf.pings")
            if "perf.vehicles" in c.after_sql:
                conn.execute("analyze perf.vehicles")
            a_ms, a_plan, a_buf = explain(conn, c.after_sql, params)
            out.append(
                f"| {i} | {c.title} | {b_ms:,.1f} ms | {a_ms:,.2f} ms | **{b_ms / max(a_ms, 0.001):,.0f}x** | {b_plan} → {a_plan} |"
            )
            details.append(
                f"### {i}. {c.title}\n\n**Problem.** {c.problem}\n\n**Fix.** {c.fix}\n\n"
                f"Buffers touched: {b_buf:,} → {a_buf:,}\n\n```sql\n-- before\n{' '.join(c.before_sql.split())}\n"
                + "".join(f"\n{' '.join(d.split())};" for d in c.setup_after)
                + f"\n-- after\n{' '.join(c.after_sql.split())}\n```\n"
            )
        sizes = conn.execute(
            """select relname, pg_size_pretty(pg_relation_size(c.oid)) from pg_class c join pg_namespace n on n.oid = c.relnamespace
               where nspname = 'perf' order by pg_relation_size(c.oid) desc"""
        ).fetchall()
    out.append("\n**Index sizes** (why BRIN and partial indexes matter): " + ", ".join(f"`{n}` {s}" for n, s in sizes) + "\n")
    out += details
    out.append(
        "**Trade-off to mention in an interview:** every index slows writes and costs disk. The pipeline writes pings in bulk once "
        "and reads them many times, so read-optimising is right here; for a write-heavy OLTP table the answer would differ.\n"
    )
    (HERE / "RESULTS.md").write_text("\n".join(out), encoding="utf-8")
    print("\n".join(out[: 2 + len(CASES) + 1]))


if __name__ == "__main__":
    main()
