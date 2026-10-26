"""Ask the warehouse a question in English; get back guarded SQL, the rows, and an explanation.

    python -m fdp.ai.text_to_sql "Which transporter overbilled us the most in July?"

Schema context comes from the live database (columns) plus dbt (model descriptions),
so the prompt never drifts from what actually exists. If the SQL is rejected by the
guard or fails in Postgres, the error goes back to the model once for a repair.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, field

from ..config import DBT_DIR
from ..db import connect
from .sql_guard import guard, run_readonly

MODEL = os.getenv("FDP_MODEL", "claude-opus-5-5")

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "sql": {"type": "string", "description": "One PostgreSQL SELECT over the marts schema"},
        "explanation": {"type": "string", "description": "One or two sentences on how the query answers the question"},
    },
    "required": ["sql", "explanation"],
    "additionalProperties": False,
}


def schema_context(conn) -> tuple[str, set[str]]:
    descriptions: dict[str, str] = {}
    manifest = DBT_DIR / "target" / "manifest.json"
    if manifest.exists():
        for node in json.loads(manifest.read_text())["nodes"].values():
            if node.get("resource_type") == "model" and node.get("schema") == "marts":
                descriptions[node["name"]] = node.get("description", "")
    rows = conn.execute(
        """select table_name, string_agg(column_name || ' ' || data_type, ', ' order by ordinal_position)
           from information_schema.columns where table_schema = 'marts' group by table_name order by table_name"""
    ).fetchall()
    lines = [f"marts.{t}({cols})" + (f"  -- {descriptions[t]}" if descriptions.get(t) else "") for t, cols in rows]
    return "\n".join(lines), {t for t, _ in rows}


SYSTEM = """You write PostgreSQL for a freight analytics warehouse. Use only these tables:

{schema}

Rules:
- One SELECT statement, schema-qualified tables (marts.x). No DDL, DML or session settings.
- on_time is null for undelivered shipments; percentages should be 0-100 rounded to 1 decimal.
- Money columns are INR. "Overbilled" means is_overbilled = true.
- If the question cannot be answered from these tables, return SQL that selects a single row explaining why."""


@dataclass
class Answer:
    question: str
    sql: str
    explanation: str
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    attempts: int = 1
    error: str | None = None


def _ask(client, system: str, messages: list[dict]) -> dict:
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=system,
        messages=messages,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": ANSWER_SCHEMA}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("model declined the request")
    text = next(b.text for b in response.content if b.type == "text")
    return {"parsed": json.loads(text), "content": response.content}


def answer(question: str, client=None, conn=None, max_attempts: int = 2) -> Answer:
    if client is None:
        import anthropic

        client = anthropic.Anthropic()
    own_conn = conn is None
    conn = conn or connect()
    try:
        schema, tables = schema_context(conn)
        system = SYSTEM.format(schema=schema)
        messages: list[dict] = [{"role": "user", "content": question}]
        last_error = None
        for attempt in range(1, max_attempts + 1):
            out = _ask(client, system, messages)
            proposal = out["parsed"]
            try:
                guarded = guard(proposal["sql"], allowed_tables=tables)
                cols, rows = run_readonly(conn, guarded.sql)
                return Answer(question, guarded.sql, proposal["explanation"], cols, rows, attempt)
            except Exception as exc:  # guard rejection (UnsafeSQL) or database error: let the model repair once
                last_error = f"{type(exc).__name__}: {exc}"
                messages += [
                    {"role": "assistant", "content": out["content"]},
                    {"role": "user", "content": f"That query failed: {last_error}\nReturn a corrected query."},
                ]
        return Answer(question, proposal["sql"], proposal["explanation"], attempts=max_attempts, error=last_error)
    finally:
        if own_conn:
            conn.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    a = ap.parse_args()
    ans = answer(a.question)
    print(f"-- {ans.explanation}\n{ans.sql}\n")
    if ans.error:
        print(f"FAILED after {ans.attempts} attempts: {ans.error}")
        return
    print(" | ".join(ans.columns))
    for r in ans.rows[:50]:
        print(" | ".join(str(v) for v in r))


if __name__ == "__main__":
    main()
