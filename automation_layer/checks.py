"""Checks that run before anything is delivered.

Reconciliation is a hard gate: if the cache does not match the sources row for row,
the run stops and nothing is published. Data-quality findings are reported, not fatal:
they are real problems in the source systems that the reader should know about.
"""
from __future__ import annotations

from datetime import timedelta

import pandas as pd

from . import config as C
from .extract import source_totals


class ReconciliationError(RuntimeError):
    pass


def reconcile(con) -> pd.DataFrame:
    """Cache vs sources: row counts for every table and the invoiced fee total."""
    src = source_totals()
    rows = []
    for table, tot in src.items():
        cached = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        rows.append({"check": f"{table}: rows", "source": tot["n"], "cache": float(cached)})
    fee = con.execute("SELECT COALESCE(SUM(fee), 0) FROM invoices").fetchone()[0]
    rows.append({"check": "invoices: fee total", "source": src["invoices"]["fee"], "cache": float(fee)})
    df = pd.DataFrame(rows)
    df["match"] = (df["source"] - df["cache"]).abs() < 0.005
    return df


def require_reconciled(recon: pd.DataFrame) -> None:
    bad = recon[~recon["match"]]
    if not bad.empty:
        raise ReconciliationError("Cache does not match the sources:\n" + bad.to_string(index=False))


def quality(con, as_of) -> pd.DataFrame:
    """Cross-system problems a single-source query cannot see."""
    grace = as_of - timedelta(days=C.INVOICE_GRACE_DAYS)
    queries = {
        "Closed parcel with no invoice (billing gap)":
            ("SELECT COUNT(*) FROM parcels p LEFT JOIN invoices i USING (parcel_id) "
             "WHERE p.closed_at IS NOT NULL AND p.closed_at < ? AND i.parcel_id IS NULL", [grace],
             "SELECT COUNT(*) FROM parcels WHERE closed_at IS NOT NULL AND closed_at < ?", [grace]),
        "Invoice for a parcel the operations system does not have":
            ("SELECT COUNT(*) FROM invoices i LEFT JOIN parcels p USING (parcel_id) WHERE p.parcel_id IS NULL", [],
             "SELECT COUNT(*) FROM invoices", []),
        "Parcel whose hub or merchant is missing from the CRM":
            ("SELECT COUNT(*) FROM parcels p LEFT JOIN hubs h USING (hub_id) LEFT JOIN merchants m USING (merchant_id) "
             "WHERE h.hub_id IS NULL OR m.merchant_id IS NULL", [], "SELECT COUNT(*) FROM parcels", []),
        "Merchant with no account manager":
            ("SELECT COUNT(*) FROM merchants WHERE account_manager IS NULL", [], "SELECT COUNT(*) FROM merchants", []),
    }
    rows = []
    for name, (q, p, base_q, base_p) in queries.items():
        issues = con.execute(q, p).fetchone()[0]
        base = con.execute(base_q, base_p).fetchone()[0]
        rows.append({"check": name, "issues": int(issues), "out_of": int(base),
                     "rate": round(issues / base, 4) if base else 0.0})
    return pd.DataFrame(rows)
