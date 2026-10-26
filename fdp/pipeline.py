"""One command per pipeline stage, plus `run` for all of them. Airflow calls the same commands.

    python -m fdp.pipeline generate --shipments 1200
    python -m fdp.pipeline run                  # load -> dbt build (models + tests) -> report
    python -m fdp.pipeline load --day 2026-06-15
    python -m fdp.pipeline report
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time

from .config import DBT_DIR
from .db import bootstrap, reset

log = logging.getLogger("fdp.pipeline")


def dbt(*args: str) -> None:
    """Run dbt as a subprocess so the exit code (failed tests!) stops the pipeline."""
    env = {**os.environ, "DBT_PROFILES_DIR": str(DBT_DIR)}
    cmd = ["dbt", *args, "--project-dir", str(DBT_DIR)]
    log.info("$ %s", " ".join(cmd))
    result = subprocess.run(cmd, env=env, check=False)
    if result.returncode != 0:
        raise SystemExit(f"dbt {' '.join(args)} failed with exit code {result.returncode}")


def transform(full_refresh: bool = False) -> None:
    dbt("seed", "--quiet")
    # build = run + test in DAG order: a failing staging test stops the marts from being rebuilt on bad data
    dbt("build", *(["--full-refresh"] if full_refresh else []))


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(prog="fdp")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate", help="simulate source systems")
    g.add_argument("--shipments", type=int, default=1200)
    g.add_argument("--days", type=int, default=92)
    g.add_argument("--ping-minutes", type=int, default=2)
    ld = sub.add_parser("load", help="extract + load into raw")
    ld.add_argument("--day", help="only this landing partition (YYYY-MM-DD)")
    tr = sub.add_parser("transform", help="dbt seed + build")
    tr.add_argument("--full-refresh", action="store_true")
    sub.add_parser("report", help="write reports/findings.md from the marts")
    run = sub.add_parser("run", help="load + transform + report")
    run.add_argument("--day")
    sub.add_parser("reset", help="drop all project schemas")
    a = ap.parse_args(argv)

    start = time.perf_counter()
    if a.cmd == "generate":
        from .generate import generate

        print(json.dumps(generate(a.shipments, days=a.days, ping_minutes=a.ping_minutes), indent=2))
    elif a.cmd == "load":
        from .extract_load import load_all

        print(json.dumps(load_all(a.day), indent=2, default=str))
    elif a.cmd == "transform":
        transform(a.full_refresh)
    elif a.cmd == "report":
        from .report import write_report

        print(write_report())
    elif a.cmd == "run":
        from .extract_load import load_all
        from .report import write_report

        bootstrap()
        print(json.dumps(load_all(a.day), indent=2, default=str))
        transform()
        print(write_report())
    elif a.cmd == "reset":
        reset()
    log.info("%s finished in %.1fs", a.cmd, time.perf_counter() - start)


if __name__ == "__main__":
    main(sys.argv[1:])
