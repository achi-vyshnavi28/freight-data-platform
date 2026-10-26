"""Execution-accuracy eval for text-to-SQL: does the model's query return the same answer as the gold query?

python -m fdp.ai.eval_text_to_sql --check-gold   # free: gold SQL passes the guard and runs (CI does this)
python -m fdp.ai.eval_text_to_sql                # calls Claude for every question (costs money)
"""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path

from ..db import connect
from .sql_guard import guard, run_readonly
from .text_to_sql import answer, schema_context

QUESTIONS = json.loads((Path(__file__).parent / "eval_questions.json").read_text())


def normalise(rows: list[tuple]) -> list[tuple]:
    """Order-insensitive, column-name-insensitive, numbers rounded: what a human would call the same answer."""

    def cell(v):
        if isinstance(v, float | Decimal):
            return round(float(v), 1)
        return v

    return sorted((tuple(cell(v) for v in r) for r in rows), key=repr)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check-gold", action="store_true")
    a = ap.parse_args()
    with connect() as conn:
        _, tables = schema_context(conn)
        correct = 0
        for q in QUESTIONS:
            _, gold_rows = run_readonly(conn, guard(q["gold"], tables).sql)
            if a.check_gold:
                correct += 1
                continue
            ans = answer(q["question"], conn=conn)
            ok = ans.error is None and normalise(ans.rows) == normalise(gold_rows)
            correct += ok
            print(
                f"{'PASS' if ok else 'FAIL'}  {q['question']}\n      {ans.sql}"
                + (f"\n      error: {ans.error}" if ans.error else "")
            )
    label = "gold queries valid" if a.check_gold else "execution accuracy"
    print(f"\n{label}: {correct}/{len(QUESTIONS)}")
    raise SystemExit(0 if correct == len(QUESTIONS) or not a.check_gold else 1)


if __name__ == "__main__":
    main()
