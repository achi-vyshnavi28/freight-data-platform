"""DAG structure. Skipped unless Airflow is installed (pip install ".[airflow]")."""

import pytest

from fdp.config import ROOT

pytest.importorskip("airflow")


def test_dag_parses_and_is_wired_correctly():
    from airflow.models.dagbag import DagBag

    bag = DagBag(dag_folder=str(ROOT / "dags"))
    assert bag.import_errors == {}
    dag = bag.dags["freight_daily"]
    assert set(dag.get_task("dbt_build").upstream_task_ids) == {"load_tms", "load_gps", "load_erp", "load_rates"}
    assert set(dag.get_task("dbt_build").downstream_task_ids) == {"report", "triage"}
    assert str(dag.get_task("triage").trigger_rule).lower().endswith("all_done")
    assert dag.catchup and dag.max_active_runs == 1
