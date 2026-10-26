# SQL tuning case study

Table `perf.pings`: **5,190,816 rows** (real pipeline GPS data replicated x8). Postgres 16, median of 3 warm runs, `EXPLAIN (ANALYZE, BUFFERS)`.

| # | Query | Before | After | Speed-up | Plan before → after |
|---|---|---|---|---|---|
| 1 | One vehicle's track for one day | 131.2 ms | 0.49 ms | **267x** | Seq Scan → Bitmap Heap Scan, Bitmap Index Scan on pings_vehicle_ts |
| 2 | Non-sargable date filter | 183.7 ms | 5.84 ms | **31x** | Seq Scan → Bitmap Heap Scan, Bitmap Index Scan on pings_ts_brin |
| 3 | Latest position of every vehicle | 45,272.4 ms | 2.53 ms | **17,873x** | Index Only Scan on pings_vehicle_ts, Seq Scan → Index Scan on pings_vehicle_ts, Seq Scan |
| 4 | Data-quality worklist (rare rows) | 163.2 ms | 0.05 ms | **3,401x** | Seq Scan → Index Scan on pings_bad |
| 5 | Daily pings per vehicle for a dashboard | 607.8 ms | 0.66 ms | **915x** | Bitmap Heap Scan, Bitmap Index Scan on pings_ts_brin → Bitmap Heap Scan, Bitmap Index Scan on dvp_day |

**Index sizes** (why BRIN and partial indexes matter): `pings` 380 MB, `pings_vehicle_ts` 201 MB, `daily_vehicle_pings` 1024 kB, `pings_bad` 160 kB, `dvp_day` 112 kB, `pings_ts_brin` 72 kB, `vehicles` 16 kB

### 1. One vehicle's track for one day

**Problem.** No index: Postgres reads all ~5M rows to return ~700.

**Fix.** Composite B-tree on (vehicle_no, ts): equality column first, range column second, so the index also returns rows already sorted.

Buffers touched: 48,688 → 214

```sql
-- before
select ts, lat, lon from perf.pings where vehicle_no = %(veh)s and ts >= '2026-07-15' and ts < date '2026-07-15' + 1 order by ts

create index if not exists pings_vehicle_ts on perf.pings (vehicle_no, ts);
-- after
select ts, lat, lon from perf.pings where vehicle_no = %(veh)s and ts >= '2026-07-15' and ts < date '2026-07-15' + 1 order by ts
```

### 2. Non-sargable date filter

**Problem.** date(ts) = ... wraps the column in a function, so no index on ts can be used.

**Fix.** Rewrite as a half-open range on the raw column, and add a BRIN index (table is append-ordered by time, so a BRIN of a few KB replaces a B-tree of ~100 MB).

Buffers touched: 48,576 → 362

```sql
-- before
select count(*) from perf.pings where date(ts) = '2026-07-15'

create index if not exists pings_ts_brin on perf.pings using brin (ts) with (pages_per_range = 32);
-- after
select count(*) from perf.pings where ts >= '2026-07-15' and ts < date '2026-07-15' + 1
```

### 3. Latest position of every vehicle

**Problem.** Correlated subquery runs max(ts) once per row: 5M index probes. DISTINCT ON is better but still reads all 5M rows.

**Fix.** Drive from the vehicle dimension and fetch each vehicle's newest ping with LATERAL ... ORDER BY ts DESC LIMIT 1: one short index probe per vehicle (a few thousand) instead of touching every ping.

Buffers touched: 20,852,063 → 1,722

```sql
-- before
select p.vehicle_no, p.ts, p.lat, p.lon from perf.pings p where p.ts = (select max(ts) from perf.pings p2 where p2.vehicle_no = p.vehicle_no)

create table if not exists perf.vehicles as select distinct vehicle_no from perf.pings;
-- after
select v.vehicle_no, p.ts, p.lat, p.lon from perf.vehicles v cross join lateral (select ts, lat, lon from perf.pings p where p.vehicle_no = v.vehicle_no order by ts desc limit 1) p
```

### 4. Data-quality worklist (rare rows)

**Problem.** ~0.3% of pings are bad; finding them means scanning every row.

**Fix.** Partial index covering only the bad rows: tiny, and only maintained when a bad row is written.

Buffers touched: 48,688 → 15

```sql
-- before
select vehicle_no, ts, quality_issue from perf.pings where quality_issue is not null order by ts desc limit 100

create index if not exists pings_bad on perf.pings (ts desc) where quality_issue is not null;
-- after
select vehicle_no, ts, quality_issue from perf.pings where quality_issue is not null order by ts desc limit 100
```

### 5. Daily pings per vehicle for a dashboard

**Problem.** The dashboard aggregates 5M rows on every refresh.

**Fix.** Pre-aggregate into a materialized view refreshed by the pipeline; the dashboard reads ~10k rows.

Buffers touched: 15,498 → 115

```sql
-- before
select vehicle_no, ts::date as day, count(*) as pings, max(speed_kmph) as max_speed from perf.pings where ts >= '2026-07-01' and ts < '2026-08-01' group by 1, 2

create materialized view if not exists perf.daily_vehicle_pings as select vehicle_no, ts::date as day, count(*) as pings, max(speed_kmph) as max_speed from perf.pings group by 1, 2;
create index if not exists dvp_day on perf.daily_vehicle_pings (day);
-- after
select vehicle_no, day, pings, max_speed from perf.daily_vehicle_pings where day >= '2026-07-01' and day < '2026-08-01'
```

**Trade-off to mention in an interview:** every index slows writes and costs disk. The pipeline writes pings in bulk once and reads them many times, so read-optimising is right here; for a write-heavy OLTP table the answer would differ.
