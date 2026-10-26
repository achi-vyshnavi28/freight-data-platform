"""Text-to-SQL and triage with a scripted fake model, against the real warehouse."""

import json
from pathlib import Path
from types import SimpleNamespace

from fdp.ai.text_to_sql import answer


class FakeMessages:
    def __init__(self, replies):
        self.replies, self.requests = list(replies), []

    def create(self, **params):
        self.requests.append(params)
        sql, why = self.replies.pop(0)
        text = json.dumps({"sql": sql, "explanation": why})
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)])


def fake_client(*replies):
    messages = FakeMessages(replies)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


def test_answer_runs_guarded_sql(built_warehouse, conn):
    client, msgs = fake_client(("select transporter from marts.mart_transporter_scorecard order by overbilled_pct desc", "top"))
    ans = answer("who overbills most?", client=client, conn=conn)
    assert ans.error is None and ans.rows[0][0] == "Godavari Freight Carriers"
    assert ans.sql.lower().endswith("limit 1000")
    # the prompt carried the live schema
    assert "marts.fct_shipments(" in msgs.requests[0]["system"]


def test_unsafe_sql_is_sent_back_for_repair(built_warehouse, conn):
    client, msgs = fake_client(
        ("delete from marts.fct_shipments", "oops"),
        ("select count(*) from marts.fct_shipments", "fixed"),
    )
    ans = answer("how many shipments?", client=client, conn=conn)
    assert ans.attempts == 2 and ans.error is None and ans.rows[0][0] > 0
    assert "only SELECT" in msgs.requests[1]["messages"][-1]["content"]
    assert conn.execute("select count(*) from marts.fct_shipments").fetchone()[0] > 0  # nothing was deleted


def test_database_errors_are_repaired_too(built_warehouse, conn):
    client, _ = fake_client(
        ("select no_such_column from marts.fct_shipments", "typo"),
        ("select count(*) from marts.fct_shipments", "fixed"),
    )
    assert answer("count", client=client, conn=conn).attempts == 2


def test_gives_up_after_two_bad_attempts(built_warehouse, conn):
    client, _ = fake_client(("select * from raw.erp_invoices", "a"), ("select * from pg_shadow", "b"))
    ans = answer("show me secrets", client=client, conn=conn)
    assert ans.error and not ans.rows


def test_triage_report_without_api_key(built_warehouse):
    from fdp.ai.explain_failures import triage

    text = Path(triage()).read_text()
    assert "arithmetic_ok" in text  # the warn-level test shows up with sample rows
