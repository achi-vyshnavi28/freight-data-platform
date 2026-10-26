"""Guardrails for model-written SQL. The model proposes; this module decides what may run.

Defence in depth:
  1. parse   - exactly one statement, and it must be a read query
  2. scope   - only whitelisted schemas/tables (the marts), no system catalogs
  3. deny    - no dangerous functions (pg_sleep, file access, dblink, set_config ...)
  4. bound   - a LIMIT is always present and capped
  5. execute - inside a READ ONLY transaction with a statement timeout
               (and, in production, as a role that only has SELECT on marts: sql/roles.sql)
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp

ALLOWED_SCHEMAS = {"marts"}
MAX_ROWS = 1000
DENIED_FUNCTIONS = {
    "pg_sleep",
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "pg_stat_file",
    "lo_import",
    "lo_export",
    "dblink",
    "dblink_exec",
    "set_config",
    "pg_terminate_backend",
    "pg_cancel_backend",
    "pg_reload_conf",
    "current_setting",
    "query_to_xml",
    "copy",
}
WRITE_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.Merge,
    exp.Command,
    exp.Grant,
    exp.TruncateTable,
    exp.Transaction,
    exp.Set,
)


class UnsafeSQL(ValueError):
    pass


@dataclass
class GuardedSQL:
    sql: str
    tables: list[str]


def guard(sql: str, allowed_tables: set[str] | None = None, max_rows: int = MAX_ROWS) -> GuardedSQL:
    try:
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except sqlglot.errors.ParseError as e:
        raise UnsafeSQL(f"could not parse SQL: {e}") from e
    if len(statements) != 1:
        raise UnsafeSQL(f"expected exactly one statement, got {len(statements)}")
    tree = statements[0]
    if not isinstance(tree, exp.Query):
        raise UnsafeSQL(f"only SELECT queries are allowed, got {type(tree).__name__}")
    for node in tree.walk():
        if isinstance(node, WRITE_NODES):
            raise UnsafeSQL(f"write/DDL operation not allowed: {type(node).__name__}")
        if isinstance(node, exp.Func):
            name = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()
            if name in DENIED_FUNCTIONS:
                raise UnsafeSQL(f"function not allowed: {name}")
        if isinstance(node, exp.Into):
            raise UnsafeSQL("SELECT INTO is not allowed")

    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    tables = []
    for t in tree.find_all(exp.Table):
        name, schema = t.name.lower(), (t.db or "").lower()
        if not schema and name in cte_names:
            continue
        if isinstance(t.this, exp.Func):  # table functions like generate_series are fine, they read nothing
            continue
        if schema not in ALLOWED_SCHEMAS:
            raise UnsafeSQL(
                f"table {schema + '.' if schema else ''}{name} is outside the allowed schemas {sorted(ALLOWED_SCHEMAS)}"
            )
        if allowed_tables is not None and name not in allowed_tables:
            raise UnsafeSQL(f"unknown table marts.{name}")
        tables.append(f"{schema}.{name}")

    limit = tree.args.get("limit")
    if limit is None:
        tree = tree.limit(max_rows)
    else:
        try:
            n = int(limit.expression.name)
        except (AttributeError, ValueError):
            n = max_rows + 1
        if n > max_rows:
            tree = tree.limit(max_rows)
    return GuardedSQL(tree.sql(dialect="postgres"), sorted(set(tables)))


def run_readonly(conn, sql: str, timeout_ms: int = 5000) -> tuple[list[str], list[tuple]]:
    with conn.transaction():
        conn.execute("set transaction read only")
        conn.execute(f"set local statement_timeout = {int(timeout_ms)}")
        cur = conn.execute(sql)
        return [d.name for d in cur.description], cur.fetchall()
