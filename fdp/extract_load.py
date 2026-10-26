"""Extract + load: landing zone files and the ERP database into raw tables.

Guarantees:
  * idempotent   - a file is loaded once per content hash; re-running a day is a no-op,
                   and a corrected re-delivery of the same file replaces the old rows
  * incremental  - the ERP is read from a high-water mark (with a lookback window for
                   late commits), never a full table scan
  * auditable    - every batch is a row in meta.load_audit with counts and errors
  * fast         - rows are streamed with COPY, not INSERT per row
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import logging
import os
from collections.abc import Callable, Iterable
from datetime import timedelta

import psycopg

from .config import PG_DSN
from .db import bootstrap, connect
from .generate import TMS_COLUMNS
from .storage import ObjectStore, checksum, open_store

log = logging.getLogger("fdp.load")

ERP_DSN = os.getenv("FDP_ERP_DSN", PG_DSN)  # the source database; same server locally, separate in production
ERP_LOOKBACK = timedelta(hours=1)  # re-read a little behind the watermark to catch rows committed late


def _start_batch(conn: psycopg.Connection, source: str, key: str | None) -> int:
    with conn.transaction():
        return conn.execute("insert into meta.load_audit (source, object_key) values (%s, %s) returning batch_id", (source, key)).fetchone()[0]


def _finish_batch(conn: psycopg.Connection, batch_id: int, rows: int | None, error: str | None = None) -> None:
    with conn.transaction():
        conn.execute(
            "update meta.load_audit set rows_loaded = %s, status = %s, error = %s, finished_at = now() where batch_id = %s",
            (rows, "failed" if error else "ok", error, batch_id),
        )


def _load_file(
    conn: psycopg.Connection,
    store: ObjectStore,
    key: str,
    source: str,
    table: str,
    columns: list[str],
    rows_of: Callable[[bytes], Iterable[list]],
) -> int | None:
    """Load one file into `table`. Returns rows loaded, or None if it was already loaded unchanged."""
    data = store.get(key)
    digest = checksum(data)
    previous = conn.execute("select checksum, batch_id from meta.loaded_files where object_key = %s", (key,)).fetchone()
    if previous and previous[0] == digest:
        return None
    batch_id = _start_batch(conn, source, key)
    try:
        with conn.transaction(), conn.cursor() as cur:
            if previous:  # re-delivered with different content: replace, don't double count
                cur.execute(f"delete from {table} where _batch_id = %s", (previous[1],))
            n = 0
            with cur.copy(f"copy {table} ({', '.join(columns)}, _batch_id) from stdin") as cp:
                for row in rows_of(data):
                    cp.write_row([*row, batch_id])
                    n += 1
            cur.execute(
                """insert into meta.loaded_files (object_key, checksum, batch_id) values (%s, %s, %s)
                   on conflict (object_key) do update set checksum = excluded.checksum, batch_id = excluded.batch_id, loaded_at = now()""",
                (key, digest, batch_id),
            )
        _finish_batch(conn, batch_id, n)
        return n
    except Exception as exc:
        _finish_batch(conn, batch_id, None, f"{type(exc).__name__}: {exc}")
        raise


def _csv_rows(columns: list[str]) -> Callable[[bytes], Iterable[list]]:
    def rows(data: bytes) -> Iterable[list]:
        reader = csv.DictReader(io.StringIO(data.decode("utf-8")))
        missing = set(columns) - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"file is missing columns {sorted(missing)}")  # schema drift: fail loudly
        for r in reader:
            yield [r[c] if r[c] != "" else None for c in columns]

    return rows


GPS_COLUMNS = ["vehicle_no", "ts", "lat", "lon", "speed_kmph"]


def _gps_rows(data: bytes) -> Iterable[list]:
    for line in gzip.decompress(data).splitlines():
        if line.strip():
            p = json.loads(line)
            yield [p.get(c) for c in GPS_COLUMNS]


RATE_COLUMNS = ["origin", "destination", "capacity_t", "rate_per_trip", "effective_from"]


def load_prefix(prefix: str, source: str, table: str, columns: list[str], rows_of, day: str | None = None,
                store: ObjectStore | None = None) -> dict:
    store = store or open_store()
    keys = store.list(f"{prefix}/dt={day}" if day else prefix)
    loaded = skipped = rows = 0
    with connect() as conn:
        for key in keys:
            n = _load_file(conn, store, key, source, table, columns, rows_of)
            if n is None:
                skipped += 1
            else:
                loaded, rows = loaded + 1, rows + n
    log.info("%s: %d files loaded (%d rows), %d unchanged", source, loaded, rows, skipped)
    return {"files_loaded": loaded, "files_skipped": skipped, "rows": rows}


def load_tms(day: str | None = None, store: ObjectStore | None = None) -> dict:
    return load_prefix("tms", "tms", "raw.tms_shipments", TMS_COLUMNS, _csv_rows(TMS_COLUMNS), day, store)


def load_gps(day: str | None = None, store: ObjectStore | None = None) -> dict:
    return load_prefix("gps", "gps", "raw.gps_pings", GPS_COLUMNS, _gps_rows, day, store)


def load_rate_cards(store: ObjectStore | None = None) -> dict:
    return load_prefix("rates", "rates", "raw.rate_cards", RATE_COLUMNS, _csv_rows(RATE_COLUMNS), None, store)


ERP_COLUMNS = ["invoice_id", "shipment_id", "transporter", "seller_gstin", "buyer_gstin", "invoice_date", "freight_amount",
               "other_charges", "taxable_value", "cgst", "sgst", "igst", "total_amount", "updated_at"]


def extract_erp_invoices() -> dict:
    """Incremental pull from the ERP by updated_at. Staging keeps the latest version per invoice."""
    with connect() as conn:
        row = conn.execute("select value from meta.watermarks where source = 'erp.invoices'").fetchone()
        since = row[0] - ERP_LOOKBACK if row else None
        batch_id = _start_batch(conn, "erp.invoices", f"since {since.isoformat() if since else 'beginning'}")
        try:
            with connect(ERP_DSN) as src, src.cursor(name="erp_extract") as cur:  # server-side cursor: constant memory
                cur.itersize = 5000
                cur.execute(
                    f"select {', '.join(ERP_COLUMNS)} from erp.invoices where %s::timestamptz is null or updated_at > %s order by updated_at",
                    (since, since),
                )
                n, high = 0, row[0] if row else None
                with conn.transaction(), conn.cursor() as dst:
                    with dst.copy(f"copy raw.erp_invoices ({', '.join(ERP_COLUMNS)}, _batch_id) from stdin") as cp:
                        for r in cur:
                            cp.write_row([*r, batch_id])
                            n += 1
                            high = r[-1] if high is None or r[-1] > high else high
                    if high is not None:
                        dst.execute(
                            """insert into meta.watermarks (source, value) values ('erp.invoices', %s)
                               on conflict (source) do update set value = excluded.value, updated_at = now()""",
                            (high,),
                        )
            _finish_batch(conn, batch_id, n)
        except Exception as exc:
            _finish_batch(conn, batch_id, None, f"{type(exc).__name__}: {exc}")
            raise
    log.info("erp.invoices: %d rows since %s", n, since)
    return {"rows": n, "since": since.isoformat() if since else None}


def load_all(day: str | None = None, store: ObjectStore | None = None) -> dict:
    bootstrap()
    return {
        "rates": load_rate_cards(store),
        "tms": load_tms(day, store),
        "gps": load_gps(day, store),
        "erp": extract_erp_invoices(),
    }
