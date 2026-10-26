# Freight Data Platform

An end-to-end data pipeline for a logistics client. It takes data from **four messy source systems**, loads it into **Postgres**, transforms and tests it with **dbt**, orchestrates it with **Airflow**, serves it to **Grafana**, and lets people **ask questions in English** through guarded AI-generated SQL.

It is built like a forward deployed engineer's first month at a client. The source data is broken in realistic ways, the job is to make it trustworthy, and the payoff is finding money the business is losing.

## What it found

From 1,200 shipments, 650k GPS pings and 1,176 invoices (`reports/findings.md`, regenerated on every run):

- **₹3.6 lakh overbilled** across 93 invoices. **One transporter accounts for 83% of it**: it pads 30% of its invoices.
- **₹13.9 lakh of delivered freight never invoiced**, plus 7 invoices for shipments that don't exist.
- **The Mumbai lanes are on time only 18–23% of the time**, against 86–94% elsewhere. Trips with a stop over 60 minutes are on time 67% of the time vs 82%.
- **The obvious query is wrong.** Rates rose 6% on 1 July. Comparing invoices with the *current* rate card misses **19 of 93** overbilled invoices (20%), because padded June bills look normal against July prices. The marts join the rate *in force on the dispatch date* (SCD type 2 validity ranges).

## Architecture

```
 SOURCE SYSTEMS                 INGEST (Python)                  WAREHOUSE (Postgres + dbt)                   SERVE
 ──────────────                 ───────────────                  ─────────────────────────                    ─────
 TMS daily CSV  ─┐              COPY streaming                   raw.*            text, as received
 GPS JSONL.gz   ─┤  S3/MinIO ─▶ idempotent (content hash)  ─▶    staging.*        parse, normalise, dedupe    ─▶ Grafana (read-only role)
 Rate card CSV  ─┘  or local    audited (meta.load_audit)         intermediate.*   trips, stops, rate history  ─▶ findings.md report
 ERP database  ───────────────▶ incremental (watermark+lookback)  marts.*          facts, scorecards, worklists ─▶ AI text-to-SQL (guarded)
                                schema-drift detection            quality.*        quarantine + failed test rows ─▶ AI test-failure triage
                                                     ▲
                                            Airflow DAG (daily, backfillable) · dbt build = models + 51 data tests
```

## Coverage of the job description

| They want | Where it is |
|---|---|
| **Strong SQL: write, read, tune** | 16 dbt models: window functions (`ROW_NUMBER`, `LAG`, `LEAD`, `RANK`, running sums), gaps-and-islands, `DISTINCT ON`, `percentile_cont`, `FILTER`, anti-joins, effective-dated joins, GSTIN checksum in pure SQL. **Tuning:** [`performance/RESULTS.md`](performance/RESULTS.md) |
| Python for data | `fdp/`: streaming COPY loads, gzip/JSON/CSV parsing, a data simulator, CLI |
| Postgres (SQL Server) | Postgres 16 warehouse. The ERP source schema stands in for a client SQL Server; only the source DSN changes |
| **At least one end-to-end pipeline** | `python -m fdp.pipeline run`: ingest → transform → model → test → deliver |
| Data-analysis fundamentals | `fdp/report.py` → `reports/findings.md`: vendor scorecard, lane reliability, delay drivers, revenue leakage |
| GitHub with data projects | This repo, with CI |
| S3 / Azure Blob | `fdp/storage.py`: one interface for a local folder or S3/MinIO, tested with moto |
| Airflow / mage.ai | `dags/freight_daily.py`: parallel loads, retries, backfill-safe, triage on failure |
| dbt | `dbt/`: sources, staging, intermediate, marts, seeds, macros, incremental model, generic and singular tests, `store_failures` |
| Snowflake / Databricks / BigQuery | Not used. The dbt models are warehouse-agnostic SQL apart from a few Postgres functions |
| GitLab CI/CD | `.gitlab-ci.yml`, and the same pipeline in `.github/workflows/ci.yml` |
| Grafana | `grafana/`: provisioned datasource and 9-panel dashboard; every panel query is tested |
| LLM / RAG awareness | `fdp/ai/`: text-to-SQL with schema context from dbt, SQL guard, read-only execution, self-repair, eval set; AI triage of failed data tests |
| "AI for query generation, data quality and debugging" | Exactly the three things `fdp/ai/` does |

## The source data is broken on purpose

| Source | What's wrong with it | How it's handled |
|---|---|---|
| TMS CSV | A shipment reappears at every status change; 4 date formats; "Hyd", "Bombay", "Bengaluru"; weights in kg, MT, "9,800 KGS"; padded/lowercase plates; duplicate rows; missing cities | `stg_tms__shipments`: alias seed, format-aware parsing, latest-version dedupe with tie-break, rows with problems go to `quality.quarantine_shipments` with a reason |
| GPS | Duplicates, out-of-order rows, (0,0) fixes, 300+ km/h spikes, files re-delivered with corrections | Incremental dedupe on a hashed key, validity flags, re-delivered files replace their old batch |
| ERP | Invoices edited in place, arithmetic errors, invoices for unknown shipments | Watermark + 1 h lookback extraction, latest-revision dedupe, arithmetic and GST tax-type checks |
| Rate card | Mid-period revision | Validity ranges via `LEAD()`, joined on dispatch date |

## SQL tuning

5.2M-row GPS table, `EXPLAIN (ANALYZE, BUFFERS)`, median of 3 warm runs. Full write-up with plans and reasoning: [`performance/RESULTS.md`](performance/RESULTS.md).

| Query | Before | After | Fix |
|---|---|---|---|
| One vehicle's track for a day | 131 ms | 0.5 ms | Composite B-tree `(vehicle_no, ts)` |
| `date(ts) = ...` filter | 184 ms | 5.8 ms | Sargable range rewrite + BRIN (**72 KB** vs 201 MB for a B-tree) |
| Latest position per vehicle | **45 s** | **2.5 ms** | Correlated subquery → `LATERAL ... ORDER BY ts DESC LIMIT 1` per vehicle |
| Bad-row worklist | 163 ms | 0.05 ms | Partial index `WHERE quality_issue IS NOT NULL` (160 KB) |
| Dashboard aggregate | 608 ms | 0.7 ms | Materialized view |

## Engineering decisions worth asking about

- **Raw is text, staging decides.** Raw tables keep every value as received, so a parsing bug is fixed by changing a model and rebuilding, never by re-ingesting.
- **Idempotency by content hash.** Re-running a day loads nothing; a corrected re-delivery replaces its own batch instead of double counting. Tested.
- **Audit rows must survive failures.** A test caught that a failed load's audit row was rolled back with the data, because the transaction was nested inside an implicit one. Loaders now use autocommit connections with explicit transactions.
- **Incremental with a lookback.** The ERP is read from `max(updated_at) - 1h` to catch rows committed late; staging dedupes the overlap.
- **Severity matters.** A wrong invoice is a *business finding* (`warn`), and a broken parser is a *pipeline failure* (`error`, which stops the marts). `assert_quarantine_is_small` fails the build if more than 2% of rows stop parsing, the usual sign of a source format change.
- **AI SQL is never trusted.** Model output is parsed with sqlglot (one SELECT, marts only, no dangerous functions, LIMIT enforced), run in a READ ONLY transaction with a timeout, and in production as a role that can only `SELECT` on marts. Rejected or failing SQL goes back to the model once for a repair.

## Run it

```bash
# needs Python 3.11+ and Postgres 16 (or: docker compose up -d db)
python -m venv .venv && . .venv/bin/activate
pip install ".[dev]"

python -m fdp.pipeline generate              # simulate the 4 source systems
python -m fdp.pipeline run                   # load -> dbt build (51 tests) -> reports/findings.md
python -m fdp.pipeline triage                # reports/dq_triage.md
python -m performance.tune --scale 8         # SQL tuning case study -> performance/RESULTS.md
ANTHROPIC_API_KEY=... python -m fdp.ai.text_to_sql "Which transporter overbilled us most in July?"

pytest -q                                    # 60 tests against a real Postgres (database fdp_test)
```

Full stack with MinIO, Airflow and Grafana: see the header of `docker-compose.yml`.

## What is verified and what is not

| Part | Status |
|---|---|
| Ingestion, idempotency, re-delivery, schema drift, incremental ERP, S3 (moto) | Tested |
| dbt build: 16 models, 51 data tests | Runs clean in CI and locally |
| Findings recovered from the data | Tested (`tests/test_findings.py`) |
| SQL guard, read-only execution, repair loop | Tested, with a scripted fake model |
| Grafana panel queries as the read-only role | Tested; Grafana itself not started here |
| Airflow DAG | Parsed and checked with Airflow 3.3 (`tests/test_dag.py`); not run in a scheduler here |
| Text-to-SQL against the live Claude API | **Not run yet** (needs a key): `python -m fdp.ai.eval_text_to_sql` measures execution accuracy on 8 questions |
| Docker Compose stack | Written, **not run** (no Docker daemon available) |

The data is simulated (`fdp/generate.py`), so the findings are planted and the tests check they are recovered. The SQL, the tuning and the engineering are real.
