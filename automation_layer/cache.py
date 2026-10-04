"""Local DuckDB cache: the place where data from separate systems finally meets.

The first run loads everything. Later runs ask each source only for rows whose
`updated_at` is newer than the last one seen, and upsert them by primary key.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd

from . import config as C
from .extract import TABLES, fetch

log = logging.getLogger("layer.cache")

SCHEMA = {
    "parcels": "parcel_id BIGINT PRIMARY KEY, merchant_id INTEGER, hub_id INTEGER, created_at TIMESTAMP, "
               "closed_at TIMESTAMP, status VARCHAR, weight_g INTEGER, updated_at TIMESTAMP",
    "merchants": "merchant_id INTEGER PRIMARY KEY, merchant_name VARCHAR, segment VARCHAR, account_manager VARCHAR, "
                 "signup_date TIMESTAMP, updated_at TIMESTAMP",
    "hubs": "hub_id INTEGER PRIMARY KEY, hub_name VARCHAR, region VARCHAR, updated_at TIMESTAMP",
    "invoices": "parcel_id BIGINT PRIMARY KEY, fee DOUBLE, cod_collected DOUBLE, invoiced_at TIMESTAMP, "
                "paid INTEGER, updated_at TIMESTAMP",
}


def connect(path: Path | None = None) -> duckdb.DuckDBPyConnection:
    path = Path(path or C.CACHE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    for table, ddl in SCHEMA.items():
        con.execute(f"CREATE TABLE IF NOT EXISTS {table} ({ddl})")
    con.execute("CREATE TABLE IF NOT EXISTS watermark (table_name VARCHAR PRIMARY KEY, updated_at TIMESTAMP)")
    return con


def watermark(con, table: str) -> datetime | None:
    row = con.execute("SELECT updated_at FROM watermark WHERE table_name = ?", [table]).fetchone()
    return row[0] if row else None


def upsert(con, table: str, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    key, cols = TABLES[table][1], ", ".join(TABLES[table][2])
    con.register("incoming", df)
    try:
        con.execute(f"DELETE FROM {table} WHERE {key} IN (SELECT {key} FROM incoming)")
        con.execute(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM incoming")
    finally:
        con.unregister("incoming")
    con.execute("INSERT OR REPLACE INTO watermark VALUES (?, ?)", [table, df["updated_at"].max().to_pydatetime()])
    return len(df)


def refresh(con, full: bool = False) -> dict[str, int]:
    """Bring every cached table up to date. Returns rows pulled per table."""
    pulled = {}
    for table in TABLES:
        if full:
            con.execute(f"DELETE FROM {table}")
            con.execute("DELETE FROM watermark WHERE table_name = ?", [table])
        since = watermark(con, table)
        pulled[table] = upsert(con, table, fetch(table, since))
        log.info("%-9s %7s rows pulled (%s)", table, f"{pulled[table]:,}",
                 "full load" if since is None else f"changed since {since:%Y-%m-%d %H:%M}")
    return pulled
