"""Pull rows from each source system, only what changed since the last run."""
from __future__ import annotations

import logging
import time
from datetime import datetime
from functools import lru_cache
from typing import Callable, TypeVar

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from . import config as C

log = logging.getLogger("layer.extract")
T = TypeVar("T")

# table -> (source system, primary key, columns)
TABLES: dict[str, tuple[str, str, list[str]]] = {
    "parcels": ("orders", "parcel_id",
                ["parcel_id", "merchant_id", "hub_id", "created_at", "closed_at", "status", "weight_g", "updated_at"]),
    "merchants": ("crm", "merchant_id",
                  ["merchant_id", "merchant_name", "segment", "account_manager", "signup_date", "updated_at"]),
    "hubs": ("crm", "hub_id", ["hub_id", "hub_name", "region", "updated_at"]),
    "invoices": ("finance", "parcel_id",
                 ["parcel_id", "fee", "cod_collected", "invoiced_at", "paid", "updated_at"]),
}
TIME_COLUMNS = {"created_at", "closed_at", "updated_at", "signup_date", "invoiced_at"}


@lru_cache(maxsize=None)
def engine(source: str):
    return create_engine(C.SOURCE_URLS[source])


def with_retry(fn: Callable[[], T], attempts: int = C.DB_RETRIES, wait: float = C.DB_RETRY_WAIT_SEC,
               errors: tuple = (OperationalError,)) -> T:
    """Run fn, retrying on connection errors with a growing pause."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except errors as exc:
            if attempt == attempts:
                raise
            log.warning("attempt %s/%s failed (%s); retrying in %ss", attempt, attempts,
                        str(exc).splitlines()[0], wait * attempt)
            time.sleep(wait * attempt)
    raise RuntimeError("unreachable")


def fetch(table: str, since: datetime | None = None) -> pd.DataFrame:
    """Rows of `table` changed after `since` (all rows when since is None)."""
    source, _key, cols = TABLES[table]
    sql = f"SELECT {', '.join(cols)} FROM {table}"
    params = {}
    if since is not None:
        sql += " WHERE updated_at > :since"
        params["since"] = since.strftime("%Y-%m-%d %H:%M:%S")

    def run() -> pd.DataFrame:
        with engine(source).connect() as con:
            return pd.read_sql_query(text(sql), con, params=params)

    df = with_retry(run)
    for col in TIME_COLUMNS & set(df.columns):
        df[col] = pd.to_datetime(df[col], errors="coerce")
    return df


def source_totals() -> dict[str, dict]:
    """Row counts (and fee total) straight from the sources, for reconciliation."""
    out = {}
    for table, (source, _key, _cols) in TABLES.items():
        extra = ", COALESCE(SUM(fee), 0) AS fee" if table == "invoices" else ""
        with engine(source).connect() as con:
            row = con.execute(text(f"SELECT COUNT(*) AS n{extra} FROM {table}")).mappings().one()
        out[table] = {k: float(v) for k, v in row.items()}
    return out
