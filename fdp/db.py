"""Postgres helpers and the warehouse bootstrap (schemas, raw tables, load metadata)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg

from .config import PG_DSN

BOOTSTRAP_SQL = """
create schema if not exists raw;
create schema if not exists meta;
create schema if not exists erp;      -- stands in for the client's SQL Server ERP (source system)

-- Every load is auditable: which file, how many rows, when, did it succeed.
create table if not exists meta.load_audit (
    batch_id      bigserial primary key,
    source        text not null,
    object_key    text,
    rows_loaded   integer,
    status        text not null default 'running',
    error         text,
    started_at    timestamptz not null default now(),
    finished_at   timestamptz
);

-- File-level idempotency: a file is loaded once, keyed by its content hash.
create table if not exists meta.loaded_files (
    object_key    text primary key,
    checksum      text not null,
    batch_id      bigint references meta.load_audit(batch_id),
    loaded_at     timestamptz not null default now()
);

-- High-water marks for incremental extraction from databases.
create table if not exists meta.watermarks (
    source        text primary key,
    value         timestamptz not null,
    updated_at    timestamptz not null default now()
);

-- Raw tables keep source values as text: nothing is lost before staging decides how to parse it.
create table if not exists raw.tms_shipments (
    shipment_id     text,
    booked_at       text,
    dispatched_at   text,
    delivered_at    text,
    promised_at     text,
    origin          text,
    destination     text,
    transporter     text,
    vehicle_no      text,
    weight          text,
    status          text,
    updated_at      text,
    _batch_id       bigint not null,
    _loaded_at      timestamptz not null default now()
);

create table if not exists raw.gps_pings (
    vehicle_no      text,
    ts              text,
    lat             double precision,
    lon             double precision,
    speed_kmph      double precision,
    _batch_id       bigint not null
);

create table if not exists raw.erp_invoices (
    invoice_id      text,
    shipment_id     text,
    transporter     text,
    seller_gstin    text,
    buyer_gstin     text,
    invoice_date    text,
    freight_amount  numeric,
    other_charges   numeric,
    taxable_value   numeric,
    cgst            numeric,
    sgst            numeric,
    igst            numeric,
    total_amount    numeric,
    updated_at      timestamptz,
    _batch_id       bigint not null
);

create table if not exists raw.rate_cards (
    origin          text,
    destination     text,
    capacity_t      numeric,
    rate_per_trip   numeric,
    effective_from  text,
    _batch_id       bigint not null
);

-- Source ERP tables (in production these live in the client's own database).
create table if not exists erp.invoices (
    invoice_id      text primary key,
    shipment_id     text,
    transporter     text,
    seller_gstin    text,
    buyer_gstin     text,
    invoice_date    date,
    freight_amount  numeric(12,2),
    other_charges   numeric(12,2),
    taxable_value   numeric(12,2),
    cgst            numeric(12,2),
    sgst            numeric(12,2),
    igst            numeric(12,2),
    total_amount    numeric(12,2),
    updated_at      timestamptz not null
);
create index if not exists erp_invoices_updated_at on erp.invoices (updated_at);
"""


def connect(dsn: str = PG_DSN, **kw) -> psycopg.Connection:
    return psycopg.connect(dsn, **kw)


@contextmanager
def transaction(dsn: str = PG_DSN) -> Iterator[psycopg.Connection]:
    with connect(dsn) as conn, conn.transaction():
        yield conn


def bootstrap(dsn: str = PG_DSN) -> None:
    with transaction(dsn) as conn:
        conn.execute(BOOTSTRAP_SQL)


def reset(dsn: str = PG_DSN) -> None:
    """Drop everything this project owns. Used by tests and `fdp reset`."""
    with transaction(dsn) as conn:
        for schema in ("raw", "meta", "erp", "staging", "intermediate", "marts", "quality", "seeds", "snapshots"):
            conn.execute(f"drop schema if exists {schema} cascade")
