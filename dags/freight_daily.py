"""Daily freight pipeline.

    landing partition for {{ ds }} ──┬─ load_tms ─┐
                                     ├─ load_gps ─┤
    ERP database (incremental) ──────┼─ load_erp ─┼─ dbt_build ─ report
    rate card ───────────────────────┴─ load_rates┘        └─ triage (runs even if dbt fails)

Each task is the same CLI command a developer runs locally, so there is no
"works in Airflow but not on my laptop" logic hidden in the DAG.
catchup=True + max_active_runs=1 means a backfill replays days in order; loads
are idempotent, so re-running any day is safe.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from airflow import DAG

try:  # Airflow 3
    from airflow.providers.standard.operators.bash import BashOperator
except ImportError:  # Airflow 2.x
    from airflow.operators.bash import BashOperator

PROJECT = "/opt/freight-data-platform"
CLI = f"cd {PROJECT} && python -m fdp.pipeline"

default_args = {
    "owner": "data-platform",
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
    "retry_exponential_backoff": True,
}

with DAG(
    dag_id="freight_daily",
    description="Ingest TMS, GPS, ERP and rate cards; build and test the warehouse; publish findings.",
    start_date=datetime(2026, 6, 1),
    schedule="30 2 * * *",  # 02:30 IST-ish: after the TMS nightly export lands
    catchup=True,
    max_active_runs=1,
    default_args=default_args,
    tags=["freight", "dbt"],
) as dag:
    load_tms = BashOperator(task_id="load_tms", bash_command=f"{CLI} load --source tms --day {{{{ ds }}}}")
    load_gps = BashOperator(task_id="load_gps", bash_command=f"{CLI} load --source gps --day {{{{ ds }}}}")
    load_erp = BashOperator(task_id="load_erp", bash_command=f"{CLI} load --source erp")
    load_rates = BashOperator(task_id="load_rates", bash_command=f"{CLI} load --source rates")

    # build = models + tests in dependency order; a failing error-severity test stops downstream models
    dbt_build = BashOperator(
        task_id="dbt_build",
        bash_command=f"{CLI} transform",
        execution_timeout=timedelta(minutes=30),
    )
    report = BashOperator(task_id="report", bash_command=f"{CLI} report")
    triage = BashOperator(task_id="triage", bash_command=f"{CLI} triage", trigger_rule="all_done", retries=0)

    [load_tms, load_gps, load_erp, load_rates] >> dbt_build >> [report, triage]
