"""AI-assisted debugging of data-quality failures.

After `dbt build`, read run_results.json, pull a sample of the failing rows that dbt
stored (store_failures: true), and write reports/dq_triage.md with a probable cause
and a suggested fix per failing test. With no API key it writes the same report
without the AI section, so the triage file always exists.

    python -m fdp.ai.explain_failures
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..config import DBT_DIR, ROOT
from ..db import connect
from .text_to_sql import MODEL

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "probable_cause": {"type": "string"},
        "is_pipeline_bug": {"type": "boolean", "description": "true if our code is wrong, false if the source data is wrong"},
        "suggested_fix": {"type": "string"},
    },
    "required": ["probable_cause", "is_pipeline_bug", "suggested_fix"],
    "additionalProperties": False,
}


def failing_tests(run_results: Path | None = None) -> list[dict]:
    path = run_results or DBT_DIR / "target" / "run_results.json"
    manifest = json.loads((DBT_DIR / "target" / "manifest.json").read_text())
    out = []
    for r in json.loads(path.read_text())["results"]:
        if r["status"] not in ("fail", "warn", "error") or not r["unique_id"].startswith("test."):
            continue
        node = manifest["nodes"].get(r["unique_id"], {})
        out.append(
            {
                "test": node.get("name", r["unique_id"]),
                "status": r["status"],
                "failures": r.get("failures"),
                "message": r.get("message"),
                "sql": node.get("compiled_code") or node.get("raw_code", ""),
                "failures_table": node.get("relation_name"),
            }
        )
    return out


def sample_rows(conn, relation: str | None, n: int = 5) -> list[dict]:
    if not relation:
        return []
    try:
        cur = conn.execute(f"select * from {relation} limit {n}")
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, map(str, row), strict=True)) for row in cur.fetchall()]
    except Exception:
        conn.rollback()
        return []


def triage(client=None) -> str:
    tests = failing_tests()
    if client is None and os.getenv("ANTHROPIC_API_KEY"):
        import anthropic

        client = anthropic.Anthropic()
    lines = ["# Data-quality triage\n"]
    if not tests:
        lines.append("All dbt tests passed. Nothing to triage.")
    with connect() as conn:
        for t in tests:
            rows = sample_rows(conn, t["failures_table"])
            lines.append(f"## `{t['test']}`: {t['status']} ({t['failures']} rows)\n")
            if rows:
                lines.append("Sample failing rows:\n```\n" + "\n".join(json.dumps(r) for r in rows) + "\n```")
            if client is not None:
                response = client.beta.messages.create(
                    model=MODEL,
                    max_tokens=16000,
                    system="You triage dbt data-quality test failures for a freight data pipeline. Be specific and brief.",
                    messages=[{"role": "user", "content": f"Test SQL:\n{t['sql']}\n\nFailing rows:\n{json.dumps(rows)}"}],
                    output_config={"effort": "low", "format": {"type": "json_schema", "schema": TRIAGE_SCHEMA}},
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                )
                if response.stop_reason != "refusal":
                    d = json.loads(next(b.text for b in response.content if b.type == "text"))
                    owner = (
                        "pipeline bug, fix our code" if d["is_pipeline_bug"] else "source data issue, raise with the data owner"
                    )
                    lines.append(
                        f"**Probable cause:** {d['probable_cause']}\n\n**Owner:** {owner}\n\n**Suggested fix:** {d['suggested_fix']}\n"
                    )
    path = ROOT / "reports" / "dq_triage.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


if __name__ == "__main__":
    print(triage())
